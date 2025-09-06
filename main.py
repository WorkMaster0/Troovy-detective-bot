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

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

TRADE_AMOUNT_USD = float(os.getenv("TRADE_AMOUNT_USD", 50))
SPREAD_THRESHOLD = float(os.getenv("SPREAD_THRESHOLD", 0.8))  # Зменшили для частіших знахідок
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 30))  # Частіші перевірки
LEVERAGE = int(os.getenv("LEVERAGE", 3))

bot = telebot.TeleBot(API_KEY_TELEGRAM)
app = Flask(__name__)

# Ініціалізація бірж
exchanges = {}

try:
    # Gate.io Futures
    exchanges['gate'] = ccxt.gateio({
        "apiKey": GATE_API_KEY,
        "secret": GATE_API_SECRET,
        "options": {"defaultType": "swap"}
    })
    exchanges['gate'].load_markets()
    print(f"{datetime.now()} | ✅ Успішно підключено до Gate.io Futures")
except Exception as e:
    print(f"{datetime.now()} | ❌ Помилка підключення до Gate.io Futures: {e}")

try:
    # Binance Futures (як джерело цін)
    exchanges['binance'] = ccxt.binance({
        "options": {"defaultType": "future"}
    })
    exchanges['binance'].load_markets()
    print(f"{datetime.now()} | ✅ Успішно підключено до Binance Futures")
except Exception as e:
    print(f"{datetime.now()} | ❌ Помилка підключення до Binance Futures: {e}")

active_positions = {}
token_blacklist = set()
last_arbitrage_found = 0

# -------------------------
# ПОКРАЩЕНЕ ОТРИМАННЯ ЦІН
# -------------------------
def get_futures_prices(exchange_name: str, limit: int = 20) -> Dict[str, float]:
    """Отримання цін ф'ючерсів з біржі"""
    prices = {}
    try:
        exchange = exchanges.get(exchange_name)
        if not exchange:
            return prices
            
        markets = exchange.load_markets()
        count = 0
        
        for symbol, market in markets.items():
            if (market.get('swap', False) and market.get('active', False) and 
                symbol.endswith('/USDT:USDT') and count < limit):
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    if ticker['last'] and ticker['last'] > 0:
                        clean_symbol = symbol.replace(':USDT', '').replace('/USDT', '')
                        prices[clean_symbol] = ticker['last']
                        count += 1
                except:
                    continue
                    
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка отримання цін з {exchange_name}: {e}")
    
    return prices

def get_top_volatile_tokens() -> List[str]:
    """Отримання топ волатильних токенів"""
    try:
        # Використовуємо Binance для пошуку волатильних пар
        url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
        response = requests.get(url, timeout=10)
        
        if response.status_code == 200:
            tickers = response.json()
            # Сортуємо за об'ємом та зміною ціни
            volatile_tokens = []
            for ticker in tickers:
                symbol = ticker['symbol'].replace('USDT', '')
                price_change = float(ticker['priceChangePercent'])
                volume = float(ticker['volume'])
                
                if abs(price_change) > 2.0 and volume > 1000000:  # Фільтр волатильності
                    volatile_tokens.append(symbol)
                    if len(volatile_tokens) >= 15:
                        break
            
            return volatile_tokens
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка отримання волатильних токенів: {e}")
    
    return ['BTC', 'ETH', 'SOL', 'XRP', 'ADA', 'DOT', 'LINK', 'AVAX', 'MATIC', 'DOGE']

# -------------------------
# Ф'ЮЧЕРСНА ТОРГОВА ЛОГІКА
# -------------------------
def calculate_futures_amount(symbol: str, price: float) -> float:
    """Розрахунок кількості контрактів"""
    try:
        market = exchanges['gate'].market(symbol + '/USDT:USDT')
        contract_size = float(market['contractSize'])
        amount = (TRADE_AMOUNT_USD * LEVERAGE) / (price * contract_size)
        return round(amount, market['precision']['amount'])
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка розрахунку кількості {symbol}: {e}")
        return 0

def execute_futures_trade(symbol: str, gate_price: float, binance_price: float, spread: float):
    """Виконання торгової операції"""
    try:
        futures_symbol = symbol + '/USDT:USDT'
        
        # Встановлюємо плече
        exchanges['gate'].set_leverage(LEVERAGE, futures_symbol)
        
        # Розраховуємо кількість
        amount = calculate_futures_amount(symbol, gate_price)
        if amount <= 0:
            return
        
        if spread > 0:  # Binance ціна вища - купуємо на Gate
            order = exchanges['gate'].create_market_buy_order(futures_symbol, amount)
            side = "LONG"
        else:  # Gate ціна вища - продаємо на Gate
            order = exchanges['gate'].create_market_sell_order(futures_symbol, amount)
            side = "SHORT"
        
        msg = f"🎯 {side} {symbol}\n"
        msg += f"Ціна Gate: ${gate_price:.4f}\n"
        msg += f"Ціна Binance: ${binance_price:.4f}\n"
        msg += f"Spread: {abs(spread):.2f}%\n"
        msg += f"Кількість: {amount:.4f} контрактів\n"
        msg += f"Плече: {LEVERAGE}x"
        
        bot.send_message(CHAT_ID, msg)
        print(f"{datetime.now()} | {msg}")
        
        # Додаємо в активні позиції
        active_positions[symbol] = {
            'side': side.lower(),
            'entry_price': gate_price,
            'amount': amount,
            'timestamp': datetime.now(),
            'spread': spread
        }
        
        global last_arbitrage_found
        last_arbitrage_found = time.time()
        
    except Exception as e:
        error_msg = f"❌ ПОМИЛКА торгівлі {symbol}: {e}"
        bot.send_message(CHAT_ID, error_msg)
        print(f"{datetime.now()} | {error_msg}")

# -------------------------
# ПОШУК АРБІТРАЖУ МІЖ БІРЖАМИ
# -------------------------
def find_arbitrage_opportunities():
    """Пошук арбітражних можливостей між біржами"""
    opportunities = []
    
    try:
        # Отримуємо ціни з обох бірж
        gate_prices = get_futures_prices('gate', 30)
        binance_prices = get_futures_prices('binance', 30)
        
        # Шукаємо спільні токени
        common_symbols = set(gate_prices.keys()) & set(binance_prices.keys())
        
        for symbol in common_symbols:
            if symbol in active_positions or symbol in token_blacklist:
                continue
                
            gate_price = gate_prices[symbol]
            binance_price = binance_prices[symbol]
            
            if gate_price == 0 or binance_price == 0:
                continue
                
            spread = ((binance_price - gate_price) / gate_price) * 100
            
            # Фільтр спреду та об'єму
            if abs(spread) >= SPREAD_THRESHOLD:
                opportunities.append((symbol, gate_price, binance_price, spread))
    
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка пошуку арбітражу: {e}")
    
    return opportunities

# -------------------------
# ОСНОВНИЙ ЦИКЛ АРБІТРАЖУ
# -------------------------
def start_futures_arbitrage():
    """Основний цикл арбітражу"""
    bot.send_message(CHAT_ID, "🚀 Ф'ючерсний арбітражний бот запущено!")
    
    cycle = 0
    while True:
        cycle += 1
        
        try:
            balance = exchanges['gate'].fetch_balance()['USDT']['total']
            print(f"{datetime.now()} | 🔄 Цикл {cycle} | Баланс: ${balance:.2f}")
            
            # Шукаємо арбітражні можливості
            opportunities = find_arbitrage_opportunities()
            
            if opportunities:
                print(f"{datetime.now()} | 📊 Знайдено {len(opportunities)} арбітражних можливостей")
                
                # Сортуємо за найбільшим спредом
                opportunities.sort(key=lambda x: abs(x[3]), reverse=True)
                
                # Обробляємо топ 3 можливості
                for symbol, gate_price, binance_price, spread in opportunities[:3]:
                    print(f"{datetime.now()} | 💡 {symbol}: Spread {spread:.2f}%")
                    execute_futures_trade(symbol, gate_price, binance_price, spread)
                    time.sleep(1)  # Запобігаємо rate limit
            else:
                print(f"{datetime.now()} | 🔍 Арбітражні можливості не знайдені")
                
                # Кожні 10 циклів шукаємо волатильні токени
                if cycle % 10 == 0:
                    volatile_tokens = get_top_volatile_tokens()
                    print(f"{datetime.now()} | 🌪️ Волатильні токени: {volatile_tokens}")
            
        except Exception as e:
            print(f"{datetime.now()} | ❌ Помилка в головному циклі: {e}")
        
        time.sleep(CHECK_INTERVAL)

# -------------------------
# TELEGRAM КОМАНДИ
# -------------------------
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "🤖 Ф'ючерсний арбітражний бот активовано!")

@bot.message_handler(commands=['arbitrage'])
def find_arbitrage_now(message):
    """Миттєвий пошук арбітражу"""
    opportunities = find_arbitrage_opportunities()
    
    if opportunities:
        msg = "🎯 Знайдені арбітражі:\n\n"
        for symbol, gate_price, binance_price, spread in opportunities[:5]:
            msg += f"• {symbol}: {spread:.2f}%\n"
            msg += f"  Gate: ${gate_price:.4f} | Binance: ${binance_price:.4f}\n\n"
    else:
        msg = "🔍 Арбітражі не знайдені"
    
    bot.reply_to(message, msg)

@bot.message_handler(commands=['tokens'])
def show_tokens(message):
    """Показати доступні токени"""
    try:
        gate_prices = get_futures_prices('gate', 10)
        binance_prices = get_futures_prices('binance', 10)
        
        msg = "📊 Доступні токени:\n\n"
        for symbol in list(gate_prices.keys())[:10]:
            gate_price = gate_prices.get(symbol, 0)
            binance_price = binance_prices.get(symbol, 0)
            spread = ((binance_price - gate_price) / gate_price) * 100 if gate_price else 0
            
            msg += f"• {symbol}: {spread:+.2f}%\n"
        
        bot.reply_to(message, msg)
    except Exception as e:
        bot.reply_to(message, f"❌ Помилка: {e}")

# -------------------------
# ЗАПУСК
# -------------------------
if __name__ == "__main__":
    print(f"{datetime.now()} | 🚀 Запуск арбітражного бота...")
    
    # Запускаємо арбітраж в окремому потоці
    arbitrage_thread = threading.Thread(target=start_futures_arbitrage, daemon=True)
    arbitrage_thread.start()
    
    print(f"{datetime.now()} | ✅ Бот запущено. Очікую команди...")
    
    # Запускаємо polling для Telegram
    bot.remove_webhook()
    bot.polling(none_stop=True, timeout=60)