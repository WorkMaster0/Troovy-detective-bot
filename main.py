import ccxt
import requests
import time
import os
from datetime import datetime
from flask import Flask, request
import telebot
import threading
import json
from web3 import Web3
import asyncio

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
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))  # Збільшили інтервал

bot = telebot.TeleBot(API_KEY_TELEGRAM)
app = Flask(__name__)

# Ініціалізація бірж
gate = ccxt.gateio({
    "apiKey": GATE_API_KEY,
    "secret": GATE_API_SECRET,
    "options": {"defaultType": "swap"}
})

# Підключення до Web3 провайдерів
WEB3_PROVIDERS = {
    "eth": Web3(Web3.HTTPProvider("https://mainnet.infura.io/v3/f0a02bb1ea5a4005b13ee19da737bbf8")),
    "bsc": Web3(Web3.HTTPProvider("https://bsc-dataseed.binance.org/")),
    "sol": None
}

active_positions = {}
token_blacklist = set()

# -------------------------
# ПОКРАЩЕНИЙ ОТРИМАННЯ ТОКЕНІВ
# -------------------------
def get_top_tokens_from_coingecko(limit=50):
    """Отримання топ токенів з CoinGecko"""
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {
            "vs_currency": "usd",
            "order": "volume_desc",
            "per_page": limit,
            "page": 1,
            "sparkline": False
        }
        
        if COINGECKO_API_KEY:
            headers = {"x-cg-demo-api-key": COINGECKO_API_KEY}
        else:
            headers = {}
            
        response = requests.get(url, params=params, headers=headers, timeout=15)
        if response.status_code == 200:
            tokens = []
            for coin in response.json():
                symbol = coin["symbol"].upper() + "/USDT"
                price = coin["current_price"]
                if price and price > 0:
                    tokens.append((symbol, price))
            return tokens
        return []
    except Exception as e:
        print(f"{datetime.now()} | ❌ CoinGecko помилка: {e}")
        return []

def get_tokens_from_moralis(chain, limit=30):
    """Отримання токенів з Moralis з фільтрацією"""
    url = f"https://deep-index.moralis.io/api/v2.2/erc20/metadata?chain={chain}&limit={limit}"
    headers = {"X-API-Key": MORALIS_API_KEY}
    
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            tokens = []
            for token in response.json():
                symbol = token.get("symbol", "").upper()
                address = token.get("address", "")
                
                if not symbol or not address or symbol in token_blacklist:
                    continue
                    
                # Отримуємо ціну
                price_url = f"https://deep-index.moralis.io/api/v2.2/erc20/{address}/price?chain={chain}"
                price_response = requests.get(price_url, headers=headers, timeout=10)
                
                if price_response.status_code == 200:
                    price_data = price_response.json()
                    usd_price = float(price_data.get("usdPrice", 0))
                    if usd_price > 0.000001:  # Фільтр дуже дешевих токенів
                        tokens.append((f"{symbol}/USDT", usd_price))
            
            return tokens
    except Exception as e:
        print(f"{datetime.now()} | ❌ Moralis {chain} помилка: {e}")
    
    return []

# -------------------------
# ПОКРАЩЕНА ПЕРЕВІРКА ДОСТУПНОСТІ ПАРИ
# -------------------------
def is_pair_available(symbol):
    """Перевірка чи пара доступна на Gate.io"""
    try:
        # Спробуємо різні формати пар
        formats = [
            symbol.replace("/", "_USDT"),
            symbol.replace("/", "/USDT:USDT"),
            symbol.replace("/", "").lower() + "_usdt"
        ]
        
        markets = gate.load_markets()
        for fmt in formats:
            if fmt in markets:
                market = markets[fmt]
                if market['active'] and market['spot']:
                    return True
        return False
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка перевірки пари {symbol}: {e}")
        return False

# -------------------------
# ПОКРАЩЕНИЙ АРБІТРАЖ
# -------------------------
def smart_arbitrage(symbol, dex_price):
    """Розумний арбітраж з перевіркою волатильності"""
    if symbol in active_positions or not is_pair_available(symbol):
        return
        
    try:
        gate_symbol = symbol.replace("/", "_USDT")
        ticker = gate.fetch_ticker(gate_symbol)
        gate_price = ticker['last']
        
        if gate_price == 0 or dex_price == 0:
            return
            
        spread = ((dex_price - gate_price) / gate_price) * 100
        
        # Додаткова перевірка волатильності
        if abs(spread) < SPREAD_THRESHOLD:
            return
            
        print(f"{datetime.now()} | 📊 {symbol} | Gate: {gate_price:.6f} | DEX: {dex_price:.6f} | Spread: {spread:.2f}%")
        
        # Відкриття позиції
        if spread >= SPREAD_THRESHOLD:
            open_position(symbol, "buy", gate_price, dex_price)
        elif spread <= -SPREAD_THRESHOLD:
            open_position(symbol, "sell", gate_price, dex_price)
            
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка арбітражу {symbol}: {e}")
        token_blacklist.add(symbol.split('/')[0])

# -------------------------
# ПОКРАЩЕНЕ ВІДКРИТТЯ ПОЗИЦІЇ
# -------------------------
def open_position(symbol, side, gate_price, dex_price):
    """Відкриття позиції з перевіркою балансу"""
    try:
        balance = gate.fetch_balance()
        usdt_balance = balance['total'].get('USDT', 0)
        
        if usdt_balance < TRADE_AMOUNT_USD:
            print(f"{datetime.now()} | ⚠️ Недостатньо USDT: {usdt_balance:.2f}")
            return
            
        # Розрахунок обсягу
        amount = TRADE_AMOUNT_USD / gate_price
        gate_symbol = symbol.replace("/", "_USDT")
        
        # Створення ордера
        order = gate.create_order(
            symbol=gate_symbol,
            type="market",
            side=side,
            amount=amount
        )
        
        active_positions[symbol] = {
            'side': side,
            'amount': amount,
            'entry_price': gate_price,
            'dex_price': dex_price,
            'timestamp': datetime.now()
        }
        
        msg = f"✅ ВІДКРИТО {side.upper()} {amount:.4f} {symbol}\n💵 Ціна: {gate_price:.6f}\n📊 Spread: {((dex_price - gate_price)/gate_price*100):.2f}%"
        print(f"{datetime.now()} | {msg}")
        bot.send_message(CHAT_ID, msg)
        
        # Плануємо закриття
        threading.Timer(300, close_position, args=[symbol]).start()
        
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка відкриття позиції {symbol}: {e}")

# -------------------------
# ПОКРАЩЕНЕ ЗАКРИТТЯ ПОЗИЦІЇ
# -------------------------
def close_position(symbol):
    """Закриття позиції з перевіркою прибутку"""
    if symbol not in active_positions:
        return
        
    position = active_positions[symbol]
    
    try:
        gate_symbol = symbol.replace("/", "_USDT")
        ticker = gate.fetch_ticker(gate_symbol)
        current_price = ticker['last']
        
        # Розрахунок PnL
        if position['side'] == 'buy':
            pnl = (current_price - position['entry_price']) / position['entry_price'] * 100
        else:
            pnl = (position['entry_price'] - current_price) / position['entry_price'] * 100
            
        close_side = "sell" if position['side'] == "buy" else "buy"
        
        order = gate.create_order(
            symbol=gate_symbol,
            type="market",
            side=close_side,
            amount=position['amount'],
            params={"reduceOnly": True}
        )
        
        msg = f"🎯 ЗАКРИТО {symbol}\n📈 PnL: {pnl:.2f}%\n💵 Ціна: {current_price:.6f}"
        print(f"{datetime.now()} | {msg}")
        bot.send_message(CHAT_ID, msg)
        
        del active_positions[symbol]
        
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка закриття позиції {symbol}: {e}")

# -------------------------
# НОВІ ФУНКЦІЇ МОНІТОРИНГУ
# -------------------------
def monitor_balances():
    """Моніторинг балансів"""
    while True:
        try:
            balance = gate.fetch_balance()
            total_usdt = balance['total'].get('USDT', 0)
            msg = f"💰 Баланс: {total_usdt:.2f} USDT\n📊 Активних позицій: {len(active_positions)}"
            bot.send_message(CHAT_ID, msg)
            time.sleep(3600)  # Кожну годину
        except Exception as e:
            print(f"{datetime.now()} | ❌ Помилка моніторингу балансу: {e}")
            time.sleep(300)

def health_check():
    """Перевірка здоров'я системи"""
    while True:
        try:
            # Перевірка підключення до бірж
            gate.fetch_time()
            
            # Перевірка API ключів
            balance = gate.fetch_balance()
            
            msg = "✅ Система працює нормально\n"
            msg += f"💰 Баланс: {balance['total'].get('USDT', 0):.2f} USDT\n"
            msg += f"📊 Активних позицій: {len(active_positions)}"
            
            bot.send_message(CHAT_ID, msg)
            time.sleep(7200)  #Кожні 2 години
            
        except Exception as e:
            error_msg = f"❌ Проблема з системою: {e}"
            print(f"{datetime.now()} | {error_msg}")
            bot.send_message(CHAT_ID, error_msg)
            time.sleep(300)

# -------------------------
# ОСНОВНИЙ ЦИКЛ АРБІТРАЖУ
# -------------------------
def start_arbitrage():
    """Основний цикл арбітражу"""
    bot.send_message(CHAT_ID, "🚀 Бот запущено. Починаю моніторинг...")
    
    # Запуск додаткових моніторів
    threading.Thread(target=monitor_balances, daemon=True).start()
    threading.Thread(target=health_check, daemon=True).start()
    
    cycle = 0
    while True:
        cycle += 1
        print(f"{datetime.now()} | 🔄 Цикл {cycle}")
        
        # Отримуємо токени з різних джерел
        tokens = []
        
        # Спосіб 1: CoinGecko (найнадійніший)
        tokens.extend(get_top_tokens_from_coingecko(30))
        
        # Спосіб 2: Moralis для різних мереж
        chains = ["eth", "bsc", "polygon"]
        for chain in chains:
            chain_tokens = get_tokens_from_moralis(chain, 20)
            tokens.extend(chain_tokens)
            time.sleep(1)  # Затримка між запитами
        
        # Видаляємо дублікати
        unique_tokens = list(set(tokens))
        
        print(f"{datetime.now()} | 📦 Знайдено {len(unique_tokens)} унікальних токенів")
        
        if not unique_tokens:
            bot.send_message(CHAT_ID, "⚠️ Не вдалося отримати токени. Перевірте API ключі.")
            time.sleep(300)
            continue
        
        # Перевіряємо арбітраж для кожного токена
        for symbol, price in unique_tokens:
            smart_arbitrage(symbol, price)
            time.sleep(0.5)  # Затримка між перевірками
        
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
                         "/positions - Активні позиції\n"
                         "/stop - Зупинити бота")

@bot.message_handler(commands=['status'])
def send_status(message):
    """Статус системи"""
    try:
        balance = gate.fetch_balance()
        msg = f"✅ Система працює\n💰 Баланс: {balance['total'].get('USDT', 0):.2f} USDT\n"
        msg += f"📊 Активних позицій: {len(active_positions)}\n"
        msg += f"⚫ Чорний список: {len(token_blacklist)} токенів"
        bot.reply_to(message, msg)
    except Exception as e:
        bot.reply_to(message, f"❌ Помилка: {e}")

@bot.message_handler(commands=['balance'])
def send_balance(message):
    """Баланс"""
    try:
        balance = gate.fetch_balance()
        usdt = balance['total'].get('USDT', 0)
        bot.reply_to(message, f"💰 Баланс: {usdt:.2f} USDT")
    except Exception as e:
        bot.reply_to(message, f"❌ Помилка: {e}")

@bot.message_handler(commands=['positions'])
def send_positions(message):
    """Активні позиції"""
    if not active_positions:
        bot.reply_to(message, "📭 Немає активних позицій")
        return
        
    msg = "📊 Активні позиції:\n\n"
    for symbol, pos in active_positions.items():
        msg += f"• {symbol} {pos['side'].upper()} {pos['amount']:.4f}\n"
    bot.reply_to(message, msg)

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
    setup_webhook()
    threading.Thread(target=start_arbitrage, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)