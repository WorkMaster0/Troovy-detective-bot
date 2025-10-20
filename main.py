#!/usr/bin/env python3
import os
import asyncio
import time
import logging
from datetime import datetime
from threading import Thread
from typing import Dict
from flask import Flask, request
import requests
import ccxt.pro as ccxtpro
import ccxt

# ================= CONFIG =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. https://your-app.onrender.com/webhook
CHAT_ID = os.getenv("CHAT_ID")
PORT = int(os.getenv("PORT", "10000"))

# Біржові пари для моніторингу
PAIRS = {
    "bybit_mexc": ["bybit", "mexc"],
    "mexc_gmgn": ["mexc", "gmgn"],   # заміна lbank → gmgn (DEX Screener)
}

SPREAD_MIN_PCT = 2.0
SPREAD_MAX_PCT = 100.0
ALERT_COOLDOWN = 60
DEX_REFRESH_INTERVAL = 5  # сек

# ================= LOGGER =================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("spread-bot")

# ================= FLASK =================
app = Flask(__name__)

# Глобальні змінні
active_tasks: Dict[str, asyncio.Future] = {}
latest_quote = {}
last_alert_ts = {}

# ================= TELEGRAM =================
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("Telegram token or chat ID not set.")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)


# ================== BOT COMMANDS ==================
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "message" not in data:
        return "ok", 200

    msg = data["message"]
    text = msg.get("text", "").strip().lower()

    if text == "/start":
        send_telegram("🤖 *Spread Bot запущено!*\nВикористай /help для списку команд.")
    elif text == "/help":
        send_telegram(
            "📘 *Команди:*\n"
            "/status — статус активних моніторів\n"
            "/list — доступні пари\n"
            "/bybit_mexc — запуск BYBIT↔MEXC\n"
            "/mexc_gmgn — запуск MEXC↔DEX (GMGN)\n"
            "/stop — зупинити всі\n"
            "/stop_bybit_mexc — зупинити BYBIT↔MEXC\n"
            "/stop_mexc_gmgn — зупинити MEXC↔GMGN"
        )
    elif text == "/status":
        if not active_tasks:
            send_telegram("🟡 Немає активних моніторів.")
        else:
            send_telegram("🟢 Активні монітори:\n" + "\n".join(f"• {n}" for n in active_tasks))
    elif text == "/list":
        send_telegram("📊 *Доступні пари:*\n" + "\n".join(f"/{n}" for n in PAIRS))
    elif text.startswith("/bybit_mexc"):
        start_monitor("bybit_mexc")
    elif text.startswith("/mexc_gmgn"):
        start_monitor("mexc_gmgn")
    elif text == "/stop":
        stop_all()
    elif text.startswith("/stop_bybit_mexc"):
        stop_monitor("bybit_mexc")
    elif text.startswith("/stop_mexc_gmgn"):
        stop_monitor("mexc_gmgn")
    else:
        send_telegram("❓ Невідома команда. Використай /help")

    return "ok", 200


# ================== EXCHANGE SETUP ==================
async def create_client(ex_id):
    if ex_id == "gmgn":
        return "dexscreener"  # маркер, що це не ccxt біржа
    try:
        client = getattr(ccxtpro, ex_id)({"enableRateLimit": True})
        client.options["defaultType"] = "swap"
        await asyncio.sleep(0)
        return client
    except Exception as e:
        logger.error("Failed to init %s: %s", ex_id, e)
        return None


# ================== WATCH LOGIC ==================
async def fetch_dex_price(symbol: str):
    """Отримати ціну токена з DEX Screener API."""
    try:
        url = f"https://api.dexscreener.com/latest/dex/search?q={symbol.replace('/', '')}"
        r = requests.get(url, timeout=10).json()
        if "pairs" in r and len(r["pairs"]) > 0:
            pair = r["pairs"][0]
            price = float(pair.get("priceUsd", 0))
            if price > 0:
                return price
    except Exception as e:
        logger.debug("DEX fetch error: %s", e)
    return None


async def watch_pair(ex1, ex2):
    logger.info(f"👀 Starting monitor {ex1.upper()} ↔ {ex2.upper()}")
    clients = {}
    for ex in [ex1, ex2]:
        clients[ex] = await create_client(ex)
        latest_quote[ex] = {}

    # Визначення режиму (DEX чи біржа)
    dex_mode = "gmgn" in [ex1, ex2]

    # Якщо є DEX — беремо спільні USDT-пари між MEXC та DEX-токенами
    if dex_mode:
        try:
            ccx_ex = ex1 if ex1 != "gmgn" else ex2
            m1 = getattr(ccxt, ccx_ex)().load_markets()
            symbols = [s for s in m1 if "USDT" in s and ":USDT" in s]
        except Exception as e:
            logger.error("Market load error: %s", e)
            return
    else:
        # стандартна логіка
        try:
            m1 = getattr(ccxt, ex1)().load_markets()
            m2 = getattr(ccxt, ex2)().load_markets()
            common = set(m1).intersection(set(m2))
            symbols = [s for s in common if "USDT" in s and ":USDT" in s]
        except Exception as e:
            logger.error("Market load error: %s", e)
            return

    async def watch_ccxt(ex):
        while True:
            for s in symbols[:60]:
                try:
                    ob = await clients[ex].watch_order_book(s)
                    bid, ask = ob["bids"][0][0], ob["asks"][0][0]
                    latest_quote[ex][s] = {"bid": bid, "ask": ask, "ts": time.time()}
                    await check_spread(s, ex1, ex2)
                except Exception:
                    await asyncio.sleep(0.1)
            await asyncio.sleep(0.05)

    async def watch_dex():
        while True:
            for s in symbols[:30]:
                price = await fetch_dex_price(s)
                if not price:
                    continue
                latest_quote["gmgn"][s] = {
                    "bid": price * 0.999,
                    "ask": price * 1.001,
                    "ts": time.time(),
                }
                await check_spread(s, ex1, ex2)
                await asyncio.sleep(0.2)
            await asyncio.sleep(DEX_REFRESH_INTERVAL)

    # Запускаємо
    if dex_mode:
        if ex1 == "gmgn":
            await asyncio.gather(watch_dex(), watch_ccxt(ex2))
        else:
            await asyncio.gather(watch_ccxt(ex1), watch_dex())
    else:
        await asyncio.gather(watch_ccxt(ex1), watch_ccxt(ex2))


async def check_spread(symbol, ex1, ex2):
    if symbol not in latest_quote.get(ex1, {}) or symbol not in latest_quote.get(ex2, {}):
        return

    q1, q2 = latest_quote[ex1][symbol], latest_quote[ex2][symbol]
    for (buy_ex, buy_ask), (sell_ex, sell_bid) in [
        ((ex1, q1["ask"]), (ex2, q2["bid"])),
        ((ex2, q2["ask"]), (ex1, q1["bid"])),
    ]:
        diff = sell_bid - buy_ask
        if diff <= 0:
            continue
        pct = (diff / buy_ask) * 100
        if pct < SPREAD_MIN_PCT or pct > SPREAD_MAX_PCT:
            continue

        key = (symbol, buy_ex, sell_ex)
        now = time.time()
        if now - last_alert_ts.get(key, 0) < ALERT_COOLDOWN:
            return
        last_alert_ts[key] = now

        msg = (
            f"🔔 *Spread Alert*\n"
            f"Symbol: `{symbol}`\n"
            f"Buy: `{buy_ask:.6f}` @ *{buy_ex}*\n"
            f"Sell: `{sell_bid:.6f}` @ *{sell_ex}*\n"
            f"Spread: *{pct:.2f}%* (`{diff:.6f}`)\n"
            f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        logger.info(f"ALERT: {symbol} {pct:.2f}% ({buy_ex}->{sell_ex})")
        send_telegram(msg)


# ================== CONTROL FUNCTIONS ==================
def start_monitor(pair_name):
    if pair_name in active_tasks:
        send_telegram(f"⚙️ Монітор {pair_name} вже активний.")
        return

    ex1, ex2 = PAIRS[pair_name]
    task = asyncio.run_coroutine_threadsafe(watch_pair(ex1, ex2), loop)
    active_tasks[pair_name] = task
    send_telegram(f"✅ Запущено монітор *{pair_name.upper()}*")


def stop_monitor(pair_name):
    if pair_name not in active_tasks:
        send_telegram(f"⚙️ Монітор {pair_name} не активний.")
        return
    active_tasks[pair_name].cancel()
    active_tasks.pop(pair_name, None)
    send_telegram(f"🛑 Зупинено монітор {pair_name.upper()}")


def stop_all():
    for n, t in list(active_tasks.items()):
        t.cancel()
        active_tasks.pop(n, None)
    send_telegram("🛑 Усі монітори зупинено.")


# ================== SERVER ==================
def run_flask():
    app.run(host="0.0.0.0", port=PORT)


# ================== ENTRY POINT ==================
if __name__ == "__main__":
    logger.info("🚀 Starting Spread Bot (DEX + Futures)")

    if TELEGRAM_TOKEN and WEBHOOK_URL:
        try:
            requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={WEBHOOK_URL}/webhook"
            )
            logger.info("Webhook set successfully.")
        except Exception as e:
            logger.error(f"Webhook setup failed: {e}")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    Thread(target=run_flask, daemon=True).start()

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        logger.info("Stopped.")