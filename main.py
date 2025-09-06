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
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))

bot = telebot.TeleBot(API_KEY_TELEGRAM)
app = Flask(__name__)

# Ініціалізація бірж
try:
    gate = ccxt.gateio({
        "apiKey": GATE_API_KEY,
        "secret": GATE_API_SECRET,
        "options": {"defaultType": "swap"}
    })
    # Перевірка підключення до Gate.io
    gate.load_markets()
    print(f"{datetime.now()} | ✅ Успішно підключено до Gate.io")
except Exception as e:
    print(f"{datetime.now()} | ❌ Помилка підключення до Gate.io: {e}")
    gate = None

active_positions = {}
token_blacklist = set()

# -------------------------
# ПЕРЕВІРКА API КЛЮЧІВ
# -------------------------
def check_api_keys():
    """Перевірка коректності API ключів"""
    issues = []
    
    if not API_KEY_TELEGRAM or API_KEY_TELEGRAM == "your_telegram_bot_token":
        issues.append("❌ Telegram API ключ не налаштовано")
    
    if not CHAT_ID or CHAT_ID == "your_chat_id":
        issues.append("❌ Chat ID не налаштовано")
    
    if not GATE_API_KEY or GATE_API_KEY == "your_gate_api_key":
        issues.append("❌ Gate.io API ключ не налаштовано")
    
    if not GATE_API_SECRET or GATE_API_SECRET == "your_gate_api_secret":
        issues.append("❌ Gate.io API секрет не налаштовано")
    
    if not MORALIS_API_KEY or MORALIS_API_KEY == "your_moralis_api_key":
        issues.append("⚠️ Moralis API ключ не налаштовано (не обов'язково)")
    
    # Перевірка працездатності Gate.io API
    if gate:
        try:
            balance = gate.fetch_balance()
            print(f"{datetime.now()} | ✅ Gate.io API працює, баланс: {balance['total'].get('USDT', 0):.2f} USDT")
        except Exception as e:
            issues.append(f"❌ Gate.io API не працює: {str(e)[:100]}")
    
    return issues

# -------------------------
# ПОКРАЩЕНИЙ ОТРИМАННЯ ТОКЕНІВ
# -------------------------
def get_top_tokens_from_coingecko(limit=30):
    """Отримання топ токенів з CoinGecko"""
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
            
        response = requests.get(url, params=params, headers=headers, timeout=10)
        
        if response.status_code == 200:
            tokens = []
            for coin in response.json():
                symbol = coin["symbol"].upper() + "/USDT"
                price = coin["current_price"]
                if price and price > 0:
                    tokens.append((symbol, price))
            print(f"{datetime.now()} | ✅ CoinGecko: знайдено {len(tokens)} токенів")
            return tokens
        else:
            print(f"{datetime.now()} | ❌ CoinGecko HTTP {response.status_code}")
            return []
    except Exception as e:
        print(f"{datetime.now()} | ❌ CoinGecko помилка: {e}")
        return []

def get_tokens_from_coingecko_trending(limit=15):
    """Отримання трендових токенів з CoinGecko"""
    try:
        url = "https://api.coingecko.com/api/v3/search/trending"
        response = requests.get(url, timeout=10)
        
        if response.status_code == 200:
            tokens = []
            data = response.json()
            trending_coins = data.get("coins", [])[:limit]
            
            for item in trending_coins:
                coin_id = item["item"]["id"]
                symbol = item["item"]["symbol"].upper() + "/USDT"
                
                # Спрощена версія без додаткового запиту ціни
                price_url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
                price_response = requests.get(price_url, timeout=8)
                
                if price_response.status_code == 200:
                    price_data = price_response.json()
                    usd_price = price_data.get(coin_id, {}).get("usd", 0)
                    if usd_price > 0:
                        tokens.append((symbol, usd_price))
            
            print(f"{datetime.now()} | ✅ CoinGecko Trending: знайдено {len(tokens)} токенів")
            return tokens
        return []
    except Exception as e:
        print(f"{datetime.now()} | ❌ CoinGecko trending помилка: {e}")
        return []

def get_tokens_from_moralis(chain, limit=15):
    """Отримання токенів з Moralis"""
    # Перевірка чи API ключ налаштовано
    if not MORALIS_API_KEY or MORALIS_API_KEY == "your_moralis_api_key":
        return []
    
    chain_mapping = {
        "eth": "eth",
        "bsc": "bsc", 
        "polygon": "polygon"
    }
    
    if chain not in chain_mapping:
        return []
    
    moralis_chain = chain_mapping[chain]
    url = f"https://deep-index.moralis.io/api/v2.2/erc20/metadata?chain={moralis_chain}&limit={limit}"
    headers = {"X-API-Key": MORALIS_API_KEY}
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            tokens = []
            data = response.json()
            
            if isinstance(data, list):
                for token in data[:limit]:  # Обмежуємо кількість
                    symbol = token.get("symbol", "").upper()
                    address = token.get("address", "")
                    
                    if not symbol or not address or symbol in token_blacklist:
                        continue
                    
                    tokens.append((f"{symbol}/USDT", 1.0))  # Типова ціна для тесту
                    
            print(f"{datetime.now()} | ✅ Moralis {chain}: знайдено {len(tokens)} токенів")
            return tokens
        else:
            print(f"{datetime.now()} | ❌ Moralis {chain} HTTP {response.status_code}")
            return []
    except Exception as e:
        print(f"{datetime.now()} | ❌ Moralis {chain} помилка: {e}")
        return []

# -------------------------
# РЕЗЕРВНИЙ СПИСОК ТОКЕНІВ
# -------------------------
def get_backup_tokens():
    """Резервний список популярних токенів"""
    backup_tokens = [
        ("BTC/USDT", 50000),
        ("ETH/USDT", 3000),
        ("BNB/USDT", 500),
        ("SOL/USDT", 100),
        ("XRP/USDT", 0.5),
        ("ADA/USDT", 0.4),
        ("DOGE/USDT", 0.1),
        ("DOT/USDT", 5),
        ("LINK/USDT", 15),
        ("MATIC/USDT", 0.8),
        ("AVAX/USDT", 20),
        ("ATOM/USDT", 10),
        ("LTC/USDT", 70),
        ("UNI/USDT", 6),
        ("XLM/USDT", 0.12)
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
        base_symbol = symbol.split('/')[0]
        formats = [
            f"{base_symbol}_USDT",
            f"{base_symbol}/USDT:USDT",
            f"{base_symbol.lower()}_usdt"
        ]
        
        markets = gate.load_markets()
        for fmt in formats:
            if fmt in markets:
                market = markets[fmt]
                if market['active']:
                    return True
        return False
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка перевірки пари {symbol}: {e}")
        return False

# -------------------------
# ОСНОВНИЙ ЦИКЛ АРБІТРАЖУ
# -------------------------
def start_arbitrage():
    """Основний цикл арбітражу"""
    
    # Перевірка API ключів при старті
    api_issues = check_api_keys()
    if api_issues:
        error_msg = "🔴 ПРОБЛЕМИ З API КЛЮЧАМИ:\n\n" + "\n".join(api_issues)
        print(f"{datetime.now()} | {error_msg}")
        bot.send_message(CHAT_ID, error_msg)
    
    bot.send_message(CHAT_ID, "🚀 Бот запущено. Починаю моніторинг...")
    
    cycle = 0
    while True:
        cycle += 1
        print(f"{datetime.now()} | 🔄 Цикл {cycle}")
        
        tokens = []
        
        # Спосіб 1: CoinGecko (найнадійніший)
        tokens.extend(get_top_tokens_from_coingecko(25))
        tokens.extend(get_tokens_from_coingecko_trending(10))
        
        # Спосіб 2: Moralis (якщо працює)
        if MORALIS_API_KEY and MORALIS_API_KEY != "your_moralis_api_key":
            chains = ["eth", "bsc"]
            for chain in chains:
                try:
                    chain_tokens = get_tokens_from_moralis(chain, 10)
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
            if gate:  # Перевіряємо чи gate ініціалізовано
                smart_arbitrage(symbol, price)
            time.sleep(0.2)
        
        time.sleep(CHECK_INTERVAL)

# -------------------------
# Спрощені функції арбітражу для тесту
# -------------------------
def smart_arbitrage(symbol, dex_price):
    """Спрощена версія арбітражу для тесту"""
    if not gate or symbol in active_positions or not is_pair_available(symbol):
        return
        
    try:
        gate_symbol = symbol.replace("/", "_USDT")
        ticker = gate.fetch_ticker(gate_symbol)
        gate_price = ticker['last']
        
        if gate_price == 0 or dex_price == 0:
            return
            
        spread = ((dex_price - gate_price) / gate_price) * 100
        
        print(f"{datetime.now()} | 📊 {symbol} | Gate: {gate_price:.6f} | DEX: {dex_price:.6f} | Spread: {spread:.2f}%")
        
        # Тільки логуємо, не відкриваємо реальні позиції
        if abs(spread) >= SPREAD_THRESHOLD:
            msg = f"🎯 Знайдено арбітраж {symbol}\nSpread: {spread:.2f}%"
            print(f"{datetime.now()} | {msg}")
            
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка арбітражу {symbol}: {e}")

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
    issues = check_api_keys()
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

@bot.message_handler(commands=['balance'])
def send_balance(message):
    """Баланс"""
    try:
        if gate:
            balance = gate.fetch_balance()
            usdt = balance['total'].get('USDT', 0)
            bot.reply_to(message, f"💰 Баланс: {usdt:.2f} USDT")
        else:
            bot.reply_to(message, "❌ Не підключено до Gate.io")
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