from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import os

app = Flask(__name__)

@app.route("/", methods=["POST"])
def whatsapp_reply():
    incoming_msg = request.values.get('Body', '').strip().lower()
    resp = MessagingResponse()
    msg = resp.message()

    if "привет" in incoming_msg:
        msg.body("Привет! Чем могу помочь?")
    elif "заявка" in incoming_msg:
        msg.body("Пожалуйста, опишите вашу заявку подробнее.")
    else:
        msg.body("Спасибо за сообщение! Мы скоро с вами свяжемся.")

    return str(resp)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)

