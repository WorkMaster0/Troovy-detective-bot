import ccxt
import requests
import time
import os
from datetime import datetime
from flask import Flask, request
import telebot
import threading
import json
import math
from typing import List, Tuple, Dict

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
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 300))
MIN_VOLUME_USD = float(os.getenv("MIN_VOLUME_USD", 10000))  # Мінімальний об'єм

bot = telebot.TeleBot(API_KEY_TELEGRAM)
app = Flask(__name__)

# Ініціалізація бірж
try:
    gate = ccxt.gateio({
        "apiKey": GATE_API_KEY,
        "secret": GATE_API_SECRET,
        "options": {"defaultType": "spot"}
    })
    gate.load_markets()
    print(f"{datetime.now()} | ✅ Успішно підключено до Gate.io")
except Exception as e:
    print(f"{datetime.now()} | ❌ Помилка підключення до Gate.io: {e}")
    gate = None

active_positions = {}
token_blacklist = set()
coingecko_last_call = 0

# -------------------------
# ПОКРАЩЕНЕ ОТРИМАННЯ ТОКЕНІВ
# -------------------------
def get_top_tokens_from_coingecko(limit=25) -> List[Tuple[str, float]]:
    """Отримання топ токенів з CoinGecko"""
    global coingecko_last_call
    
    current_time = time.time()
    if current_time - coingecko_last_call < 60:
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
        if COINGECKO_API_KEY:
            headers = {"x-cg-demo-api-key": COINGECKO_API_KEY}
            
        response = requests.get(url, params=params, headers=headers, timeout=15)
        coingecko_last_call = current_time
        
        if response.status_code == 200:
            tokens = []
            for coin in response.json():
                symbol = coin["symbol"].upper()
                price = coin["current_price"]
                if price and price > 0:
                    # ПРАВИЛЬНИЙ ФОРМАТ ДЛЯ GATE.IO
                    tokens.append((f"{symbol}/USDT", price))
            return tokens
        return []
    except Exception as e:
        print(f"{datetime.now()} | ❌ CoinGecko помилка: {e}")
        return []

def get_tokens_from_binance() -> List[Tuple[str, float]]:
    """Отримання топ токенів з Binance через їх API"""
    try:
        url = "https://api.binance.com/api/v3/ticker/price"
        response = requests.get(url, timeout=10)
        
        if response.status_code == 200:
            tokens = []
            for item in response.json():
                symbol = item['symbol']
                if symbol.endswith('USDT'):
                    tokens.append((symbol.replace('USDT', '/USDT'), float(item['price'])))
                    if len(tokens) >= 30:  # Обмежуємо кількість
                        break
            return tokens
        return []
    except Exception as e:
        print(f"{datetime.now()} | ❌ Binance помилка: {e}")
        return []

# -------------------------
# ТОРГОВА ЛОГІКА
# -------------------------
def execute_trade(symbol: str, gate_price: float, dex_price: float, spread: float):
    """Виконання торгової операції"""
    try:
        if spread > 0:  # DEX ціна вища - купуємо на Gate, продаємо на DEX
            # Купівля на Gate.io
            amount = TRADE_AMOUNT_USD / gate_price
            order = gate.create_market_buy_order(symbol, amount)
            
            msg = f"✅ ВИКОНАНО: Купівля {symbol}\n"
            msg += f"Сума: {amount:.6f}\n"
            msg += f"Ціна: {gate_price:.6f}\n"
            msg += f"Spread: {spread:.2f}%"
            
        else:  # Gate ціна вища - купуємо на DEX, продаємо на Gate
            # Продаж на Gate.io
            amount = TRADE_AMOUNT_USD / gate_price
            order = gate.create_market_sell_order(symbol, amount)
            
            msg = f"✅ ВИКОНАНО: Продаж {symbol}\n"
            msg += f"Сума: {amount:.6f}\n"
            msg += f"Ціна: {gate_price:.6f}\n"
            msg += f"Spread: {spread:.2f}%"
        
        bot.send_message(CHAT_ID, msg)
        print(f"{datetime.now()} | {msg}")
        
    except Exception as e:
        error_msg = f"❌ ПОМИЛКА торгівлі {symbol}: {e}"
        bot.send_message(CHAT_ID, error_msg)
        print(f"{datetime.now()} | {error_msg}")

# -------------------------
# ПОКРАЩЕНИЙ АРБІТРАЖ
# -------------------------
def smart_arbitrage(symbol: str, dex_price: float):
    """Розумний арбітраж з перевіркою волатильності"""
    if not gate or symbol in active_positions or symbol in token_blacklist:
        return
    
    try:
        # Перевірка доступності пари
        markets = gate.load_markets()
        if symbol not in markets or not markets[symbol].get('active', False):
            return
        
        # Перевірка об'єму торгів
        ticker = gate.fetch_ticker(symbol)
        gate_price = ticker['last']
        volume = ticker['quoteVolume']  # Об'єм в USDT
        
        if gate_price == 0 or dex_price == 0 or volume < MIN_VOLUME_USD:
            return
        
        spread = ((dex_price - gate_price) / gate_price) * 100
        
        # Фільтр значущого спреду
        if abs(spread) < SPREAD_THRESHOLD:
            return
        
        print(f"{datetime.now()} | 📊 {symbol} | Gate: {gate_price:.6f} | DEX: {dex_price:.6f} | Spread: {spread:.2f}%")
        
        # Виконання торгівлі при значному спреді
        if abs(spread) >= SPREAD_THRESHOLD:
            execute_trade(symbol, gate_price, dex_price, spread)
            
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка арбітражу {symbol}: {e}")
        token_blacklist.add(symbol)  # Додаємо в чорний список

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
        
        # Різні джерела токенів
        if cycle % 3 == 0:
            tokens.extend(get_top_tokens_from_coingecko(20))
        else:
            tokens.extend(get_tokens_from_binance())
        
        # Видаляємо дублікати
        unique_tokens = list(set(tokens))
        
        print(f"{datetime.now()} | 📦 Знайдено {len(unique_tokens)} токенів")
        
        # Перевіряємо арбітраж
        for symbol, price in unique_tokens:
            smart_arbitrage(symbol, price)
            time.sleep(0.3)  # Запобігаємо rate limits
        
        time.sleep(CHECK_INTERVAL)

# -------------------------
# ДОДАТКОВІ КОМАНДИ
# -------------------------
@bot.message_handler(commands=['profit'])
def show_profit(message):
    """Показати прибуток"""
    # Тут можна додати логіку відстеження прибутку
    bot.reply_to(message, "📈 Функція аналізу прибутку в розробці...")

@bot.message_handler(commands=['blacklist'])
def show_blacklist(message):
    """Показати чорний список"""
    bl_list = "\n".join(list(token_blacklist)[:10])
    bot.reply_to(message, f"⚫ Чорний список ({len(token_blacklist)}):\n{bl_list}")

# -------------------------
# ЗАПУСК
# -------------------------
if __name__ == "__main__":
    print(f"{datetime.now()} | 🚀 Запуск арбітражного бота...")
    
    # Перевірка обов'язкових ключів
    required_keys = [API_KEY_TELEGRAM, CHAT_ID, GATE_API_KEY, GATE_API_SECRET]
    if not all(required_keys):
        print(f"{datetime.now()} | ❌ Відсутні обов'язкові API ключі!")
        exit(1)
    
    # Запуск в окремому потоці
    arbitrage_thread = threading.Thread(target=start_arbitrage, daemon=True)
    arbitrage_thread.start()
    
    # Запуск Flask для webhook
    app.run(host="0.0.0.0", port=5000)