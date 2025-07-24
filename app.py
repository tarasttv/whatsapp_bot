import os
import json
from flask import Flask, request
from oauth2client.service_account import ServiceAccountCredentials
import gspread
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv

# Загружаем переменные из .env (для локального запуска, Render это игнорирует)
load_dotenv()

app = Flask(__name__)

# Авторизация через переменную среды GOOGLE_CREDENTIALS_JSON
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
credentials_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")

if not credentials_json:
    raise Exception("Переменная окружения GOOGLE_CREDENTIALS_JSON не установлена")

credentials_dict = json.loads(credentials_json)
creds = ServiceAccountCredentials.from_json_keyfile_dict(credentials_dict, scope)
client = gspread.authorize(creds)

# Открываем таблицу по названию
sheet = client.open("whatsapp_bot_sheet").sheet1

@app.route("/webhook", methods=['POST'])
def whatsapp_reply():
    incoming_msg = request.values.get('Body', '').strip()
    sender_number = request.values.get('From', '')

    print(f"Сообщение от {sender_number}: {incoming_msg}")

    # Сохраняем сообщение в Google Sheets
    sheet.append_row([sender_number, incoming_msg])

    # Формируем ответ
    resp = MessagingResponse()
    reply = resp.message("Спасибо! Ваше сообщение записано.")

    return str(resp)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

