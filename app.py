import os
import json
from flask import Flask, request
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

app = Flask(__name__)

# Загрузка учётных данных из переменной окружения
google_credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
if not google_credentials_json:
    raise Exception("Переменная окружения GOOGLE_CREDENTIALS_JSON не установлена")

# Преобразуем строку JSON в словарь и сохраняем во временный файл
credentials_dict = json.loads(google_credentials_json)
with open("temp_credentials.json", "w") as f:
    json.dump(credentials_dict, f)

# Подключение к Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("temp_credentials.json", scope)
client = gspread.authorize(creds)

# Открытие таблицы по имени
sheet = client.open("whatsapp_bot_sheet").sheet1

@app.route("/webhook", methods=['POST'])
def whatsapp_reply():
    # Получаем сообщение и номер отправителя из запроса
    incoming_msg = request.values.get('Body', '').strip()
    sender_number = request.values.get('From', '').strip()

    # Получаем текущую дату и время
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Генерируем номер заявки (номер строки - 1)
    last_row = len(sheet.get_all_values())
    request_id = str(last_row)  # можно +1, если без заголовков

    # Добавляем запись в таблицу: Заявка № | Дата/время | Номер отправителя | Сообщение
    sheet.append_row([request_id, now, sender_number, incoming_msg])

    # Отправляем автоответ
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>Спасибо, Ваша заявка записана. Мы скоро с Вами свяжемся.</Message>
</Response>""", 200, {'Content-Type': 'application/xml'}

@app.route("/", methods=['GET'])
def home():
    return "WhatsApp Bot is running ✅"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

