from dotenv import load_dotenv
load_dotenv()

import os
import datetime
import json
import threading
import time
import re
import traceback
from typing import Dict, List, Optional

import gspread
from gspread.exceptions import APIError
from flask import Flask, request
from openai import OpenAI
from google.oauth2 import service_account
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

# ===============================
# OpenAI client
# ===============================
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ===============================
# Google Sheets (ленивая инициализация)
# ===============================
GSHEETS_READY = False
_gsheets_client: Optional[gspread.Client] = None
_sheet: Optional[gspread.Worksheet] = None
_gsheets_error: Optional[str] = None

def get_sheet() -> Optional[gspread.Worksheet]:
    """
    Ленивая инициализация клиента и листа.
    Возвращает worksheet или None, если инициализация не удалась.
    """
    global GSHEETS_READY, _gsheets_client, _sheet, _gsheets_error
    if GSHEETS_READY and _sheet is not None:
        return _sheet
    try:
        creds_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if not creds_json:
            raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS is empty")
        creds_dict = json.loads(creds_json)
        if "\\n" in creds_dict.get("private_key", ""):
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")

        scope = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=scope)
        _gsheets_client = gspread.authorize(creds)
        _sheet = _gsheets_client.open("whatsapp_bot_sheet").sheet1
        GSHEETS_READY = True
        _gsheets_error = None
        return _sheet
    except Exception as e:
        _gsheets_error = f"{e}\n{traceback.format_exc()}"
        # Не падаем — вернём None; запись попробуем позже/повторно
        return None

# ===============================
# Состояния пользователей и хранилища
# ===============================
user_states: Dict[str, str] = {}
user_messages: Dict[str, List[str]] = {}

# Последний нормализованный вопрос пользователя (для детекта повторов)
last_question_norm: Dict[str, str] = {}
# Счётчик повторов одинакового вопроса
repeat_count: Dict[str, int] = {}

# ===============================
# Очередь на запись в Google Sheets
# ===============================
pending_rows: List[List[str]] = []
pending_lock = threading.Lock()

def save_to_sheet(phone: str, dialog: List[str]) -> None:
    """
    Кладёт строку диалога в очередь для последующей пакетной записи в Google Sheets.
    """
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conversation = "\n".join(dialog)
    row = [timestamp, phone, conversation]
    with pending_lock:
        pending_rows.append(row)

def flush_worker() -> None:
    """
    Фоновая задача: раз в 10 секунд или при накоплении >=3 записей
    отправляет их батчем в Google Sheets.
    Обрабатывает временные ошибки (429/5xx) экспоненциальным бэкоффом.
    """
    last_flush_time = time.time()
    while True:
        time.sleep(1)
        with pending_lock:
            count = len(pending_rows)
            if count == 0:
                continue
            now = time.time()
            if count < 3 and (now - last_flush_time < 10):
                continue
            # забираем пакет
            batch = pending_rows[:]
            pending_rows.clear()

        # пробуем отправить
        success = False
        give_up = False
        backoff = 1
        for attempt in range(5):
            try:
                sheet = get_sheet()
                if sheet is None:
                    # Нет соединения с Sheets — вернём записи назад и подождём
                    with pending_lock:
                        pending_rows[0:0] = batch
                    time.sleep(5)
                    break
                sheet.append_rows(batch, value_input_option='RAW')
                success = True
                last_flush_time = time.time()
                break
            except APIError as e:
                # Попробуем извлечь код ошибки
                error_code = None
                if hasattr(e, 'response') and e.response is not None:
                    try:
                        err_json = e.response.json()
                        if isinstance(err_json, dict):
                            error_code = err_json.get('error', {}).get('code')
                    except Exception:
                        if hasattr(e.response, 'status_code'):
                            error_code = e.response.status_code
                if error_code is None:
                    m = re.search(r'"code":\s*(\d+)', str(e))
                    if m:
                        error_code = int(m.group(1))

                if error_code == 429 or (isinstance(error_code, int) and 500 <= error_code < 600):
                    print(f"[Sheets] Временная ошибка {error_code}, попытка {attempt+1}/5; повтор через {backoff}с")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                else:
                    print(f"[Sheets] Критическая ошибка, не ретраим: {e}")
                    give_up = True
                    break
            except Exception as e:
                print(f"[Sheets] Неожиданная ошибка: {e}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

        if not success:
            if give_up:
                print("[Sheets] Пакет записей пропущен из-за критической ошибки.")
            else:
                # Вернуть в очередь для повторной отправки позже
                with pending_lock:
                    pending_rows[0:0] = batch

# Старт фонового потока
threading.Thread(target=flush_worker, daemon=True).start()

# ===============================
# Вспомогательные функции Диалогов/НЛП
# ===============================
def normalize_text(text: str) -> str:
    """
    Нормализация: удалить всё, кроме букв/цифр, привести к нижнему регистру.
    Это даёт стабильное сравнение «одинаковых» вопросов.
    """
    return "".join(ch.lower() for ch in text if ch.isalnum())

def classify_message(text: str) -> str:
    """
    Классификация стартового сообщения: 'greeting' | 'question' | 'noise'
    - noise: нет букв/цифр или пусто
    - question: >=4 слов и вопросительная форма (вопрос. слово или '?')
    - greeting: типичные приветствия
    """
    t = text.strip()
    if t == "":
        return "noise"
    if sum(ch.isalnum() for ch in t) == 0:
        return "noise"

    words = t.lower().split()
    word_count = len(words)

    greetings = ["привет", "здравствуйте", "добрый день", "доброе утро", "добрый вечер", "здравствуй", "hi", "hello"]
    question_words = ["что", "как", "почему", "зачем", "когда", "где", "какой", "какая", "какие"]

    if word_count >= 4 and ("?" in t or any(qw in t.lower() for qw in question_words)):
        return "question"
    for greet in greetings:
        if greet in t.lower():
            return "greeting"
    return "greeting"

def gpt_reply_short(user_text: str) -> str:
    """
    Краткий, понятный ответ (до ~400 символов).
    """
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
        max_tokens=220,
        temperature=0.2
    )
    text = resp.choices[0].message.content.strip()
    if len(text) > 400:
        text = text[:400].rstrip() + "…"
    return text

def alternative_solution(user_question: str, previous_answer: str) -> str:
    """
    Генерирует ИНОЙ подход/стратегию (не повторять предыдущий ответ).
    Даст краткий план, когда применять, риски/минусы.
    """
    prompt = (
        "Пользователь задал вопрос:\n"
        f"{user_question}\n\n"
        "Ты уже предложил один способ (не повторяй его):\n"
        f"{previous_answer}\n\n"
        "Теперь предложи ДРУГОЙ подход/стратегию решения. Обязательно:\n"
        "1) Краткий план шагов (3–6 шагов)\n"
        "2) Когда этот подход лучше применять\n"
        "3) Возможные риски/минусы\n"
        "Избегай повторения предыдущих шагов и формулировок. До 700 символов."
    )
    resp = openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "Ты инженер-консультант. Давай практичные варианты решения в разных стилях."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=450,
        temperature=0.7,
        presence_penalty=0.6,
        frequency_penalty=0.4
    )
    return resp.choices[0].message.content.strip()

def followup_question(user_question: str, previous_answer: str) -> str:
    """
    Один короткий уточняющий вопрос, чтобы выбрать другой путь.
    """
    resp = openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "Сформулируй один уточняющий вопрос по сути проблемы, без лишних слов."},
            {"role": "user", "content": f"Вопрос пользователя: {user_question}\nПредыдущий ответ: {previous_answer}\nСпроси один уточняющий вопрос."}
        ],
        max_tokens=60,
        temperature=0.2
    )
    return resp.choices[0].message.content.strip()

def get_last_bot_answer(dialog: List[str]) -> str:
    """
    Возвращает текст последнего ответа бота из истории диалога (если есть).
    """
    for entry in reversed(dialog):
        if entry.startswith("Бот:"):
            return entry[5:].strip()
    return ""

# ===============================
# UI-тексты
# ===============================
CONSULT_ENDING_MENU = (
    "\n\nВыберите:\n"
    "1 — Всё понятно, спасибо\n"
    "2 — Требуется дополнительная консультация"
)

# ===============================
# Healthcheck
# ===============================
@app.route("/health", methods=["GET", "POST"])
def health():
    # Минимальный ответ (Twilio и Railway довольно нетребовательны)
    return ("ok", 200)

# ===============================
# Основной вебхук WhatsApp
# ===============================
@app.route("/webhook", methods=["POST"])
def whatsapp_reply():
    incoming_msg = request.values.get("Body", "").strip()
    sender_number = request.values.get("From", "").replace("whatsapp:", "")
    resp = MessagingResponse()
    msg = resp.message()

    state = user_states.get(sender_number, "start")
    dialog = user_messages.get(sender_number, [])

    # START: классифицируем первое сообщение
    if state == "start":
        category = classify_message(incoming_msg)
        if category == "noise":
            msg.body("Пожалуйста, напишите, с чем вам нужна помощь, в одном-двух предложениях.")
            return str(resp)
        elif category == "question":
            # Сразу консультация: краткий ответ и меню 1/2
            user_states[sender_number] = "consultation_menu"
            user_messages[sender_number] = [f"Пользователь: {incoming_msg}"]
            answer = gpt_reply_short(incoming_msg)
            user_messages[sender_number].append(f"Бот: {answer}")
            # Инициализируем контроль повторов
            last_question_norm[sender_number] = normalize_text(incoming_msg)
            repeat_count[sender_number] = 0
            msg.body(answer + CONSULT_ENDING_MENU)
            return str(resp)
        else:
            # Приветствие: показываем меню выбора
            user_states[sender_number] = "awaiting_choice"
            user_messages[sender_number] = [f"Пользователь: {incoming_msg}"]
            msg.body(
                "Добрый день! Чем я могу помочь?\n"
                "1. Консультация\n"
                "2. Ремонт / Диагностика\n"
                "3. Помощь с программным обеспечением"
            )
            return str(resp)

    # Меню выбора 1/2/3
    if state == "awaiting_choice":
        dialog.append(f"Пользователь: {incoming_msg}")
        if incoming_msg == "1":
            user_states[sender_number] = "consultation"
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

    # Консультация: первый или следующий вопрос
    if state == "consultation":
        dialog.append(f"Пользователь: {incoming_msg}")
        norm_incoming = normalize_text(incoming_msg)
        is_repeat = (sender_number in last_question_norm and norm_incoming == last_question_norm[sender_number])

        cnt = repeat_count.get(sender_number, 0)
        if is_repeat:
            cnt += 1
            repeat_count[sender_number] = cnt
        else:
            repeat_count[sender_number] = 0

        if is_repeat and cnt >= 1:
            last_ans = get_last_bot_answer(dialog) or ""
            answer = alternative_solution(incoming_msg, last_ans)
        else:
            answer = gpt_reply_short(incoming_msg)

        dialog.append(f"Бот: {answer}")
        last_question_norm[sender_number] = norm_incoming
        user_states[sender_number] = "consultation_menu"
        msg.body(answer + CONSULT_ENDING_MENU)
        return str(resp)

    # Меню после консультации (1 — завершить, 2 — ещё вопрос, либо прислали новый/повторный вопрос)
    if state == "consultation_menu":
        # Завершение
        if incoming_msg == "1":
            dialog.append("Пользователь: Всё понятно, спасибо")
            final = "Рад помочь! Если появятся вопросы — пишите."
            dialog.append(f"Бот: {final}")
            msg.body(final)
            save_to_sheet(sender_number, dialog)
            # очистка состояний
            user_states.pop(sender_number, None)
            user_messages.pop(sender_number, None)
            last_question_norm.pop(sender_number, None)
            repeat_count.pop(sender_number, None)
            return str(resp)
        # Доп. консультация
        elif incoming_msg == "2":
            dialog.append("Пользователь: Требуется дополнительная консультация")
            user_states[sender_number] = "consultation"
            msg.body("Пожалуйста, уточните ваш вопрос или опишите детали.")
            return str(resp)
        # Прислан новый/повторный вопрос вместо выбора 1/2
        else:
            dialog.append(f"Пользователь: {incoming_msg}")
            norm_incoming = normalize_text(incoming_msg)
            is_repeat = (sender_number in last_question_norm and norm_incoming == last_question_norm[sender_number])

            cnt = repeat_count.get(sender_number, 0)
            if is_repeat:
                cnt += 1
                repeat_count[sender_number] = cnt
            else:
                repeat_count[sender_number] = 0

            if is_repeat and cnt >= 1:
                last_ans = get_last_bot_answer(dialog) or ""
                alt = alternative_solution(incoming_msg, last_ans)
                if cnt >= 2:
                    q = followup_question(incoming_msg, last_ans)
                    answer = f"{alt}\n\nУточните, пожалуйста: {q}"
                else:
                    answer = alt
            else:
                answer = gpt_reply_short(incoming_msg)

            dialog.append(f"Бот: {answer}")
            last_question_norm[sender_number] = norm_incoming
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
        repeat_count.pop(sender_number, None)
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
        repeat_count.pop(sender_number, None)
        return str(resp)

    # Фолбэк
    msg.body("Произошла ошибка. Давайте начнём заново. Напишите любое сообщение.")
    user_states.pop(sender_number, None)
    user_messages.pop(sender_number, None)
    last_question_norm.pop(sender_number, None)
    repeat_count.pop(sender_number, None)
    return str(resp)

# ===============================
# Локальный запуск (dev)
# ===============================
if __name__ == "__main__":
    # Для локальных тестов: python app.py
    port = int(os.environ.get("PORT", 5000))
    # В продакшене на Railway запускайте через gunicorn:
    # gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 120
    app.run(host="0.0.0.0", port=port, debug=True)
