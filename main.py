import ccxt
import requests
import time
from datetime import datetime
from flask import Flask, request
import telebot
import threading

# -------------------------
# Налаштування
# -------------------------
API_KEY_TELEGRAM = "8051222216:AAFORHEn1IjWllQyPp8W_1OY3gVxcBNVZI"
CHAT_ID = "6053907025"
WEBHOOK_HOST = "https://troovy-detective-bot-1.onrender.com"
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = WEBHOOK_HOST + WEBHOOK_PATH

MORALIS_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJub25jZSI6ImI4NjlmZDRjLTRmMTEtNGUxYi1hYjk2LWUyYjhlOTYxMDAzNiIsIm9yZ0lkIjoiNDY5NDkxIiwidXNlcklkIjoiNDgyOTgzIiwidHlwZUlkIjoiN2I3YTRhM2ItOWJlMC00YWVlLWJkZDAtNmEwZTdmNGYyNzc0IiwidHlwZSI6IlBST0pFQ1QiLCJpYXQiOjE3NTcxODYzOTksImV4cCI6NDkxMjk0NjM5OX0.WwfzETTGBUWMApDPuWVW8p8tuTdreYKOAgrolp5TuWM"

GATE_API_KEY = "cf99af3f8c0c1a711408f1a1970be8be"
GATE_API_SECRET = "4bd0a51eac2133386e60f4c5e1a78ea9c364e542399bc1865e679f509e93f72e"

TRADE_AMOUNT_USD = 5
SPREAD_THRESHOLD = 2.0
CHECK_INTERVAL = 10

bot = telebot.TeleBot(API_KEY_TELEGRAM)
app = Flask(__name__)

gate = ccxt.gateio({
    "apiKey": GATE_API_KEY,
    "secret": GATE_API_SECRET,
    "options": {"defaultType": "swap"}  # ф'ючерси
})

active_positions = {}

# -------------------------
# Moralis API: отримання топ токенів
# -------------------------
def get_top_tokens_moralis(chain="eth", limit=10):
    url = f"https://deep-index.moralis.io/api/v2.2/market-data/{chain}/erc20/top-tokens"
    headers = {"X-API-Key": MORALIS_API_KEY}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"{datetime.now()} | ⚠️ Moralis {chain} HTTP {resp.status_code}")
            return []

        data = resp.json()
        tokens = []
        for t in data.get("result", [])[:limit]:
            symbol = t["symbol"].upper() + "/USDT"
            price = float(t["price_usd"])
            tokens.append((symbol, price))
        return tokens
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка Moralis ({chain}):", e)
        return []

def get_all_tokens(limit=5):
    tokens = []
    for chain in ["eth", "bsc", "solana"]:
        tokens.extend(get_top_tokens_moralis(chain, limit))
    return tokens

# -------------------------
# Gate: перевірка пари
# -------------------------
def is_pair_available(symbol):
    pair = symbol.replace("/", "/USDT:USDT")
    try:
        markets = gate.load_markets()
        return pair in markets
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка завантаження ринків:", e)
        return False

# -------------------------
# Відкриття позиції
# -------------------------
def open_gate_position(symbol, side):
    pair = symbol.replace("/", "/USDT:USDT")
    if not is_pair_available(symbol):
        return None, None
    if symbol in active_positions:
        return None, None

    try:
        balance = gate.fetch_balance()
        usdt_available = balance['total'].get('USDT', 0)
        if usdt_available < TRADE_AMOUNT_USD:
            return None, None

        ticker = gate.fetch_ticker(pair)
        gate_price = ticker['last']
        amount = TRADE_AMOUNT_USD / gate_price

        gate.create_order(
            symbol=pair,
            type="market",
            side=side.lower(),
            amount=amount
        )
        active_positions[symbol] = side
        msg = f"✅ Відкрито {side} {amount:.4f} {symbol}\nGate ціна: {gate_price:.4f}"
        bot.send_message(CHAT_ID, msg)
        return amount, gate_price
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка відкриття позиції:", e)
        return None, None

# -------------------------
# Закриття позиції
# -------------------------
def close_gate_position(symbol, side, amount, dex_price):
    pair = symbol.replace("/", "/USDT:USDT")
    if not is_pair_available(symbol):
        return
    try:
        close_side = "SELL" if side == "BUY" else "BUY"
        gate.create_order(
            symbol=pair,
            type="limit",
            side=close_side.lower(),
            amount=amount,
            price=dex_price,
            params={"reduceOnly": True}
        )
        msg = f"🎯 Закриття {close_side} {amount:.4f} {symbol}\nDEX ціна: {dex_price:.4f}"
        bot.send_message(CHAT_ID, msg)
        if symbol in active_positions:
            del active_positions[symbol]
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка закриття позиції:", e)

# -------------------------
# Арбітраж
# -------------------------
def arbitrage(symbol, dex_price):
    try:
        pair = symbol.replace("/", "/USDT:USDT")
        if not is_pair_available(symbol):
            return

        gate_price = gate.fetch_ticker(pair)['last']
        spread = (dex_price - gate_price) / gate_price * 100

        bot.send_message(CHAT_ID, f"{symbol} | DEX: {dex_price:.4f} | Gate: {gate_price:.4f} | Spread: {spread:.2f}%")

        if spread >= SPREAD_THRESHOLD:
            amount, _ = open_gate_position(symbol, "BUY")
            if amount:
                close_gate_position(symbol, "BUY", amount, dex_price)
        elif spread <= -SPREAD_THRESHOLD:
            amount, _ = open_gate_position(symbol, "SELL")
            if amount:
                close_gate_position(symbol, "SELL", amount, dex_price)
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка арбітражу:", e)

# -------------------------
# Основний цикл
# -------------------------
def start_arbitrage():
    cycle = 0
    while True:
        cycle += 1
        tokens = get_all_tokens(limit=5)
        bot.send_message(CHAT_ID, f"🔎 Цикл {cycle}: отримано {len(tokens)} токенів")
        for symbol, dex_price in tokens:
            arbitrage(symbol, dex_price)
        time.sleep(CHECK_INTERVAL)

# -------------------------
# Telegram Webhook
# -------------------------
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    json_str = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "!", 200

def setup_webhook():
    url = f"https://api.telegram.org/bot{API_KEY_TELEGRAM}/setWebhook"
    response = requests.post(url, data={"url": WEBHOOK_URL})
    print("Webhook setup:", response.json())

# -------------------------
# Запуск
# -------------------------
if __name__ == "__main__":
    setup_webhook()
    bot.send_message(CHAT_ID, "🚀 Бот запущено. Починаю моніторинг (Moralis API)")
    threading.Thread(target=start_arbitrage, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)