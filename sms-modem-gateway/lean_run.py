"""HTTP entry point for the lean Home Assistant modem add-on."""
from __future__ import annotations

import atexit
import json
import logging
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request
from flask_httpauth import HTTPBasicAuth
from waitress import serve
from werkzeug.security import check_password_hash, generate_password_hash

from lean_engine import ModemEngine
from lean_store import MessageStore
from support import init_state_machine


VERSION = "0.1.0-dev.3"
OPTIONS_PATH = Path("/data/options.json")
DATABASE_PATH = Path("/data/messages.db")


def load_options() -> dict[str, Any]:
    defaults = {
        "device_path": "/dev/ttyUSB0",
        "modem_baud_rate": "115200",
        "pin": "",
        "username": "admin",
        "password": "password",
        "poll_interval": 10,
        "voice_call_enabled": False,
        "message_retention_days": 30,
    }
    if OPTIONS_PATH.exists():
        defaults.update(json.loads(OPTIONS_PATH.read_text(encoding="utf-8")))
    return defaults


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
options = load_options()
machine = init_state_machine(
    options.get("pin", ""),
    options["device_path"],
    str(options.get("modem_baud_rate", "115200")),
)
store = MessageStore(DATABASE_PATH)
engine = ModemEngine(
    machine,
    store,
    poll_interval=int(options.get("poll_interval", 10)),
    retention_days=int(options.get("message_retention_days", 30)),
)

app = Flask(__name__)
auth = HTTPBasicAuth()
username = str(options.get("username", "admin"))
password_hash = generate_password_hash(str(options.get("password", "password")))


@auth.verify_password
def verify_password(candidate: str, password: str) -> str | None:
    if candidate == username and check_password_hash(password_hash, password):
        return candidate
    return None


@app.get("/")
def index():
    return jsonify(
        name="SMS Modem Gateway",
        version=VERSION,
        api="/v1",
        durable_queue=True,
    )


@app.get("/health")
def health():
    return jsonify(status="ok", version=VERSION, last_error=engine.last_error)


@app.get("/v1/messages")
@auth.login_required
def messages():
    limit = request.args.get("limit", default=100, type=int)
    return jsonify(store.pending(limit or 100))


@app.post("/v1/messages/<message_id>/ack")
@auth.login_required
def acknowledge(message_id: str):
    if not store.acknowledge(message_id):
        return jsonify(error="message_not_found"), 404
    logging.info("Queue ACK completed: id=%s", message_id[:12])
    return jsonify(acknowledged=True, id=message_id)


@app.post("/v1/sms")
@auth.login_required
def send_sms():
    payload = request.get_json(silent=True) or {}
    number = str(payload.get("number") or payload.get("target") or "").strip()
    text = str(payload.get("text") or payload.get("message") or "")
    if not number or not text:
        return jsonify(error="number_and_text_are_required"), 400
    try:
        result = engine.send_sms(
            number,
            text,
            unicode=payload.get("unicode"),
            flash=bool(payload.get("flash", False)),
            smsc=payload.get("smsc"),
        )
    except Exception as error:
        logging.exception("SMS send failed")
        return jsonify(error="sms_send_failed", detail=str(error)), 503
    return jsonify(sent=True, references=result)


@app.get("/v1/status")
@auth.login_required
def status():
    try:
        return jsonify(engine.status())
    except Exception as error:
        return jsonify(online=False, error=str(error)), 503


@app.get("/v1/modem")
@auth.login_required
def modem():
    try:
        return jsonify(engine.modem_info())
    except Exception as error:
        return jsonify(error=str(error)), 503


@app.post("/v1/calls/dial")
@auth.login_required
def dial():
    if not options.get("voice_call_enabled", False):
        return jsonify(error="voice_calls_disabled"), 403
    payload = request.get_json(silent=True) or {}
    number = str(payload.get("number") or "").strip()
    if not number:
        return jsonify(error="number_is_required"), 400
    try:
        engine.dial(number)
    except Exception as error:
        return jsonify(error="dial_failed", detail=str(error)), 503
    return jsonify(dialing=True, number=number)


@app.post("/v1/calls/hangup")
@auth.login_required
def hangup():
    if not options.get("voice_call_enabled", False):
        return jsonify(error="voice_calls_disabled"), 403
    try:
        engine.hangup()
    except Exception as error:
        return jsonify(error="hangup_failed", detail=str(error)), 503
    return jsonify(hung_up=True)


@app.post("/v1/poll")
@auth.login_required
def poll_now():
    try:
        return jsonify(engine.poll_once())
    except Exception as error:
        logging.exception("Manual poll failed")
        return jsonify(error="poll_failed", detail=str(error)), 503


atexit.register(engine.stop)

if __name__ == "__main__":
    engine.start()
    logging.info("SMS Modem Gateway %s started", VERSION)
    logging.info("Device: %s", options["device_path"])
    logging.info("Durable queue: %s", DATABASE_PATH)
    logging.info("REST API ready on port 5000")
    serve(app, host="0.0.0.0", port=5000, threads=4, channel_timeout=90)
