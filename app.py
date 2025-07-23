from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

@app.route("/whatsapp", methods=["POST"])
def whatsapp_reply():
    incoming_msg = request.form.get('Body')
    print(f"[WHATSAPP]: {incoming_msg}")

    resp = MessagingResponse()
    msg = resp.message()

    if "заявка" in incoming_msg.lower():
        msg.body("Спасибо! Ваша заявка получена. Мы свяжемся с вами.")
    else:
        msg.body("Здравствуйте! Напишите 'заявка', чтобы оставить обращение.")

    return str(resp)
