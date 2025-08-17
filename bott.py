напimport sqlite3
import requests
import time
import json
import logging
import random
import re
import threading
import os
from flask import Flask

# ===== НАЛАШТУВАННЯ =====
TOKEN = os.environ.get("TOKEN")  # 🔑 Бери токен з Environment у Render
DB_NAME = "crypto_bot.db"
COINGECKO_API_URL = "https://api.coingecko.com/api/v3"
REQUEST_LIMIT = 30
REQUEST_DELAY = 60 / REQUEST_LIMIT
last_request_time = 0

logging.basicConfig(
    filename='crypto_bot.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

price_cache = {}
CACHE_TIME = 300
alerts = {}
subscriptions = {}

top_coins_cache = {'coins': [], 'timestamp': 0}
CACHE_TOP_TIME = 600  # 10 хв

# ===== SQL =====
def init_db():
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance REAL DEFAULT 1000.0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS portfolios (user_id INTEGER, coin TEXT, amount REAL, PRIMARY KEY(user_id, coin))''')
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logging.error(f"DB init error: {e}")
        return False

# ===== TELEGRAM API =====
def telegram_request(method, params=None):
    global last_request_time
    elapsed = time.time() - last_request_time
    if elapsed < REQUEST_DELAY:
        time.sleep(REQUEST_DELAY - elapsed)
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    try:
        r = requests.post(url, json=params, timeout=30)
        last_request_time = time.time()
        if r.status_code == 200:
            return r.json().get("result", [])
        logging.error(f"Telegram API error {r.status_code}: {r.text}")
    except Exception as e:
        logging.error(f"Telegram request error: {e}")
    return []

def send_message(chat_id, text, keyboard=None):
    params = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if keyboard:
        params["reply_markup"] = json.dumps(keyboard)
    telegram_request("sendMessage", params)

# ===== CRYPTO API =====
def get_price(coin):
    if coin in price_cache and time.time() - price_cache[coin]['timestamp'] < CACHE_TIME:
        return price_cache[coin]['price']
    try:
        r = requests.get(f"{COINGECKO_API_URL}/simple/price?ids={coin}&vs_currencies=usd", timeout=15)
        if r.status_code == 200:
            price = r.json()[coin]['usd']
            price_cache[coin] = {'price': price, 'timestamp': time.time()}
            return price
    except Exception as e:
        logging.error(f"Price error: {e}")
    return None

def get_top_coins():
    if time.time() - top_coins_cache['timestamp'] < CACHE_TOP_TIME:
        return top_coins_cache['coins']
    try:
        r = requests.get(f"{COINGECKO_API_URL}/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=5&page=1", timeout=15)
        if r.status_code == 200:
            coins = r.json()
            top_coins_cache['coins'] = coins
            top_coins_cache['timestamp'] = time.time()
            return coins
    except Exception as e:
        logging.error(f"Top coins error: {e}")
    return []

# ===== ПОРТФЕЛЬ =====
def get_balance(user_id):
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 1000.0

def update_balance(user_id, amount):
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, balance) VALUES (?,?)", (user_id, 1000.0))
    c.execute("UPDATE users SET balance=? WHERE user_id=?", (amount, user_id))
    conn.commit(); conn.close()

# ===== АЛЕРТИ =====
def set_alert(user_id, coin, target_price):
    alerts[(user_id, coin)] = target_price

def check_alerts():
    for (user_id, coin), target_price in list(alerts.items()):
        price = get_price(coin)
        if price and price >= target_price:
            send_message(user_id, f"🚨 {coin} досяг {price}$ (ціль {target_price}$)")
            del alerts[(user_id, coin)]

# ===== ОБРОБКА КОМАНД =====
def process_updates(updates, last_update_id):
    for update in updates:
        if "message" in update:
            msg = update["message"]; user_id = msg["chat"]["id"]
            text = msg.get("text", "").lower()
            if text.startswith("/start"):
                send_message(user_id, "Вітаю! Це CryptoBot 🚀")
            elif text.startswith("/price"):
                parts = text.split()
                if len(parts) == 2:
                    coin = parts[1]
                    price = get_price(coin)
                    if price: send_message(user_id, f"💰 {coin}: {price}$")
                    else: send_message(user_id, "Не вдалося отримати ціну ❌")
            elif text.startswith("/balance"):
                bal = get_balance(user_id)
                send_message(user_id, f"💵 Баланс: {bal}$")
    return updates[-1]["update_id"] if updates else last_update_id

# ===== ОСНОВНИЙ ЦИКЛ =====
def main_bot():
    if not init_db():
        print("❌ Не вдалося ініціалізувати бота")
        return
    print("🟢 Бот запущено")
    last_update_id = 0
    while True:
        try:
            updates = telegram_request("getUpdates", {"offset": last_update_id+1, "timeout": 30})
            if updates:
                last_update_id = process_updates(updates, last_update_id)
            check_alerts()
            time.sleep(1)
        except Exception as e:
            logging.error(f"Main loop error: {e}")
            time.sleep(5)

# ===== ФЕЙКОВИЙ FLASK ДЛЯ RENDER =====
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Bot is running on Render!"

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ===== ЗАПУСК =====
if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    main_bot()