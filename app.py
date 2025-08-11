from dotenv import load_dotenv
load_dotenv()

import os
import datetime
import gspread
import json
from flask import Flask, request
from openai import OpenAI
from google.oauth2 import service_account
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

# OpenAI
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Google Sheets
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

# Состояния и диалоги
user_states: dict[str, str] = {}
user_messages: dict[str, list[str]] = {}

def save_to_sheet(phone: str, dialog: list[str]):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conversation = "\n".join(dialog)
    sheet.append_row([timestamp, phone, conversation])

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

    # Стартовое меню
    if state == "start":
        user_states[sender_number] = "awaiting_choice"
        user_messages[sender_number] = [f"Пользователь: {incoming_msg}"]
        msg.body(
            "Добрый день! Чем я могу помочь?\n"
            "1. Консультация\n"
            "2. Ремонт / Диагностика\n"
            "3. Помощь с программным обеспечением"
        )
        return str(resp)

    # Выбор ветки
    if state == "awaiting_choice":
        dialog.append(f"Пользователь: {incoming_msg}")
        if incoming_msg == "1":
            user_states[sender_number] = "consultation"  # ожидаем вопрос
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

    # Консультация: получаем вопрос → даём краткий ответ → показываем меню 1/2
    if state == "consultation":
        dialog.append(f"Пользователь: {incoming_msg}")
        answer = gpt_reply_short(incoming_msg)
        dialog.append(f"Бот: {answer}")
        # показываем меню продолжения/завершения
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
            user_states.pop(sender_number, None)
            user_messages.pop(sender_number, None)
            return str(resp)
        elif incoming_msg == "2":
            dialog.append("Пользователь: Требуется дополнительная консультация")
            # Возвращаемся в состояние ожидания уточняющего вопроса
            user_states[sender_number] = "consultation"
            msg.body("Пожалуйста, уточните ваш вопрос или опишите детали.")
            return str(resp)
        else:
            msg.body("Пожалуйста, выберите 1 или 2.\n" + CONSULT_ENDING_MENU)
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
        return str(resp)

    # ПО
    if state == "software":
        dialog.append(f"Пользователь: {incoming_msg}")
        reply = "Понял. Передаю Вашу заявку в сервисный центр. Мы уточним детали и поможем с установкой/настройкой."
        dialog.append(f"Бот: {reply}")
        msg.body(reply)
        save_to_sheet(sender_number, dialog)
        user_states.pop(sender_number, None)
        user_messages.pop(sender_number, None)
        return str(resp)

    # Фолбэк
    msg.body("Произошла ошибка. Давайте начнём заново. Напишите любое сообщение.")
    user_states.pop(sender_number, None)
    user_messages.pop(sender_number, None)
    return str(resp)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
