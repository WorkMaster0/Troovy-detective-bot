#!/usr/bin/env python3
import os
import asyncio
import time
import logging
from datetime import datetime, timezone
from threading import Thread
from typing import Dict, List
from flask import Flask, request
import requests

import ccxt.pro as ccxtpro
import ccxt

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # https://your-app.onrender.com/webhook
CHAT_ID = os.getenv("CHAT_ID")
PORT = int(os.getenv("PORT", "10000"))

# exchanges
PAIRS = {
    "bybit_mexc": ["bybit", "mexc"],
    "mexc_lbank": ["mexc", "lbank"],
}

SPREAD_MIN_PCT = 2.0
SPREAD_MAX_PCT = 100.0
SPREAD_MIN_ABS = 0.0001
ALERT_COOLDOWN = 60

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("spread-bot")

# ---------------- Flask webhook ----------------
app = Flask(__name__)

# active monitor tasks
active_tasks: Dict[str, asyncio.Task] = {}

def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "message" not in data:
        return "ok", 200
    text = data["message"].get("text", "").strip().lower()
    chat_id = data["message"]["chat"]["id"]

    # Commands
    if text.startswith("/start"):
        send_telegram("🤖 Bot online. Use /bybit_mexc or /mexc_lbank to start scanning.")
    elif text.startswith("/bybit_mexc"):
        start_monitor("bybit_mexc")
    elif text.startswith("/mexc_lbank"):
        start_monitor("mexc_lbank")
    elif text.startswith("/stop"):
        stop_all()
        send_telegram("🛑 All scans stopped.")
    else:
        send_telegram("❓ Unknown command.")

    return "ok", 200

# ---------------- Spread Monitor ----------------
latest_quote = {}
last_alert_ts = {}

async def create_client(ex_id):
    kwargs = {"enableRateLimit": True}
    try:
        client = getattr(ccxtpro, ex_id)(kwargs)
        client.options["defaultType"] = "swap"
        await asyncio.sleep(0)
        return client
    except Exception as e:
        logger.error("Client init failed for %s: %s", ex_id, e)
        return None

async def watch_pair(ex1, ex2):
    logger.info(f"Starting watcher {ex1} vs {ex2}")
    clients = {}
    for ex in [ex1, ex2]:
        clients[ex] = await create_client(ex)
        latest_quote[ex] = {}

    symbols = []
    try:
        m1 = getattr(ccxt, ex1)().load_markets()
        m2 = getattr(ccxt, ex2)().load_markets()
        common = set(m1).intersection(set(m2))
        symbols = [s for s in common if "USDT" in s and ":USDT" in s]
    except Exception as e:
        logger.error("Market load error: %s", e)

    async def watch_exchange(ex):
        while True:
            for s in symbols[:50]:
                try:
                    ob = await clients[ex].watch_order_book(s)
                    bid, ask = ob["bids"][0][0], ob["asks"][0][0]
                    latest_quote[ex][s] = {"bid": bid, "ask": ask, "ts": time.time()}
                    await check_spread(s, ex1, ex2)
                except Exception as e:
                    await asyncio.sleep(0.1)
            await asyncio.sleep(0.01)

    await asyncio.gather(watch_exchange(ex1), watch_exchange(ex2))

async def check_spread(symbol, ex1, ex2):
    if symbol not in latest_quote[ex1] or symbol not in latest_quote[ex2]:
        return
    b1 = latest_quote[ex1][symbol]
    b2 = latest_quote[ex2][symbol]
    # two sides
    for (buy_ex, buy_ask), (sell_ex, sell_bid) in [
        ((ex1, b1["ask"]), (ex2, b2["bid"])),
        ((ex2, b2["ask"]), (ex1, b1["bid"]))
    ]:
        absdiff = sell_bid - buy_ask
        if absdiff <= 0:
            continue
        pct = (absdiff / buy_ask) * 100
        if pct < SPREAD_MIN_PCT or pct > SPREAD_MAX_PCT:
            continue
        key = (symbol, buy_ex, sell_ex)
        now = time.time()
        if now - last_alert_ts.get(key, 0) < ALERT_COOLDOWN:
            continue
        last_alert_ts[key] = now
        t = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        msg = (
            f"🔔 *Spread {ex1.upper()} ↔ {ex2.upper()}*\n"
            f"Symbol: `{symbol}`\n"
            f"Buy @ {buy_ex}: `{buy_ask:.6f}`\n"
            f"Sell @ {sell_ex}: `{sell_bid:.6f}`\n"
            f"Spread: *{pct:.2f}%* (`{absdiff:.6f}`)\n"
            f"Time: {t}"
        )
        send_telegram(msg)

# ---------------- Start/Stop Control ----------------
def start_monitor(pair_name):
    if pair_name in active_tasks:
        send_telegram(f"⚙️ Already running {pair_name}")
        return
    ex1, ex2 = PAIRS[pair_name]
    global loop
    task = asyncio.run_coroutine_threadsafe(watch_pair(ex1, ex2), loop)
    active_tasks[pair_name] = task
    send_telegram(f"✅ Started monitoring {pair_name.upper()} (futures spread)")

def stop_all():
    for name, t in list(active_tasks.items()):
        t.cancel()
        active_tasks.pop(name, None)
    send_telegram("🛑 All monitors stopped.")

# ---------------- Run server ----------------
def run_flask():
    app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    logger.info("Starting Telegram futures spread bot with webhook...")

    if TELEGRAM_TOKEN and WEBHOOK_URL:
        try:
            requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={WEBHOOK_URL}/webhook"
            )
            logger.info("Webhook set successfully.")
        except Exception as e:
            logger.error(f"Webhook setup failed: {e}")

    # ✅ створюємо ГОЛОВНИЙ asyncio event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # ✅ запускаємо Flask у окремому потоці
    Thread(target=run_flask, daemon=True).start()

    # ✅ запускаємо головний loop
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        logger.info("Stopped.")