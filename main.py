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

TRADE_AMOUNT_USD = float(os.getenv("TRADE_AMOUNT_USD", 50))  # Збільшили для ф'ючерсів
SPREAD_THRESHOLD = float(os.getenv("SPREAD_THRESHOLD", 1.5))  # Менший спред для ф'ючерсів
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))  # Частіші перевірки
LEVERAGE = int(os.getenv("LEVERAGE", 3))  # Кредитне плече

bot = telebot.TeleBot(API_KEY_TELEGRAM)
app = Flask(__name__)

# Ініціалізація біржі для ф'ючерсів
try:
    gate = ccxt.gateio({
        "apiKey": GATE_API_KEY,
        "secret": GATE_API_SECRET,
        "options": {
            "defaultType": "swap",  # Ф'ючерси
            "adjustForTimeDifference": True
        }
    })
    gate.load_markets()
    print(f"{datetime.now()} | ✅ Успішно підключено до Gate.io Futures")
except Exception as e:
    print(f"{datetime.now()} | ❌ Помилка підключення до Gate.io Futures: {e}")
    gate = None

active_positions = {}
token_blacklist = set()
coingecko_last_call = 0

# -------------------------
# Ф'ЮЧЕРСНІ ФУНКЦІЇ
# -------------------------
def set_leverage(symbol: str, leverage: int = LEVERAGE):
    """Встановлення кредитного плеча"""
    try:
        gate.set_leverage(leverage, symbol)
        print(f"{datetime.now()} | ⚙️ Встановлено плече {leverage}x для {symbol}")
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка встановлення плеча {symbol}: {e}")

def get_futures_balance() -> float:
    """Отримання балансу ф'ючерсного рахунку"""
    try:
        balance = gate.fetch_balance()
        return balance['USDT']['total']
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка отримання балансу: {e}")
        return 0

def get_futures_positions():
    """Отримання поточних позицій"""
    try:
        positions = gate.fetch_positions()
        return [p for p in positions if p['contracts'] > 0]
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка отримання позицій: {e}")
        return []

# -------------------------
# ОТРИМАННЯ ТОКЕНІВ ДЛЯ Ф'ЮЧЕРСІВ
# -------------------------
def get_top_futures_tokens(limit=20) -> List[Tuple[str, float]]:
    """Отримання топ токенів для ф'ючерсів"""
    try:
        # Отримуємо маркети та фільтруємо ф'ючерси
        markets = gate.load_markets()
        futures_markets = []
        
        for symbol, market in markets.items():
            if market['swap'] and market['active'] and symbol.endswith('/USDT:USDT'):
                # Перетворюємо формат: BTC/USDT:USDT -> BTC/USDT
                clean_symbol = symbol.replace(':USDT', '')
                try:
                    ticker = gate.fetch_ticker(symbol)
                    if ticker['last'] and ticker['last'] > 0:
                        futures_markets.append((clean_symbol, ticker['last']))
                        if len(futures_markets) >= limit:
                            break
                except:
                    continue
        
        return futures_markets
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка отримання ф'ючерсних токенів: {e}")
        return []

def get_futures_tokens_from_coingecko(limit=15) -> List[Tuple[str, float]]:
    """Отримання токенів з CoinGecko для ф'ючерсів"""
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
                    tokens.append((f"{symbol}/USDT", price))
            return tokens
        return []
    except Exception as e:
        print(f"{datetime.now()} | ❌ CoinGecko помилка: {e}")
        return []

# -------------------------
# Ф'ЮЧЕРСНА ТОРГОВА ЛОГІКА
# -------------------------
def calculate_futures_amount(symbol: str, price: float) -> float:
    """Розрахунок кількості контрактів для ф'ючерсів"""
    try:
        # Отримуємо інформацію про маркет
        market = gate.market(symbol + ':USDT')
        contract_size = float(market['contractSize'])
        
        # Розраховуємо кількість контрактів
        amount = (TRADE_AMOUNT_USD * LEVERAGE) / (price * contract_size)
        return round(amount, market['precision']['amount'])
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка розрахунку кількості: {e}")
        return 0

def execute_futures_trade(symbol: str, gate_price: float, dex_price: float, spread: float):
    """Виконання ф'ючерсної торгової операції"""
    try:
        futures_symbol = symbol + ':USDT'
        
        # Встановлюємо плече
        set_leverage(futures_symbol, LEVERAGE)
        
        # Розраховуємо кількість
        amount = calculate_futures_amount(symbol, gate_price)
        if amount <= 0:
            return
        
        if spread > 0:  # DEX ціна вища - купуємо ф'ючерси
            order = gate.create_market_buy_order(futures_symbol, amount)
            
            msg = f"✅ LONG {symbol}\n"
            msg += f"Кількість: {amount:.4f} контрактів\n"
            msg += f"Ціна: ${gate_price:.4f}\n"
            msg += f"Плече: {LEVERAGE}x\n"
            msg += f"Spread: {spread:.2f}%"
            
        else:  # Gate ціна вища - продаємо ф'ючерси
            order = gate.create_market_sell_order(futures_symbol, amount)
            
            msg = f"✅ SHORT {symbol}\n"
            msg += f"Кількість: {amount:.4f} контрактів\n"
            msg += f"Ціна: ${gate_price:.4f}\n"
            msg += f"Плече: {LEVERAGE}x\n"
            msg += f"Spread: {spread:.2f}%"
        
        bot.send_message(CHAT_ID, msg)
        print(f"{datetime.now()} | {msg}")
        
        # Додаємо в активні позиції
        active_positions[symbol] = {
            'side': 'long' if spread > 0 else 'short',
            'entry_price': gate_price,
            'amount': amount,
            'timestamp': datetime.now()
        }
        
    except Exception as e:
        error_msg = f"❌ ПОМИЛКА торгівлі {symbol}: {e}"
        bot.send_message(CHAT_ID, error_msg)
        print(f"{datetime.now()} | {error_msg}")

# -------------------------
# ПОКРАЩЕНИЙ АРБІТРАЖ ДЛЯ Ф'ЮЧЕРСІВ
# -------------------------
def smart_futures_arbitrage(symbol: str, dex_price: float):
    """Розумний арбітраж для ф'ючерсів"""
    if not gate or symbol in active_positions or symbol in token_blacklist:
        return
    
    try:
        futures_symbol = symbol + ':USDT'
        
        # Перевірка доступності пари
        markets = gate.load_markets()
        if futures_symbol not in markets or not markets[futures_symbol].get('active', False):
            return
        
        # Отримуємо ціну ф'ючерсів
        ticker = gate.fetch_ticker(futures_symbol)
        gate_price = ticker['last']
        volume = ticker['quoteVolume']
        
        if gate_price == 0 or dex_price == 0 or volume < 10000:
            return
        
        # Розраховуємо спред
        spread = ((dex_price - gate_price) / gate_price) * 100
        
        # Фільтр значущого спреду
        if abs(spread) < SPREAD_THRESHOLD:
            return
        
        print(f"{datetime.now()} | 📊 {symbol} | Futures: {gate_price:.6f} | Spot: {dex_price:.6f} | Spread: {spread:.2f}%")
        
        # Виконання торгівлі при значному спреді
        if abs(spread) >= SPREAD_THRESHOLD:
            execute_futures_trade(symbol, gate_price, dex_price, spread)
            
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка ф'ючерсного арбітражу {symbol}: {e}")
        token_blacklist.add(symbol)

# -------------------------
# МОНІТОРИНГ ПОЗИЦІЙ
# -------------------------
def monitor_positions():
    """Моніторинг та закриття позицій"""
    while True:
        try:
            current_prices = {}
            
            # Перевіряємо ціни для активних позицій
            for symbol in list(active_positions.keys()):
                try:
                    ticker = gate.fetch_ticker(symbol + ':USDT')
                    current_prices[symbol] = ticker['last']
                    
                    position = active_positions[symbol]
                    entry_price = position['entry_price']
                    current_price = ticker['last']
                    
                    # Розраховуємо PnL
                    if position['side'] == 'long':
                        pnl_percent = ((current_price - entry_price) / entry_price) * 100 * LEVERAGE
                    else:
                        pnl_percent = ((entry_price - current_price) / entry_price) * 100 * LEVERAGE
                    
                    # Автоматичне закриття при досягненні цілі
                    if abs(pnl_percent) >= 5:  # 5% прибуток/збиток
                        close_position(symbol, current_price, pnl_percent)
                        
                except Exception as e:
                    print(f"{datetime.now()} | ❌ Помилка моніторингу {symbol}: {e}")
            
            time.sleep(30)  # Перевіряємо кожні 30 секунд
            
        except Exception as e:
            print(f"{datetime.now()} | ❌ Помилка моніторингу позицій: {e}")
            time.sleep(60)

def close_position(symbol: str, current_price: float, pnl_percent: float):
    """Закриття позиції"""
    try:
        futures_symbol = symbol + ':USDT'
        position = active_positions[symbol]
        
        if position['side'] == 'long':
            order = gate.create_market_sell_order(futures_symbol, position['amount'])
        else:
            order = gate.create_market_buy_order(futures_symbol, position['amount'])
        
        # Видаляємо з активних позицій
        del active_positions[symbol]
        
        msg = f"🔒 ЗАКРИТО {symbol}\n"
        msg += f"Сторона: {position['side']}\n"
        msg += f"PnL: {pnl_percent:.2f}%\n"
        msg += f"Ціна закриття: ${current_price:.4f}"
        
        bot.send_message(CHAT_ID, msg)
        print(f"{datetime.now()} | {msg}")
        
    except Exception as e:
        error_msg = f"❌ ПОМИЛКА закриття {symbol}: {e}"
        bot.send_message(CHAT_ID, error_msg)
        print(f"{datetime.now()} | {error_msg}")

# -------------------------
# ОСНОВНИЙ ЦИКЛ АРБІТРАЖУ
# -------------------------
def start_futures_arbitrage():
    """Основний цикл арбітражу для ф'ючерсів"""
    bot.send_message(CHAT_ID, "🚀 Ф'ючерсний бот запущено!")
    
    # Запускаємо моніторинг позицій в окремому потоці
    monitoring_thread = threading.Thread(target=monitor_positions, daemon=True)
    monitoring_thread.start()
    
    cycle = 0
    while True:
        cycle += 1
        
        # Отримуємо баланс
        balance = get_futures_balance()
        print(f"{datetime.now()} | 🔄 Цикл {cycle} | Баланс: ${balance:.2f}")
        
        # Отримуємо токени для арбітражу
        tokens = get_top_futures_tokens(25)
        if not tokens:
            tokens = get_futures_tokens_from_coingecko(20)
        
        print(f"{datetime.now()} | 📦 Знайдено {len(tokens)} ф'ючерсних токенів")
        
        # Перевіряємо арбітраж
        for symbol, price in tokens:
            smart_futures_arbitrage(symbol, price)
            time.sleep(0.2)
        
        time.sleep(CHECK_INTERVAL)

# -------------------------
# TELEGRAM КОМАНДИ ДЛЯ Ф'ЮЧЕРСІВ
# -------------------------
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "🤖 Ф'ючерсний арбітражний бот активовано!\n\n"
                         "Доступні команди:\n"
                         "/status - Статус системи\n"
                         "/balance - Баланс\n"
                         "/positions - Поточні позиції\n"
                         "/leverage - Змінити плече")

@bot.message_handler(commands=['positions'])
def show_positions(message):
    """Показати поточні позиції"""
    if not active_positions:
        bot.reply_to(message, "📭 Немає активних позицій")
        return
    
    msg = "📊 Активні позиції:\n\n"
    for symbol, position in active_positions.items():
        msg += f"• {symbol} {position['side'].upper()}\n"
        msg += f"  Вхід: ${position['entry_price']:.4f}\n"
        msg += f"  Кількість: {position['amount']:.4f}\n"
        msg += f"  Час: {position['timestamp'].strftime('%H:%M:%S')}\n\n"
    
    bot.reply_to(message, msg)

@bot.message_handler(commands=['balance'])
def show_balance(message):
    """Показати баланс"""
    balance = get_futures_balance()
    positions = get_futures_positions()
    
    msg = f"💰 Баланс: ${balance:.2f}\n"
    msg += f"📊 Позицій: {len(positions)}\n"
    msg += f"⚫ Чорний список: {len(token_blacklist)} токенів"
    
    bot.reply_to(message, msg)

# -------------------------
# ЗАПУСК
# -------------------------
if __name__ == "__main__":
    print(f"{datetime.now()} | 🚀 Запуск ф'ючерсного арбітражного бота...")
    
    # Перевірка обов'язкових ключів
    required_keys = [API_KEY_TELEGRAM, CHAT_ID, GATE_API_KEY, GATE_API_SECRET]
    if not all(required_keys):
        print(f"{datetime.now()} | ❌ Відсутні обов'язкові API ключі!")
        exit(1)
    
    # Запуск арбітражу в окремому потоці
    arbitrage_thread = threading.Thread(target=start_futures_arbitrage, daemon=True)
    arbitrage_thread.start()
    
    # Запуск Flask для webhook
    app.run(host="0.0.0.0", port=5000)