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

import requests  # >>> NEW: для Telegram
import gspread
from gspread.exceptions import APIError
from flask import Flask, request
from openai import OpenAI
from google.oauth2 import service_account
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

# ===============================
# ENV / Secrets
# ===============================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")  # >>> NEW
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")    # >>> NEW

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
        return None

# ===============================
# Состояния пользователей и хранилища
# ===============================
user_states: Dict[str, str] = {}
user_messages: Dict[str, List[str]] = {}
last_question_norm: Dict[str, str] = {}
repeat_count: Dict[str, int] = {}

# >>> NEW: метаданные (имя/источник)
user_meta: Dict[str, Dict[str, str]] = {}  # {phone: {"name":..., "source":...}}

# ===============================
# Очередь на запись в Google Sheets
# ===============================
pending_rows: List[List[str]] = []
pending_lock = threading.Lock()

def save_to_sheet(phone: str, dialog: List[str]) -> None:
    """
    Кладёт строку диалога в очередь для последующей пакетной записи в Google Sheets.
    Формат: timestamp | phone | profile_name | source | conversation
    """
    meta = user_meta.get(phone, {})
    profile_name = meta.get("name","")
    source = meta.get("source","")
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conversation = "\n".join(dialog)
    row = [timestamp, phone, profile_name, source, conversation]
    with pending_lock:
        pending_rows.append(row)

def flush_worker() -> None:
    """
    Фоновая задача: раз в 10 секунд или при накоплении >=3 записей
    отправляет их батчем в Google Sheets.
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
            batch = pending_rows[:]
            pending_rows.clear()

        success = False
        give_up = False
        backoff = 1
        for attempt in range(5):
            try:
                sheet = get_sheet()
                if sheet is None:
                    with pending_lock:
                        pending_rows[0:0] = batch
                    time.sleep(5)
                    break
                sheet.append_rows(batch, value_input_option='RAW')
                success = True
                last_flush_time = time.time()
                break
            except APIError as e:
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
                    time.sleep(backoff); backoff = min(backoff * 2, 60)
                    continue
                else:
                    print(f"[Sheets] Критическая ошибка, не ретраим: {e}")
                    give_up = True
                    break
            except Exception as e:
                print(f"[Sheets] Неожиданная ошибка: {e}")
                time.sleep(backoff); backoff = min(backoff * 2, 60)
                continue

        if not success:
            if give_up:
                print("[Sheets] Пакет записей пропущен из-за критической ошибки.")
            else:
                with pending_lock:
                    pending_rows[0:0] = batch

# Старт фонового потока
threading.Thread(target=flush_worker, daemon=True).start()

# ===============================
# Вспомогательные функции
# ===============================
def tg_notify(text: str) -> None:
    """Отправка уведомления в Telegram (если заданы токен/чат)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode":"HTML"},
            timeout=6
        )
    except Exception as e:
        print(f"[TG] notify error: {e}")

def normalize_text(text: str) -> str:
    return "".join(ch.lower() for ch in text if ch.isalnum())

def classify_message(text: str) -> str:
    t = text.strip()
    if t == "": return "noise"
    if sum(ch.isalnum() for ch in t) == 0: return "noise"
    words = t.lower().split()
    greetings = ["привет","здравствуйте","добрый день","доброе утро","добрый вечер","здравствуй","hi","hello"]
    question_words = ["что","как","почему","зачем","когда","где","какой","какая","какие"]
    if len(words) >= 4 and ("?" in t or any(qw in t for qw in question_words)):
        return "question"
    for greet in greetings:
        if greet in t: return "greeting"
    return "greeting"

def gpt_reply_short(user_text: str) -> str:
    resp = openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role":"system","content":"Ты технический консультант. Отвечай максимально кратко, простым языком. До 400 символов."},
            {"role":"user","content":user_text}
        ],
        max_tokens=220, temperature=0.2
    )
    text = (resp.choices[0].message.content or "").strip()
    return text[:400] + ("…" if len(text) > 400 else "")

def alternative_solution(user_question: str, previous_answer: str) -> str:
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
            {"role":"system","content":"Ты инженер-консультант. Давай практичные варианты решения в разных стилях."},
            {"role":"user","content":prompt}
        ],
        max_tokens=450, temperature=0.7, presence_penalty=0.6, frequency_penalty=0.4
    )
    return (resp.choices[0].message.content or "").strip()

def followup_question(user_question: str, previous_answer: str) -> str:
    resp = openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role":"system","content":"Сформулируй один уточняющий вопрос по сути проблемы, без лишних слов."},
            {"role":"user","content":f"Вопрос пользователя: {user_question}\nПредыдущий ответ: {previous_answer}\nСпроси один уточняющий вопрос."}
        ],
        max_tokens=60, temperature=0.2
    )
    return (resp.choices[0].message.content or "").strip()

def get_last_bot_answer(dialog: List[str]) -> str:
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
    "2 — Ещё вопрос\n"
    "3 — Связаться с инженером"  # >>> NEW
)

# ===============================
# Healthcheck
# ===============================
@app.route("/health", methods=["GET", "POST"])
def health():
    return ("ok", 200)

# ===============================
# Основной вебхук WhatsApp (Twilio)
# ===============================
@app.route("/webhook", methods=["POST"])
def whatsapp_reply():
    incoming_msg = (request.values.get("Body", "") or "").strip()
    sender_number = (request.values.get("From", "") or "").replace("whatsapp:", "")
    profile_name = (request.values.get("ProfileName","") or "").strip()  # >>> NEW
    resp = MessagingResponse()
    msg = resp.message()

    # >>> NEW: сохранить имя и источник
    if sender_number not in user_meta:
        user_meta[sender_number] = {}
    if profile_name and not user_meta[sender_number].get("name"):
        user_meta[sender_number]["name"] = profile_name
    m_src = re.search(r"\[SRC:([A-Za-z0-9_\-\.]+)\]", incoming_msg)
    if m_src:
        user_meta[sender_number]["source"] = m_src.group(1)

    state = user_states.get(sender_number, "start")
    dialog = user_messages.get(sender_number, [])

    # START
    if state == "start":
        category = classify_message(incoming_msg)
        if category == "noise":
            msg.body("Пожалуйста, напишите, с чем вам нужна помощь, в одном-двух предложениях.")
            return str(resp)
        elif category == "question":
            user_states[sender_number] = "consultation_menu"
            user_messages[sender_number] = [f"Пользователь: {incoming_msg}"]
            answer = gpt_reply_short(incoming_msg)
            user_messages[sender_number].append(f"Бот: {answer}")
            last_question_norm[sender_number] = normalize_text(incoming_msg)
            repeat_count[sender_number] = 0
            msg.body(answer + CONSULT_ENDING_MENU)
            return str(resp)
        else:
            user_states[sender_number] = "awaiting_choice"
            user_messages[sender_number] = [f"Пользователь: {incoming_msg}"]
            msg.body(
                "Добрый день! Чем я могу помочь?\n"
                "1. Консультация\n"
                "2. Ремонт / Диагностика\n"
                "3. Связаться с инженером"  # >>> NEW
            )
            return str(resp)

    # Меню выбора
    if state == "awaiting_choice":
        dialog.append(f"Пользователь: {incoming_msg}")
        if incoming_msg == "1":
            user_states[sender_number] = "consultation"
            msg.body("Расскажите подробнее, с чем Вам необходимо помочь?")
            return str(resp)
        elif incoming_msg == "2":
            user_states[sender_number] = "repair"
            msg.body("Что у Вас случилось? Укажите тип оборудования (ПК/МФУ/телефон и т.п.) и проблему в одном сообщении.")
            return str(resp)
        elif incoming_msg == "3":  # >>> NEW
            user_states[sender_number] = "contact_engineer"
            msg.body("Как с вами удобнее связаться и когда? Напишите время (сегодня/завтра, интервал) и кратко суть вопроса.")
            return str(resp)
        else:
            msg.body("Пожалуйста, введите 1, 2 или 3.")
            return str(resp)

    # Консультация
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

    # Меню после консультации
    if state == "consultation_menu":
        if incoming_msg == "1":
            dialog.append("Пользователь: Всё понятно, спасибо")
            final = "Рад помочь! Если появятся вопросы — пишите."
            dialog.append(f"Бот: {final}")
            msg.body(final)
            save_to_sheet(sender_number, dialog)
            # очистка
            user_states.pop(sender_number, None)
            user_messages.pop(sender_number, None)
            last_question_norm.pop(sender_number, None)
            repeat_count.pop(sender_number, None)
            user_meta.pop(sender_number, None)
            return str(resp)
        elif incoming_msg == "2":
            dialog.append("Пользователь: Ещё вопрос")
            user_states[sender_number] = "consultation"
            msg.body("Пожалуйста, уточните ваш вопрос или опишите детали.")
            return str(resp)
        elif incoming_msg == "3":  # >>> NEW
            dialog.append("Пользователь: Связаться с инженером")
            user_states[sender_number] = "contact_engineer"
            msg.body("Когда удобно созвониться и по какому вопросу? Напишите кратко.")
            return str(resp)
        else:
            # прислан новый/повторный вопрос
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

    # >>> NEW: контакт с инженером
    if state == "contact_engineer":
        dialog.append(f"Пользователь: {incoming_msg}")
        meta = user_meta.get(sender_number, {})
        lead_text = (
            f"🔔 <b>Запрос на связь с инженером</b>\n"
            f"Имя: {meta.get('name','—')}\n"
            f"Телефон: {sender_number}\n"
            f"Источник: {meta.get('source','—')}\n"
            f"Детали: {incoming_msg}"
        )
        tg_notify(lead_text)
        reply = "Спасибо! Передали инженеру. Мы свяжемся с вами в ближайшее время."
        dialog.append(f"Бот: {reply}")
        msg.body(reply)
        save_to_sheet(sender_number, dialog)
        # очистка
        user_states.pop(sender_number, None)
        user_messages.pop(sender_number, None)
        last_question_norm.pop(sender_number, None)
        repeat_count.pop(sender_number, None)
        user_meta.pop(sender_number, None)
        return str(resp)

    # Ремонт / Диагностика
    if state == "repair":
        dialog.append(f"Пользователь: {incoming_msg}")
        meta = user_meta.get(sender_number, {})
        tg_notify(
            f"🧰 <b>Новая заявка (ремонт)</b>\n"
            f"Имя: {meta.get('name','—')}\n"
            f"Телефон: {sender_number}\n"
            f"Источник: {meta.get('source','—')}\n"
            f"Детали: {incoming_msg}"
        )
        reply = "Понял. Передаю Вашу заявку в сервисный центр. Мы свяжемся с Вами в ближайшее время."
        dialog.append(f"Бот: {reply}")
        msg.body(reply)
        save_to_sheet(sender_number, dialog)
        user_states.pop(sender_number, None)
        user_messages.pop(sender_number, None)
        last_question_norm.pop(sender_number, None)
        repeat_count.pop(sender_number, None)
        user_meta.pop(sender_number, None)
        return str(resp)

    # Программное обеспечение
    if state == "software":
        dialog.append(f"Пользователь: {incoming_msg}")
        meta = user_meta.get(sender_number, {})
        tg_notify(
            f"💽 <b>Новая заявка (ПО)</b>\n"
            f"Имя: {meta.get('name','—')}\n"
            f"Телефон: {sender_number}\n"
            f"Источник: {meta.get('source','—')}\n"
            f"Детали: {incoming_msg}"
        )
        reply = "Понял. Передаю Вашу заявку в сервисный центр. Мы уточним детали и поможем с установкой/настройкой."
        dialog.append(f"Бот: {reply}")
        msg.body(reply)
        save_to_sheet(sender_number, dialog)
        user_states.pop(sender_number, None)
        user_messages.pop(sender_number, None)
        last_question_norm.pop(sender_number, None)
        repeat_count.pop(sender_number, None)
        user_meta.pop(sender_number, None)
        return str(resp)

    # Фолбэк
    msg.body("Произошла ошибка. Давайте начнём заново. Напишите любое сообщение.")
    user_states.pop(sender_number, None)
    user_messages.pop(sender_number, None)
    last_question_norm.pop(sender_number, None)
    repeat_count.pop(sender_number, None)
    user_meta.pop(sender_number, None)
    return str(resp)

# ===============================
# Локальный запуск (dev)
# ===============================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
