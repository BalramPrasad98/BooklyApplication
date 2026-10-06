"""Bookly support agent - run `python app.py` and open http://localhost:5050"""
import os
import threading
import webbrowser
from pathlib import Path

HERE = Path(__file__).parent

# Minimal .env loader (so no extra dependency is needed)
env_file = HERE / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            k = k.strip()
            # Treat an empty existing value (e.g. a blank var exported globally) the
            # same as "unset" so a real key in .env isn't silently shadowed by it.
            if not os.environ.get(k):
                os.environ[k] = v.strip().strip('"').strip("'")

from flask import Flask, jsonify, request, send_from_directory  # noqa: E402

import data  # noqa: E402
import nlu  # noqa: E402
from agents import UnifiedAgent  # noqa: E402
from data import ORDERS  # noqa: E402

app = Flask(__name__, static_folder=str(HERE / "static"))
SESSIONS = {}  # session_id -> UnifiedAgent instance (in-memory session state)
PORT = int(os.environ.get("PORT", 5050))

LOGGED_IN_USER = {"name": "Balram", "email": "balram@customer.com"}


@app.before_request
def _keep_demo_dates_current():
    # If the server has been left running across a day boundary, roll every
    # order's dates forward so "N days ago" offsets (and the return window)
    # stay correct without needing a restart.
    data.refresh_dates()


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/info")
def info():
    return jsonify({
        "mode": f"Claude ({nlu.MODEL})" if nlu.llm_enabled() else "Mock mode (rules only, no API key)",
        "user": LOGGED_IN_USER,
        "orders": sorted(
            [{"order_id": oid, "email": o["email"], "status": o["status"], "placed_on": o["placed_on"],
              "shipped_on": o["shipped_on"], "delivered_on": o["delivered_on"], "eta": o["eta"],
              "carrier": o["carrier"], "tracking": o["tracking"], "mailing_address": o["mailing_address"],
              "items": ", ".join(i["title"] for i in o["items"])} for oid, o in ORDERS.items()],
            key=lambda o: o["placed_on"]),
    })


@app.post("/api/chat")
def chat():
    body = request.get_json(force=True)
    sid, msg = body.get("session_id"), (body.get("message") or "").strip()
    if not sid or not msg:
        return jsonify({"error": "bad request"}), 400
    agent = SESSIONS.setdefault(sid, UnifiedAgent())
    reply, trace = agent.handle(msg)
    return jsonify({"reply": reply, "trace": trace.events})


@app.delete("/api/session/<sid>")
def delete_session(sid):
    # Chat history itself lives in the browser (localStorage); this just frees the matching
    # in-memory agent state so deleted chats don't linger server-side.
    SESSIONS.pop(sid, None)
    return jsonify({"ok": True})


if __name__ == "__main__":
    url = f"http://localhost:{PORT}"
    print(f"\n  Bookly support agent running at {url}  (Ctrl+C to stop)\n")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(port=PORT, debug=False)
