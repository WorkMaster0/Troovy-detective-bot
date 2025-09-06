import ccxt
import requests
import time
import os
from datetime import datetime
from flask import Flask, request
import telebot
import threading

# -------------------------
# Налаштування через environment variables
# -------------------------
API_KEY_TELEGRAM = os.getenv("API_KEY_TELEGRAM")
CHAT_ID = os.getenv("CHAT_ID")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")  # наприклад: https://troovy-detective-bot-1-4on4.onrender.com
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = WEBHOOK_HOST + WEBHOOK_PATH

GATE_API_KEY = os.getenv("GATE_API_KEY")
GATE_API_SECRET = os.getenv("GATE_API_SECRET")

MORALIS_API_KEY = os.getenv("MORALIS_API_KEY")

TRADE_AMOUNT_USD = float(os.getenv("TRADE_AMOUNT_USD", 5))
SPREAD_THRESHOLD = float(os.getenv("SPREAD_THRESHOLD", 2.0))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 10))

bot = telebot.TeleBot(API_KEY_TELEGRAM)
app = Flask(__name__)

gate = ccxt.gateio({
    "apiKey": GATE_API_KEY,
    "secret": GATE_API_SECRET,
    "options": {"defaultType": "swap"}
})

active_positions = {}

# -------------------------
# Перевірка чи пара існує на Gate Futures
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
# Отримання всіх токенів з Moralis
# -------------------------
def get_all_tokens(chain, limit=100, retries=3):
    url = f"https://deep-index.moralis.io/api/v2/erc20?chain={chain}&limit={limit}"
    headers = {"X-API-Key": MORALIS_API_KEY}

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code != 200:
                print(f"{datetime.now()} | ⚠️ Moralis ({chain}) HTTP {resp.status_code} спроба {attempt}")
                time.sleep(2)
                continue

            data = resp.json()
            tokens = []
            for token in data:
                symbol = token.get("symbol")
                price = float(token.get("usdPrice", 0))
                if symbol and price > 0:
                    tokens.append((symbol + "/USDT", price))
            return tokens
        except Exception as e:
            print(f"{datetime.now()} | ❌ Помилка Moralis ({chain}) спроба {attempt}: {e}")
            time.sleep(2)
    return []

# -------------------------
# Відкриття позиції
# -------------------------
def open_gate_position(symbol, side):
    pair = symbol.replace("/", "/USDT:USDT")
    if not is_pair_available(symbol) or symbol in active_positions:
        return None, None
    try:
        balance = gate.fetch_balance()
        usdt_available = balance['total'].get('USDT', 0)
        if usdt_available < TRADE_AMOUNT_USD:
            return None, None
        ticker = gate.fetch_ticker(pair)
        gate_price = ticker['last']
        amount = TRADE_AMOUNT_USD / gate_price
        gate.create_order(symbol=pair, type="market", side=side.lower(), amount=amount)
        active_positions[symbol] = side
        msg = f"✅ Відкрито {side} {amount:.4f} {symbol} за Gate ціною {gate_price:.4f}"
        print(f"{datetime.now()} | {msg}")
        bot.send_message(CHAT_ID, msg)
        return amount, gate_price
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка відкриття {symbol}: {e}")
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
        gate.create_order(symbol=pair, type="limit", side=close_side.lower(),
                          amount=amount, price=dex_price, params={"reduceOnly": True})
        msg = f"🎯 Закрито {close_side} {amount:.4f} {symbol} за ціною {dex_price:.4f}"
        print(f"{datetime.now()} | {msg}")
        bot.send_message(CHAT_ID, msg)
        active_positions.pop(symbol, None)
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка закриття {symbol}: {e}")

# -------------------------
# Арбітраж
# -------------------------
def arbitrage(symbol, dex_price):
    if not is_pair_available(symbol):
        return
    try:
        gate_price = gate.fetch_ticker(symbol.replace("/", "/USDT:USDT"))['last']
        spread = (dex_price - gate_price) / gate_price * 100
        print(f"{datetime.now()} | {symbol} | Gate: {gate_price:.4f} | DEX: {dex_price:.4f} | Spread: {spread:.2f}%")
        if spread >= SPREAD_THRESHOLD:
            amount, _ = open_gate_position(symbol, "BUY")
            if amount:
                close_gate_position(symbol, "BUY", amount, dex_price)
        elif spread <= -SPREAD_THRESHOLD:
            amount, _ = open_gate_position(symbol, "SELL")
            if amount:
                close_gate_position(symbol, "SELL", amount, dex_price)
    except Exception as e:
        print(f"{datetime.now()} | ❌ Арбітраж {symbol} помилка: {e}")

# -------------------------
# Основний цикл
# -------------------------
def start_arbitrage():
    chains = ["eth", "bsc", "sol"]
    bot.send_message(CHAT_ID, "🚀 Бот запущено. Починаю моніторинг (Moralis API)")
    cycle = 0
    while True:
        cycle += 1
        all_tokens = []
        for chain in chains:
            tokens = get_all_tokens(chain, limit=100)
            all_tokens.extend(tokens)
            bot.send_message(CHAT_ID, f"🔍 {chain.upper()}: отримано {len(tokens)} токенів")
            print(f"{datetime.now()} | {chain.upper()} отримано {len(tokens)} токенів")
        if not all_tokens:
            bot.send_message(CHAT_ID, f"⚠️ Цикл {cycle}: токенів не знайдено")
        for symbol, price in all_tokens:
            arbitrage(symbol, price)
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
    threading.Thread(target=start_arbitrage, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)