import ccxt
import requests
import time
import os
from datetime import datetime
from flask import Flask, request
import telebot
import threading
import json

# -------------------------
# Налаштування через environment variables
# -------------------------
API_KEY_TELEGRAM = os.getenv("API_KEY_TELEGRAM")
CHAT_ID = os.getenv("CHAT_ID")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = WEBHOOK_HOST + WEBHOOK_PATH

GATE_API_KEY = os.getenv("GATE_API_KEY")
GATE_API_SECRET = os.getenv("GATE_API_SECRET")

MORALIS_API_KEY = os.getenv("MORALIS_API_KEY")
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")

TRADE_AMOUNT_USD = float(os.getenv("TRADE_AMOUNT_USD", 5))
SPREAD_THRESHOLD = float(os.getenv("SPREAD_THRESHOLD", 2.0))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 300))  # Збільшили до 5 хвилин

bot = telebot.TeleBot(API_KEY_TELEGRAM)
app = Flask(__name__)

# Ініціалізація бірж
try:
    gate = ccxt.gateio({
        "apiKey": GATE_API_KEY,
        "secret": GATE_API_SECRET,
        "options": {"defaultType": "spot"}  # Змінили на spot вместо swap
    })
    # Перевірка підключення до Gate.io
    gate.load_markets()
    print(f"{datetime.now()} | ✅ Успішно підключено до Gate.io")
except Exception as e:
    print(f"{datetime.now()} | ❌ Помилка підключення до Gate.io: {e}")
    gate = None

active_positions = {}
token_blacklist = set()
coingecko_last_call = 0

# -------------------------
# ПОКРАЩЕНИЙ ОТРИМАННЯ ТОКЕНІВ
# -------------------------
def get_top_tokens_from_coingecko(limit=25):
    """Отримання топ токенів з CoinGecko з обмеженням запитів"""
    global coingecko_last_call
    
    # Обмеження: 1 запит в 60 секунд
    current_time = time.time()
    if current_time - coingecko_last_call < 60:
        print(f"{datetime.now()} | ⏳ CoinGecko: зачекайте 60 секунд між запитами")
        return []
    
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": limit,
            "page": 1,
            "sparkline": False
        }
        
        headers = {}
        if COINGECKO_API_KEY and COINGECKO_API_KEY != "your_coingecko_api_key":
            headers = {"x-cg-demo-api-key": COINGECKO_API_KEY}
            
        response = requests.get(url, params=params, headers=headers, timeout=15)
        coingecko_last_call = current_time
        
        if response.status_code == 200:
            tokens = []
            for coin in response.json():
                symbol = coin["symbol"].upper()
                price = coin["current_price"]
                if price and price > 0:
                    # Правильний формат символу для Gate.io
                    tokens.append((f"{symbol}_USDT", price))
            print(f"{datetime.now()} | ✅ CoinGecko: знайдено {len(tokens)} токенів")
            return tokens
        else:
            print(f"{datetime.now()} | ❌ CoinGecko HTTP {response.status_code}")
            return []
    except Exception as e:
        print(f"{datetime.now()} | ❌ CoinGecko помилка: {e}")
        return []

def get_tokens_from_moralis_fixed(chain, limit=10):
    """Фіксована версія Moralis API"""
    if not MORALIS_API_KEY or MORALIS_API_KEY == "your_moralis_api_key":
        return []
    
    # Використовуємо правильний ендпоінт для топ токенів
    try:
        url = f"https://deep-index.moralis.io/api/v2.2/erc20/top?chain={chain}&limit={limit}"
        headers = {"X-API-Key": MORALIS_API_KEY}
        
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            tokens = []
            data = response.json()
            
            for token in data:
                symbol = token.get("symbol", "").upper()
                price = token.get("usdPrice", 0)
                
                if symbol and price > 0:
                    tokens.append((f"{symbol}_USDT", price))
            
            print(f"{datetime.now()} | ✅ Moralis {chain}: знайдено {len(tokens)} токенів")
            return tokens
        else:
            print(f"{datetime.now()} | ❌ Moralis {chain} HTTP {response.status_code}: {response.text}")
            return []
    except Exception as e:
        print(f"{datetime.now()} | ❌ Moralis {chain} помилка: {e}")
        return []

# -------------------------
# РЕЗЕРВНИЙ СПИСОК ТОКЕНІВ (з правильними символами)
# -------------------------
def get_backup_tokens():
    """Резервний список популярних токенів з правильними символами"""
    backup_tokens = [
        ("BTC", 50000),
        ("ETH", 3000),
        ("BNB", 500),
        ("SOL", 100),
        ("XRP", 0.5),
        ("ADA", 0.4),
        ("DOGE", 0.1),
        ("DOT", 5),
        ("LINK", 15),
        ("POL", 0.8),
        ("AVAX", 20),
        ("ATOM", 10),
        ("LTC", 70),
        ("UNI", 6),
        ("XLM", 0.12)
    ]
    print(f"{datetime.now()} | ✅ Резервний список: {len(backup_tokens)} токенів")
    return backup_tokens

# -------------------------
# ПОКРАЩЕНА ПЕРЕВІРКА ДОСТУПНОСТІ ПАРИ
# -------------------------
def is_pair_available(symbol):
    """Перевірка чи пара доступна на Gate.io"""
    if not gate:
        return False
        
    try:
        # Завантажуємо ринки один раз
        markets = gate.load_markets()
        
        # Перевіряємо безпосередньо символ
        if symbol in markets:
            market = markets[symbol]
            return market.get('active', False)
        
        return False
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка перевірки пари {symbol}: {e}")
        return False

# -------------------------
# ПОКРАЩЕНИЙ АРБІТРАЖ
# -------------------------
def smart_arbitrage(symbol, dex_price):
    """Розумний арбітраж з перевіркою волатильності"""
    if not gate or symbol in active_positions or not is_pair_available(symbol):
        return
        
    try:
        ticker = gate.fetch_ticker(symbol)
        gate_price = ticker['last']
        
        if gate_price == 0 or dex_price == 0:
            return
            
        spread = ((dex_price - gate_price) / gate_price) * 100
        
        # Додаткова перевірка волатильності
        if abs(spread) < SPREAD_THRESHOLD:
            return
            
        print(f"{datetime.now()} | 📊 {symbol} | Gate: {gate_price:.6f} | DEX: {dex_price:.6f} | Spread: {spread:.2f}%")
        
        # Логуємо знайдений арбітраж (без реального торгівлі)
        if abs(spread) >= SPREAD_THRESHOLD:
            msg = f"🎯 Знайдено арбітраж {symbol}\nSpread: {spread:.2f}%"
            print(f"{datetime.now()} | {msg}")
            
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка арбітражу {symbol}: {e}")

# -------------------------
# ОСНОВНИЙ ЦИКЛ АРБІТРАЖУ
# -------------------------
def start_arbitrage():
    """Основний цикл арбітражу"""
    bot.send_message(CHAT_ID, "🚀 Бот запущено. Починаю моніторинг...")
    
    cycle = 0
    while True:
        cycle += 1
        print(f"{datetime.now()} | 🔄 Цикл {cycle}")
        
        tokens = []
        
        # Спосіб 1: CoinGecko (з обмеженням запитів)
        if cycle % 2 == 1:  # Кожен другий цикл пропускаємо CoinGecko
            tokens.extend(get_top_tokens_from_coingecko(20))
        
        # Спосіб 2: Moralis (фіксована версія)
        if MORALIS_API_KEY and MORALIS_API_KEY != "your_moralis_api_key":
            chains = ["eth", "bsc"]
            for chain in chains:
                try:
                    chain_tokens = get_tokens_from_moralis_fixed(chain, 8)
                    if chain_tokens:
                        tokens.extend(chain_tokens)
                    time.sleep(1)
                except Exception as e:
                    print(f"{datetime.now()} | ❌ Moralis {chain} пропущено: {e}")
        
        # Видаляємо дублікати
        unique_tokens = list(set(tokens))
        
        # Якщо токени не знайдені, використовуємо резервний список
        if not unique_tokens:
            print(f"{datetime.now()} | ⚠️ Жодних токенів не знайдено, використовую резервний список")
            unique_tokens = get_backup_tokens()
        
        print(f"{datetime.now()} | 📦 Знайдено {len(unique_tokens)} унікальних токенів")
        
        # Перевіряємо арбітраж для кожного токена
        for symbol, price in unique_tokens:
            if gate:
                smart_arbitrage(symbol, price)
            time.sleep(0.5)
        
        time.sleep(CHECK_INTERVAL)

# -------------------------
# TELEGRAM КОМАНДИ
# -------------------------
@bot.message_handler(commands=['start'])
def send_welcome(message):
    """Команда старту"""
    bot.reply_to(message, "🤖 Арбітражний бот активовано!\n\n"
                         "Доступні команди:\n"
                         "/status - Статус системи\n"
                         "/balance - Баланс\n"
                         "/check_api - Перевірка API ключів\n"
                         "/stop - Зупинити бота")

@bot.message_handler(commands=['check_api'])
def check_api_command(message):
    """Перевірка API ключів"""
    issues = []
    
    if not API_KEY_TELEGRAM or API_KEY_TELEGRAM == "your_telegram_bot_token":
        issues.append("❌ Telegram API ключ не налаштовано")
    
    if not CHAT_ID or CHAT_ID == "your_chat_id":
        issues.append("❌ Chat ID не налаштовано")
    
    if not GATE_API_KEY or GATE_API_KEY == "your_gate_api_key":
        issues.append("❌ Gate.io API ключ не налаштовано")
    
    if not GATE_API_SECRET or GATE_API_SECRET == "your_gate_api_secret":
        issues.append("❌ Gate.io API секрет не налаштовано")
    
    if issues:
        response = "🔴 Проблеми з API ключами:\n\n" + "\n".join(issues)
    else:
        response = "✅ Всі API ключі налаштовано коректно!"
    
    bot.reply_to(message, response)

@bot.message_handler(commands=['status'])
def send_status(message):
    """Статус системи"""
    try:
        if gate:
            balance = gate.fetch_balance()
            usdt_balance = balance['total'].get('USDT', 0)
            msg = f"✅ Система працює\n💰 Баланс: {usdt_balance:.2f} USDT\n"
            msg += f"📊 Активних позицій: {len(active_positions)}\n"
            msg += f"⚫ Чорний список: {len(token_blacklist)} токенів"
        else:
            msg = "❌ Не підключено до Gate.io"
        bot.reply_to(message, msg)
    except Exception as e:
        bot.reply_to(message, f"❌ Помилка: {e}")

# -------------------------
# WEBHOOK ТА ЗАПУСК
# -------------------------
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    json_str = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "!", 200

def setup_webhook():
    """Налаштування webhook"""
    try:
        url = f"https://api.telegram.org/bot{API_KEY_TELEGRAM}/setWebhook"
        response = requests.post(url, data={"url": WEBHOOK_URL})
        print("Webhook setup:", response.json())
    except Exception as e:
        print(f"Webhook setup failed: {e}")

if __name__ == "__main__":
    print(f"{datetime.now()} | 🚀 Запуск арбітражного бота...")
    
    # Перевірка обов'язкових ключів
    required_keys = [API_KEY_TELEGRAM, CHAT_ID, GATE_API_KEY, GATE_API_SECRET]
    if not all(required_keys):
        print(f"{datetime.now()} | ❌ Відсутні обов'язкові API ключі!")
        exit(1)
    
    setup_webhook()
    threading.Thread(target=start_arbitrage, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)