"""Приём вебхуков Jins на Flask.  pip install flask  ·  JINS_WEBHOOK_SECRET=whsec_... flask --app webhook_flask run"""
import os, sys
from flask import Flask, abort, request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sdk"))
from jins_client import verify_webhook

app = Flask(__name__)
SECRET = os.environ["JINS_WEBHOOK_SECRET"]
seen = set()  # в проде храните в Redis/БД: доставки могут повторяться


@app.post("/jins-hook")
def hook():
    raw = request.get_data()  # именно сырое тело — подпись считается по нему
    if not verify_webhook(SECRET, request.headers.get("X-Jins-Signature", ""), raw):
        abort(400)
    event = request.get_json()
    if event["id"] in seen:       # идемпотентность: событие могли прислать повторно
        return "", 204
    seen.add(event["id"])
    if event["type"] == "deal.closed":
        d = event["data"]
        print(f"Сделка {d['kind']} #{d['deal_id']} закрыта: {d['outcome']}, сумма {d['amount']}")
    elif event["type"] == "message.received":
        print("Новое сообщение:", event["data"]["text"])
    return "", 204                # любой 2xx = «принято»
