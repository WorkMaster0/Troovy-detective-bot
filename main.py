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
# Налаштування
# -------------------------
API_KEY_TELEGRAM = os.getenv("API_KEY_TELEGRAM")
CHAT_ID = os.getenv("CHAT_ID")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = WEBHOOK_HOST + WEBHOOK_PATH

GATE_API_KEY = os.getenv("GATE_API_KEY")
GATE_API_SECRET = os.getenv("GATE_API_SECRET")

TRADE_AMOUNT_USD = float(os.getenv("TRADE_AMOUNT_USD", 50))
SPREAD_THRESHOLD = float(os.getenv("SPREAD_THRESHOLD", 1.0))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))
LEVERAGE = int(os.getenv("LEVERAGE", 3))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", 3))

bot = telebot.TeleBot(API_KEY_TELEGRAM)
app = Flask(__name__)

# Ініціалізація біржі
try:
    exchange = ccxt.gateio({
        "apiKey": GATE_API_KEY,
        "secret": GATE_API_SECRET,
        "options": {"defaultType": "swap"}
    })
    exchange.load_markets()
    print(f"{datetime.now()} | ✅ Успішно підключено до Gate.io Futures")
except Exception as e:
    print(f"{datetime.now()} | ❌ Помилка підключення: {e}")
    exchange = None

active_positions = {}
trade_history = []
profit_loss = 0.0

# -------------------------
# СПРАВЖНІЙ АРБІТРАЖ: Ф'ЮЧЕРСИ vs СПОТ
# -------------------------
def get_gateio_futures_prices(symbols: List[str] = None) -> Dict[str, float]:
    """Отримання цін ф'ючерсів з Gate.io"""
    prices = {}
    if not exchange:
        return prices
        
    try:
        if not symbols:
            # Топ-20 ліквідних пар
            symbols = ['BTC', 'ETH', 'SOL', 'XRP', 'ADA', 'AVAX', 'DOT', 'LINK', 'MATIC', 'DOGE',
                      'BNB', 'ATOM', 'LTC', 'OP', 'ARB', 'FIL', 'APT', 'NEAR', 'ALGO', 'XLM']
        
        for symbol in symbols:
            try:
                ticker = exchange.fetch_ticker(f"{symbol}/USDT:USDT")
                if ticker and ticker['last'] and ticker['last'] > 0:
                    prices[symbol] = ticker['last']
            except:
                continue
                
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка отримання ф'ючерсних цін: {e}")
    
    return prices

def get_gateio_spot_prices(symbols: List[str] = None) -> Dict[str, float]:
    """Отримання спотових цін з Gate.io (для арбітражу)"""
    prices = {}
    
    try:
        # Використовуємо CoinGecko для спотових цін
        url = "https://api.coingecko.com/api/v3/simple/price"
        if symbols:
            ids = ",".join([f"{s.lower()}" for s in symbols if s != 'USDT'])
        else:
            ids = "bitcoin,ethereum,solana,ripple,cardano,avalanche-2,polkadot,chainlink,polygon,dogecoin"
        
        params = {
            "ids": ids,
            "vs_currencies": "usd"
        }
        
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            for coin_id, price_data in data.items():
                symbol = coin_id.upper().replace("-", "")
                if symbol == "AVALANCHE2":
                    symbol = "AVAX"
                prices[symbol] = price_data['usd']
                
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка отримання спотових цін: {e}")
    
    return prices

def calculate_real_spread(futures_price: float, spot_price: float) -> float:
    """Розрахунок реального спреду між ф'ючерсами і спотом"""
    if not futures_price or not spot_price or spot_price == 0:
        return 0
    return ((futures_price - spot_price) / spot_price) * 100

# -------------------------
# ТОРГОВА ЛОГІКА
# -------------------------
def calculate_futures_amount(symbol: str, price: float) -> float:
    """Розрахунок кількості контрактів"""
    try:
        market = exchange.market(f"{symbol}/USDT:USDT")
        contract_size = float(market['contractSize'])
        
        if price <= 0 or contract_size <= 0:
            return 0
            
        amount = (TRADE_AMOUNT_USD * LEVERAGE) / (price * contract_size)
        precision = int(market['precision']['amount'])
        
        # Перевірка мінімальної кількості
        min_amount = float(market['limits']['amount']['min'])
        if amount < min_amount:
            print(f"{datetime.now()} | ⚠️ Кількість {amount} менша за мінімум {min_amount}")
            return 0
            
        return round(amount, precision)
        
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка розрахунку кількості {symbol}: {e}")
        return 0

def execute_trade_based_on_premium(symbol: str, futures_price: float, spot_price: float, spread: float):
    """Торгівля на основі премії ф'ючерсів"""
    try:
        if len(active_positions) >= MAX_POSITIONS:
            return
            
        amount = calculate_futures_amount(symbol, futures_price)
        if amount <= 0:
            return
        
        futures_symbol = f"{symbol}/USDT:USDT"
        
        # Встановлюємо плече
        exchange.set_leverage(LEVERAGE, futures_symbol)
        
        if spread > 0:  # Ф'ючерси дорожчі (премія) - продаємо
            order = exchange.create_market_sell_order(futures_symbol, amount)
            side = "SHORT"
            reason = "Ф'ючерси дорожчі за спот"
        else:  # Ф'ючерси дешевші (дисконт) - купуємо
            order = exchange.create_market_buy_order(futures_symbol, amount)
            side = "LONG"  
            reason = "Ф'ючерси дешевші за спот"
        
        # Зберігаємо інформацію
        trade_info = {
            'symbol': symbol,
            'side': side,
            'futures_price': futures_price,
            'spot_price': spot_price,
            'spread': spread,
            'amount': amount,
            'timestamp': datetime.now(),
            'order_id': order['id'],
            'reason': reason
        }
        trade_history.append(trade_info)
        active_positions[symbol] = trade_info
        
        msg = f"🎯 {side} {symbol}\n"
        msg += f"📈 Причина: {reason}\n"
        msg += f"💰 Ф'ючерс: ${futures_price:.6f}\n"
        msg += f"💰 Спот: ${spot_price:.6f}\n"
        msg += f"📊 Спред: {spread:+.2f}%\n"
        msg += f"📦 Кількість: {amount:.6f}\n"
        msg += f"⚖️ Плече: {LEVERAGE}x\n"
        msg += f"🆔 Order: {order['id']}"
        
        bot.send_message(CHAT_ID, msg)
        print(f"{datetime.now()} | {msg}")
        
    except Exception as e:
        error_msg = f"❌ ПОМИЛКА торгівлі {symbol}: {e}"
        bot.send_message(CHAT_ID, error_msg)
        print(f"{datetime.now()} | {error_msg}")

# -------------------------
# ПОШУК РЕАЛЬНИХ АРБІТРАЖНИХ МОЖЛИВОСТЕЙ
# -------------------------
def find_real_arbitrage_opportunities():
    """Пошук реальних арбітражних можливостей"""
    opportunities = []
    
    try:
        # Топ-15 ліквідних токенів
        top_symbols = ['BTC', 'ETH', 'SOL', 'XRP', 'ADA', 'AVAX', 'DOT', 'LINK', 'MATIC', 'DOGE', 'BNB', 'ATOM', 'LTC', 'OP', 'ARB']
        
        # Отримуємо ціни
        futures_prices = get_gateio_futures_prices(top_symbols)
        spot_prices = get_gateio_spot_prices(top_symbols)
        
        for symbol in top_symbols:
            if symbol in active_positions:
                continue
                
            futures_price = futures_prices.get(symbol)
            spot_price = spot_prices.get(symbol)
            
            if not futures_price or not spot_price or spot_price == 0:
                continue
                
            spread = calculate_real_spread(futures_price, spot_price)
            
            # Шукаємо реальні арбітражі (1-10% спред)
            if abs(spread) >= SPREAD_THRESHOLD and abs(spread) <= 10.0:
                opportunities.append((symbol, futures_price, spot_price, spread))
    
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка пошуку арбітражу: {e}")
    
    return opportunities

# -------------------------
# МОНІТОРИНГ ТА ЗАКРИТТЯ
# -------------------------
def monitor_positions():
    """Моніторинг позицій"""
    while True:
        try:
            for symbol in list(active_positions.keys()):
                try:
                    position = active_positions[symbol]
                    ticker = exchange.fetch_ticker(f"{symbol}/USDT:USDT")
                    current_price = ticker['last']
                    entry_price = position['futures_price']
                    
                    # Розраховуємо PnL
                    if position['side'] == 'LONG':
                        pnl_percent = ((current_price - entry_price) / entry_price) * 100 * LEVERAGE
                    else:
                        pnl_percent = ((entry_price - current_price) / entry_price) * 100 * LEVERAGE
                    
                    # Закриття при досягненні цілі
                    if abs(pnl_percent) >= 3.0:  # 3% прибуток/збиток
                        close_position(symbol, current_price, pnl_percent)
                        
                except Exception as e:
                    print(f"{datetime.now()} | ❌ Помилка моніторингу {symbol}: {e}")
            
            time.sleep(30)
            
        except Exception as e:
            print(f"{datetime.now()} | ❌ Помилка моніторингу: {e}")
            time.sleep(60)

def close_position(symbol: str, current_price: float, pnl_percent: float):
    """Закриття позиції"""
    try:
        position = active_positions[symbol]
        futures_symbol = f"{symbol}/USDT:USDT"
        
        if position['side'] == 'LONG':
            order = exchange.create_market_sell_order(futures_symbol, position['amount'])
        else:
            order = exchange.create_market_buy_order(futures_symbol, position['amount'])
        
        # Оновлюємо PnL
        global profit_loss
        profit_loss += (pnl_percent / 100) * TRADE_AMOUNT_USD
        
        del active_positions[symbol]
        
        msg = f"🔒 ЗАКРИТО {symbol} {position['side']}\n"
        msg += f"📈 PnL: {pnl_percent:+.2f}%\n"
        msg += f"💰 Ціна: ${current_price:.6f}\n"
        msg += f"💵 Прибуток: ${(pnl_percent/100)*TRADE_AMOUNT_USD:.2f}\n"
        msg += f"🏦 Загальний PnL: ${profit_loss:.2f}"
        
        bot.send_message(CHAT_ID, msg)
        print(f"{datetime.now()} | {msg}")
        
    except Exception as e:
        error_msg = f"❌ ПОМИЛКА закриття {symbol}: {e}"
        bot.send_message(CHAT_ID, error_msg)
        print(f"{datetime.now()} | {error_msg}")

# -------------------------
# ОСНОВНИЙ ЦИКЛ
# -------------------------
def start_arbitrage_bot():
    """Головний цикл бота"""
    bot.send_message(CHAT_ID, "🚀 Запущено РЕАЛЬНИЙ арбітражний бот!")
    
    # Запуск моніторингу
    monitor_thread = threading.Thread(target=monitor_positions, daemon=True)
    monitor_thread.start()
    
    cycle = 0
    while True:
        cycle += 1
        
        try:
            balance = exchange.fetch_balance()['USDT']['total'] if exchange else 0
            print(f"{datetime.now()} | 🔄 Цикл {cycle} | Баланс: ${balance:.2f}")
            
            # Пошук можливостей
            opportunities = find_real_arbitrage_opportunities()
            
            if opportunities:
                print(f"{datetime.now()} | 📊 Знайдено {len(opportunities)} реальних арбітражів")
                
                for symbol, futures_price, spot_price, spread in opportunities:
                    if len(active_positions) < MAX_POSITIONS:
                        execute_trade_based_on_premium(symbol, futures_price, spot_price, spread)
                        time.sleep(2)
            
            time.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            print(f"{datetime.now()} | ❌ Помилка в циклі: {e}")
            time.sleep(60)

# -------------------------
# TELEGRAM КОМАНДИ
# -------------------------
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "🤖 Реальний арбітражний бот активовано!")

@bot.message_handler(commands=['status'])
def show_status(message):
    if not exchange:
        bot.reply_to(message, "❌ Біржа не підключена")
        return
        
    balance = exchange.fetch_balance()['USDT']['total']
    msg = f"💰 Баланс: ${balance:.2f}\n"
    msg += f"📊 Позицій: {len(active_positions)}\n"
    msg += f"📈 PnL: ${profit_loss:.2f}"
    bot.reply_to(message, msg)

@bot.message_handler(commands=['arbitrage'])
def find_arbitrage_cmd(message):
    opportunities = find_real_arbitrage_opportunities()
    
    if opportunities:
        msg = "🎯 Реальні арбітражі:\n\n"
        for symbol, futures, spot, spread in opportunities:
            msg += f"• {symbol}: {spread:+.2f}%\n"
            msg += f"  Futures: ${futures:.6f}\n"
            msg += f"  Spot: ${spot:.6f}\n\n"
    else:
        msg = "🔍 Арбітражі не знайдені"
    
    bot.reply_to(message, msg)

# -------------------------
# ЗАПУСК
# -------------------------
if __name__ == "__main__":
    print(f"{datetime.now()} | 🚀 Запуск реального арбітражного бота...")
    
    if not all([API_KEY_TELEGRAM, CHAT_ID, GATE_API_KEY, GATE_API_SECRET]):
        print(f"{datetime.now()} | ❌ Відсутні обов'язкові ключі!")
        exit(1)
    
    # Запуск бота
    bot_thread = threading.Thread(target=start_arbitrage_bot, daemon=True)
    bot_thread.start()
    
    # Запуск Telegram
    bot.remove_webhook()
    bot.polling(none_stop=True)