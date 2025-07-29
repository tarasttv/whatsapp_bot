from dotenv import load_dotenv
load_dotenv()
import os
import openai
from openai import OpenAI
import datetime
import gspread
import json
from flask import Flask, request
from google.oauth2 import service_account  # <-- добавлено
from oauth2client.service_account import ServiceAccountCredentials
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

# Укажи свой OpenAI API-ключ
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Авторизация для Google Sheets
scope = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
print("Текущая директория:", os.getcwd())
print("Содержимое папки:", os.listdir())

# Загружаем creds.json и заменяем \\n на \n в ключе
creds_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
creds_dict = json.loads(creds_json)

# Преобразуем private_key, если нужно
if "\\n" in creds_dict["private_key"]:
    creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")

# Создаём credentials
creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=scope)
gsheets_client = gspread.authorize(creds)  # ✅
sheet = gsheets_client.open("whatsapp_bot_sheet").sheet1

# Хранилище состояний пользователей
user_states = {}
user_messages = {}

@app.route("/webhook", methods=["POST"])
def whatsapp_reply():
    incoming_msg = request.values.get("Body", "").strip()
    sender_number = request.values.get("From", "").replace("whatsapp:", "")
    resp = MessagingResponse()
    msg = resp.message()

    state = user_states.get(sender_number, "start")
    dialog = user_messages.get(sender_number, [])

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

    elif state == "awaiting_choice":
        dialog.append(f"Пользователь: {incoming_msg}")
        if incoming_msg == "1":
            user_states[sender_number] = "consultation"
            msg.body("Расскажите подробнее, с чем Вам необходимо помочь?")
        elif incoming_msg == "2":
            user_states[sender_number] = "repair"
            msg.body("Что у Вас случилось? Напишите вид оборудования и проблему.")
        elif incoming_msg == "3":
            user_states[sender_number] = "software"
            msg.body("Опишите, что необходимо настроить или установить.")
        else:
            msg.body("Пожалуйста, введите 1, 2 или 3.")
        return str(resp)

    elif state == "consultation":
        dialog.append(f"Пользователь: {incoming_msg}")
        # GPT-ответ
        response = openai_client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": "Привет, кто ты?"}],
            max_tokens=200
        )
        msg.body(response.choices[0].message.content.strip())
        dialog.append(f"Бот: {gpt_reply}")
        msg.body(gpt_reply)
        save_to_sheet(sender_number, dialog)
        user_states.pop(sender_number)
        user_messages.pop(sender_number)
        return str(resp)

    elif state == "repair":
        dialog.append(f"Пользователь: {incoming_msg}")
        reply = "Понятно, передаю Вашу заявку в сервисный центр."
        dialog.append(f"Бот: {reply}")
        msg.body(reply)
        save_to_sheet(sender_number, dialog)
        user_states.pop(sender_number)
        user_messages.pop(sender_number)
        return str(resp)

    elif state == "software":
        dialog.append(f"Пользователь: {incoming_msg}")
        reply = "Понятно, передаю Вашу заявку в сервисный центр."
        dialog.append(f"Бот: {reply}")
        msg.body(reply)
        save_to_sheet(sender_number, dialog)
        user_states.pop(sender_number)
        user_messages.pop(sender_number)
        return str(resp)

    else:
        msg.body("Произошла ошибка. Попробуйте снова.")
        user_states.pop(sender_number, None)
        user_messages.pop(sender_number, None)
        return str(resp)


def save_to_sheet(phone, dialog):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conversation = "\n".join(dialog)
    sheet.append_row([timestamp, phone, conversation])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

