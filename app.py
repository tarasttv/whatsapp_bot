from dotenv import load_dotenv
load_dotenv()

import os
import datetime
import gspread
import json
import threading
import time
import re
from flask import Flask, request
from openai import OpenAI
from google.oauth2 import service_account
from twilio.twiml.messaging_response import MessagingResponse
from gspread.exceptions import APIError

app = Flask(__name__)

# OpenAI client
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Google Sheets setup
scope = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
creds_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
creds_dict = json.loads(creds_json)
if "\\n" in creds_dict.get("private_key", ""):
    creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")

creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=scope)
gsheets_client = gspread.authorize(creds)
sheet = gsheets_client.open("whatsapp_bot_sheet").sheet1

# State and message logs for users
user_states: dict[str, str] = {}
user_messages: dict[str, list[str]] = {}

# Queue for pending Google Sheets writes and lock for thread safety
pending_rows: list[list[str]] = []
pending_lock = threading.Lock()

# Store last normalized user question to detect repeats
last_question_norm: dict[str, str] = {}

def normalize_text(text: str) -> str:
    """
    Нормализация текста: удаление пунктуации, пробелов, приведение к нижнему регистру.
    """
    # Оставляем только буквы и цифры, приводим к нижнему регистру
    normalized = "".join(ch.lower() for ch in text if ch.isalnum())
    return normalized

def classify_message(text: str) -> str:
    """
    Классификация входящего сообщения на категории: 'greeting' (приветствие),
    'question' (вопрос), 'noise' (шум/бессмысленный набор).
    """
    text_stripped = text.strip()
    if text_stripped == "":
        return "noise"
    # Проверяем на шум: если нет ни одной буквы/цифры (только символы/эмодзи)
    alnum_count = sum(ch.isalnum() for ch in text_stripped)
    if alnum_count == 0:
        return "noise"
    # Подсчитываем слова
    words = text_stripped.lower().split()
    word_count = len(words)
    # Список типичных приветствий
    greetings = ["привет", "здравствуйте", "добрый день", "доброе утро", "добрый вечер", "здравствуй", "hi", "hello"]
    # Список слов-показателей вопроса (на русском)
    question_words = ["что", "как", "почему", "зачем", "когда", "где", "какой", "какая", "какие"]
    # Если сообщение похоже на вопрос (достаточно длинное и содержит вопросит. конструкцию)
    if word_count >= 4 and ("?" in text_stripped or any(qw in text_stripped.lower() for qw in question_words)):
        return "question"
    # Если сообщение содержит типичное приветствие и не было классифицировано как вопрос
    for greet in greetings:
        if greet in text_stripped.lower():
            return "greeting"
    # Иное сообщение (по умолчанию считаем приветствием/началом диалога)
    return "greeting"

def gpt_reply_short(user_text: str) -> str:
    """Ответ ассистента, упрощённый и до 400 символов."""
    resp = openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {
                "role": "system",
                "content": (
                    "Ты технический консультант. Отвечай максимально кратко, простым языком, "
                    "по шагам только при необходимости. Избегай канцеляризмов. "
                    "Строго не более 400 символов текста."
                )
            },
            {"role": "user", "content": user_text}
        ],
        max_tokens=220,  # достаточно для краткого ответа
        temperature=0.2
    )
    text = resp.choices[0].message.content.strip()
    # Жёсткое ограничение символов как страховка
    if len(text) > 400:
        text = text[:400].rstrip() + "…"
    return text

def simplify_answer_text(answer_text: str) -> str:
    """
    Перефразирует заданный текст ответа более простым и понятным языком.
    Используется, если пользователь повторно задаёт тот же вопрос.
    """
    resp = openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {
                "role": "system",
                "content": "Объясни пользователю предыдущий ответ более простым и понятным языком, избегая технических терминов."
            },
            {"role": "user", "content": answer_text}
        ],
        max_tokens=300,
        temperature=0.3
    )
    simplified = resp.choices[0].message.content.strip()
    return simplified

def save_to_sheet(phone: str, dialog: list[str]):
    """
    Сохраняет диалог в очередь для записи в Google Sheets (асинхронно из отдельного потока).
    """
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conversation = "\n".join(dialog)
    row = [timestamp, phone, conversation]
    # Помещаем запись в очередь
    with pending_lock:
        pending_rows.append(row)

def get_last_bot_answer(dialog: list[str]) -> str:
    """Возвращает текст последнего ответа бота из истории диалога."""
    for entry in reversed(dialog):
        if entry.startswith("Бот:"):
            # Возвращаем текст после префикса "Бот: "
            return entry[5:].strip()
    return ""

# Фоновый поток для периодической записи накопленных диалогов в Google Sheets
def flush_worker():
    """
    Фоновая задача: раз в 10 секунд либо при накоплении 3 и более записей отправляет данные в Google Sheets.
    Реализована с экспоненциальным бэкоффом при ошибках 429 или 5xx.
    """
    last_flush_time = time.time()
    while True:
        time.sleep(1)
        with pending_lock:
            count = len(pending_rows)
            if count == 0:
                continue  # нет данных для отправки
            now = time.time()
            if count < 3 and (now - last_flush_time < 10):
                # Недостаточно записей и 10 секунд ещё не прошло
                continue
            # Забираем копию накопленных записей для отправки и очищаем очередь
            batch = pending_rows[:]
            pending_rows.clear()
        # Пытаемся отправить batch в Google Sheets
        success = False
        give_up = False
        backoff = 1
        for attempt in range(5):
            try:
                sheet.append_rows(batch, value_input_option='RAW')
                success = True
                last_flush_time = time.time()
                break
            except APIError as e:
                # Получаем код ошибки, если доступен
                error_code = None
                if hasattr(e, 'response'):
                    try:
                        err_json = e.response.json()
                        if isinstance(err_json, dict):
                            error_code = err_json.get('error', {}).get('code')
                    except Exception:
                        if hasattr(e.response, 'status_code'):
                            error_code = e.response.status_code
                if error_code is None:
                    match = re.search(r'"code":\s*(\d+)', str(e))
                    if match:
                        error_code = int(match.group(1))
                # Проверяем, является ли ошибка временной (429 или 5xx)
                if error_code == 429 or (error_code is not None and 500 <= error_code < 600):
                    # Временная ошибка: ждем с экспоненциальным ростом интервала и повторяем
                    print(f"Предупреждение: получена ошибка {error_code} при сохранении, повтор через {backoff} сек.")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                else:
                    # Критическая/непоправимая ошибка - прерываем попытки
                    print(f"Ошибка записи в Google Sheets (не ретрай): {e}")
                    give_up = True
                    break
        if not success:
            if give_up:
                # Отбрасываем batch при критической ошибке, чтобы не зациклить поток
                print("Запись диалога пропущена из-за критической ошибки.")
            else:
                # В случае временной неудачи: возвращаем записи обратно в очередь для повторной попытки позже
                with pending_lock:
                    pending_rows[0:0] = batch

# Запуск фонового потока для записи в Google Sheets
flush_thread = threading.Thread(target=flush_worker, daemon=True)
flush_thread.start()

CONSULT_ENDING_MENU = (
    "\n\nВыберите:\n"
    "1 — Всё понятно, спасибо\n"
    "2 — Требуется дополнительная консультация"
)

@app.route("/webhook", methods=["POST"])
def whatsapp_reply():
    incoming_msg = request.values.get("Body", "").strip()
    sender_number = request.values.get("From", "").replace("whatsapp:", "")
    resp = MessagingResponse()
    msg = resp.message()

    state = user_states.get(sender_number, "start")
    dialog = user_messages.get(sender_number, [])

    # Начало диалога: классифицируем сообщение
    if state == "start":
        category = classify_message(incoming_msg)
        if category == "noise":
            # Шум/бессмысленное сообщение: просим пользователя сформулировать запрос
            msg.body("Пожалуйста, напишите, с чем вам нужна помощь, в одном-двух предложениях.")
            # Оставляем состояние "start" без изменений, диалог не инициируется
            return str(resp)
        elif category == "question":
            # Пользователь сразу задал вопрос -> переходим сразу к консультации
            user_states[sender_number] = "consultation_menu"
            # Создаем новую историю диалога с первым вопросом пользователя
            user_messages[sender_number] = [f"Пользователь: {incoming_msg}"]
            # Получаем краткий ответ от GPT и добавляем его в диалог
            answer = gpt_reply_short(incoming_msg)
            user_messages[sender_number].append(f"Бот: {answer}")
            # Сохраняем нормализованный вопрос для обнаружения повторных вопросов
            last_question_norm[sender_number] = normalize_text(incoming_msg)
            # Отправляем ответ с меню вариантов продолжения
            msg.body(answer + CONSULT_ENDING_MENU)
            return str(resp)
        else:
            # Приветствие или общее сообщение: показываем стандартное стартовое меню
            user_states[sender_number] = "awaiting_choice"
            user_messages[sender_number] = [f"Пользователь: {incoming_msg}"]
            msg.body(
                "Добрый день! Чем я могу помочь?\n"
                "1. Консультация\n"
                "2. Ремонт / Диагностика\n"
                "3. Помощь с программным обеспечением"
            )
            return str(resp)

    # Выбор ветки (ожидание выбора 1/2/3 после стартового меню)
    if state == "awaiting_choice":
        dialog.append(f"Пользователь: {incoming_msg}")
        if incoming_msg == "1":
            user_states[sender_number] = "consultation"  # переводим в режим консультации, ожидаем вопрос
            msg.body("Расскажите подробнее, с чем Вам необходимо помочь?")
        elif incoming_msg == "2":
            user_states[sender_number] = "repair"
            msg.body("Что у Вас случилось? Укажите тип оборудования (ПК/МФУ/телефон и т.п.) и проблему в одном сообщении.")
        elif incoming_msg == "3":
            user_states[sender_number] = "software"
            msg.body("Опишите, что необходимо настроить или установить.")
        else:
            msg.body("Пожалуйста, введите 1, 2 или 3.")
        return str(resp)

    # Консультация: получаем вопрос пользователя -> даём краткий ответ -> показываем меню 1/2
    if state == "consultation":
        dialog.append(f"Пользователь: {incoming_msg}")
        # Проверяем, не повторяет ли пользователь свой предыдущий вопрос
        norm_incoming = normalize_text(incoming_msg)
        repeated = (sender_number in last_question_norm and norm_incoming == last_question_norm[sender_number])
        if repeated:
            # Повторный вопрос: переформулируем предыдущий ответ более простым языком
            last_answer_text = get_last_bot_answer(dialog)
            if last_answer_text:
                answer = simplify_answer_text(last_answer_text)
            else:
                # На случай, если предыдущий ответ не найден
                answer = gpt_reply_short(incoming_msg)
        else:
            # Новый вопрос: получаем стандартный краткий ответ
            answer = gpt_reply_short(incoming_msg)
        # Добавляем ответ бота в диалог и обновляем запись о последнем вопросе
        dialog.append(f"Бот: {answer}")
        last_question_norm[sender_number] = norm_incoming
        # Показываем меню продолжения/завершения консультации
        user_states[sender_number] = "consultation_menu"
        msg.body(answer + CONSULT_ENDING_MENU)
        return str(resp)

    # Меню после консультации (ожидание ответа 1 или 2, либо новый вопрос от пользователя)
    if state == "consultation_menu":
        if incoming_msg == "1":
            dialog.append("Пользователь: Всё понятно, спасибо")
            final_reply = "Рад помочь! Если появятся вопросы — пишите."
            dialog.append(f"Бот: {final_reply}")
            msg.body(final_reply)
            # Сохраняем диалог в Google Sheets и сбрасываем состояние пользователя
            save_to_sheet(sender_number, dialog)
            user_states.pop(sender_number, None)
            user_messages.pop(sender_number, None)
            last_question_norm.pop(sender_number, None)
            return str(resp)
        elif incoming_msg == "2":
            dialog.append("Пользователь: Требуется дополнительная консультация")
            # Возвращаемся в состояние ожидания следующего вопроса от пользователя
            user_states[sender_number] = "consultation"
            msg.body("Пожалуйста, уточните ваш вопрос или опишите детали.")
            return str(resp)
        else:
            # Пользователь прислал новый или повторный вопрос вместо выбора 1/2
            dialog.append(f"Пользователь: {incoming_msg}")
            norm_incoming = normalize_text(incoming_msg)
            repeated = (sender_number in last_question_norm and norm_incoming == last_question_norm[sender_number])
            if repeated:
                # Повторный вопрос: упрощаем предыдущий ответ
                last_answer_text = get_last_bot_answer(dialog)
                if last_answer_text:
                    answer = simplify_answer_text(last_answer_text)
                else:
                    answer = gpt_reply_short(incoming_msg)
            else:
                # Новый дополнительный вопрос: запрашиваем ответ у GPT
                answer = gpt_reply_short(incoming_msg)
            dialog.append(f"Бот: {answer}")
            # Обновляем последний вопрос пользователя
            last_question_norm[sender_number] = norm_incoming
            # Оставляем пользователя в меню консультации для дальнейших действий
            msg.body(answer + CONSULT_ENDING_MENU)
            return str(resp)

    # Ремонт / Диагностика
    if state == "repair":
        dialog.append(f"Пользователь: {incoming_msg}")
        reply = "Понял. Передаю Вашу заявку в сервисный центр. Мы свяжемся с Вами в ближайшее время."
        dialog.append(f"Бот: {reply}")
        msg.body(reply)
        save_to_sheet(sender_number, dialog)
        user_states.pop(sender_number, None)
        user_messages.pop(sender_number, None)
        last_question_norm.pop(sender_number, None)
        return str(resp)

    # Программное обеспечение
    if state == "software":
        dialog.append(f"Пользователь: {incoming_msg}")
        reply = "Понял. Передаю Вашу заявку в сервисный центр. Мы уточним детали и поможем с установкой/настройкой."
        dialog.append(f"Бот: {reply}")
        msg.body(reply)
        save_to_sheet(sender_number, dialog)
        user_states.pop(sender_number, None)
        user_messages.pop(sender_number, None)
        last_question_norm.pop(sender_number, None)
        return str(resp)

    # Фолбэк на непредвиденные случаи
    msg.body("Произошла ошибка. Давайте начнём заново. Напишите любое сообщение.")
    # Сбрасываем состояние на начальное
    user_states.pop(sender_number, None)
    user_messages.pop(sender_number, None)
    last_question_norm.pop(sender_number, None)
    return str(resp)
