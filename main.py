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

GATE_API_KEY = "cf99af3f8c0c1a711408f1a1970be8be"
GATE_API_SECRET = "4bd0a51eac2133386e60f4c5e1a78ea9c364e542399bc1865e679f509e93f72e"

TRADE_AMOUNT_USD = 5         # малий обсяг
SPREAD_THRESHOLD = 2.0       # мінімальний спред %
CHECK_INTERVAL = 10          # інтервал перевірки

bot = telebot.TeleBot(API_KEY_TELEGRAM)
app = Flask(__name__)

gate = ccxt.gateio({
    "apiKey": GATE_API_KEY,
    "secret": GATE_API_SECRET,
    "options": {"defaultType": "swap"}  # futures (USDT-margined)
})

active_positions = {}  # ключ = символ, значення = "BUY"/"SELL"

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
# Отримання токенів з Dexscreener (Ethereum)
# -------------------------
def get_top_tokens(limit=10):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get("https://api.dexscreener.com/latest/dex/pairs/ethereum", headers=headers, timeout=10)

        if resp.status_code != 200:
            print(f"{datetime.now()} | ❌ Dexscreener HTTP {resp.status_code}")
            return []

        try:
            data = resp.json()
        except Exception:
            print(f"{datetime.now()} | ❌ Некоректна відповідь від Dexscreener:\n{resp.text[:200]}")
            return []

        tokens = []
        for pair in data.get("pairs", [])[:limit]:
            base = pair.get("baseToken", {})
            if "symbol" in base and "priceUsd" in pair:
                symbol = base["symbol"].upper() + "/USDT"
                dex_price = float(pair["priceUsd"])
                tokens.append((symbol, dex_price))
        return tokens
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка отримання топ токенів:", e)
        return []

# -------------------------
# Відкриття позиції на Gate
# -------------------------
def open_gate_position(symbol, side):
    pair = symbol.replace("/", "/USDT:USDT")
    if not is_pair_available(symbol):
        print(f"{datetime.now()} | ⚠️ Пара {pair} відсутня на Gate Futures, пропускаємо")
        return None, None
    if symbol in active_positions:
        print(f"{datetime.now()} | ⚠️ Позиція по {symbol} вже відкрита, пропускаємо")
        return None, None

    try:
        # перевірка балансу
        balance = gate.fetch_balance()
        usdt_available = balance['total'].get('USDT', 0)
        if usdt_available < TRADE_AMOUNT_USD:
            print(f"{datetime.now()} | ❌ Недостатньо USDT для торгівлі ({usdt_available})")
            return None, None

        ticker = gate.fetch_ticker(pair)
        gate_price = ticker['last']
        amount = TRADE_AMOUNT_USD / gate_price

        order = gate.create_order(
            symbol=pair,
            type="market",
            side=side.lower(),
            amount=amount
        )
        active_positions[symbol] = side
        msg = f"✅ Відкрито {side} {amount:.4f} {symbol}\nЦіна Gate: {gate_price:.4f}"
        print(f"{datetime.now()} | {msg}")
        bot.send_message(CHAT_ID, msg)
        return amount, gate_price
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка відкриття позиції:", e)
        return None, None

# -------------------------
# Лімітний ордер на закриття за DEX
# -------------------------
def close_gate_position(symbol, side, amount, dex_price):
    pair = symbol.replace("/", "/USDT:USDT")
    if not is_pair_available(symbol):
        print(f"{datetime.now()} | ⚠️ Пара {pair} відсутня на Gate Futures, не можемо закрити")
        return
    try:
        close_side = "SELL" if side == "BUY" else "BUY"
        order = gate.create_order(
            symbol=pair,
            type="limit",
            side=close_side.lower(),
            amount=amount,
            price=dex_price,
            params={"reduceOnly": True}
        )
        msg = f"🎯 Лімітний ордер {close_side} {amount:.4f} {symbol}\nЦіна DEX: {dex_price:.4f}"
        print(f"{datetime.now()} | {msg}")
        bot.send_message(CHAT_ID, msg)
        if symbol in active_positions:
            del active_positions[symbol]
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка закриття позиції:", e)

# -------------------------
# Арбітраж по одному токені
# -------------------------
def arbitrage(symbol, dex_price):
    try:
        pair = symbol.replace("/", "/USDT:USDT")
        if not is_pair_available(symbol):
            return

        gate_ticker = gate.fetch_ticker(pair)
        gate_price = gate_ticker['last']
        spread = (dex_price - gate_price) / gate_price * 100

        log = f"{symbol} | DEX: {dex_price:.4f} | Gate: {gate_price:.4f} | Spread: {spread:.2f}%"
        print(f"{datetime.now()} | {log}")

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
# Основний цикл арбітражу
# -------------------------
def start_arbitrage():
    while True:
        tokens = get_top_tokens(limit=10)
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
    threading.Thread(target=start_arbitrage, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)