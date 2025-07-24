from flask import Flask, request
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

app = Flask(__name__)

# Подключение к Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)

# Подключение к таблице
spreadsheet = client.open_by_url("https://docs.google.com/spreadsheets/d/1UYpvOUFI_Ft2nJy8H0G8okse5H1DJcLYWpwn_6swiK0/edit?usp=sharing")
worksheet = spreadsheet.sheet1

def log_to_sheet(phone_number, message_text):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    next_row = len(worksheet.get_all_values()) + 1
    request_id = f"Z{next_row:04d}"
    worksheet.append_row([now, phone_number, message_text, request_id])

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    
    try:
        phone_number = data["entry"][0]["changes"][0]["value"]["messages"][0]["from"]
        message_text = data["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"]

        # Логируем в Google Sheets
        log_to_sheet(phone_number, message_text)

        # Ответ (если ты используешь библиотеку или API для отправки ответа — добавь здесь)
        print(f"Сообщение от {phone_number}: {message_text}")

    except Exception as e:
        print(f"Ошибка: {e}")

    return "ok", 200

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000)

