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

TRADE_AMOUNT_USD = float(os.getenv("TRADE_AMOUNT_USD", 50))
SPREAD_THRESHOLD = float(os.getenv("SPREAD_THRESHOLD", 1.0))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))
LEVERAGE = int(os.getenv("LEVERAGE", 20))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", 1))
MAX_SPREAD = float(os.getenv("MAX_SPREAD", 10.0))

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
token_blacklist = set()

# -------------------------
# WEBHOOK ТА FLASK ФУНКЦІЇ
# -------------------------
@app.route(WEBHOOK_PATH, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return 'OK', 200
    return 'Bad Request', 400

@app.route('/health', methods=['GET'])
def health_check():
    return {
        'status': 'healthy', 
        'timestamp': datetime.now().isoformat(),
        'positions': len(active_positions),
        'balance': get_balance()
    }

@app.route('/stats', methods=['GET'])
def stats():
    return {
        'total_trades': len(trade_history),
        'active_positions': len(active_positions),
        'profit_loss': profit_loss,
        'blacklisted_tokens': len(token_blacklist)
    }

def setup_webhook():
    """Налаштування вебхука для Telegram"""
    try:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=WEBHOOK_URL)
        print(f"{datetime.now()} | ✅ Вебхук налаштовано: {WEBHOOK_URL}")
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка налаштування вебхука: {e}")

# -------------------------
# ФУНКЦІЇ ДЛЯ РОБОТИ З БІРЖЕЮ
# -------------------------
def get_balance() -> float:
    """Отримання балансу"""
    try:
        balance = exchange.fetch_balance()
        return balance['USDT']['total']
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка отримання балансу: {e}")
        return 0

def get_positions():
    """Отримання позицій з біржі"""
    try:
        positions = exchange.fetch_positions()
        return [p for p in positions if p['contracts'] > 0]
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка отримання позицій: {e}")
        return []

def get_futures_prices(symbols: List[str] = None) -> Dict[str, float]:
    """Отримання цін ф'ючерсів"""
    prices = {}
    if not exchange:
        return prices
        
    try:
        if not symbols:
            symbols = ['BTC', 'ETH', 'SOL', 'XRP', 'ADA', 'AVAX', 'DOT', 'LINK', 'MATIC', 'DOGE', 
                      'BNB', 'ATOM', 'LTC', 'OP', 'ARB', 'FIL', 'APT', 'NEAR', 'ALGO', 'XLM']
        
        for symbol in symbols:
            try:
                ticker = exchange.fetch_ticker(f"{symbol}/USDT:USDT")
                if ticker and ticker['last'] and ticker['last'] > 0:
                    prices[symbol] = ticker['last']
            except Exception as e:
                print(f"{datetime.now()} | ⚠️ Помилка ціни {symbol}: {e}")
                continue
                
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка отримання цін: {e}")
    
    return prices

def get_spot_prices(symbols: List[str] = None) -> Dict[str, float]:
    """Отримання спотових цін через CoinGecko"""
    prices = {}
    
    try:
        if not symbols:
            symbols = ['BTC', 'ETH', 'SOL', 'XRP', 'ADA', 'AVAX', 'DOT', 'LINK', 'MATIC', 'DOGE']
        
        # Конвертуємо символи в CoinGecko format
        coin_map = {
            'BTC': 'bitcoin', 'ETH': 'ethereum', 'SOL': 'solana', 
            'XRP': 'ripple', 'ADA': 'cardano', 'AVAX': 'avalanche-2',
            'DOT': 'polkadot', 'LINK': 'chainlink', 'MATIC': 'polygon',
            'DOGE': 'dogecoin', 'BNB': 'binancecoin', 'ATOM': 'cosmos',
            'LTC': 'litecoin', 'OP': 'optimism', 'ARB': 'arbitrum'
        }
        
        coin_ids = []
        for symbol in symbols:
            if symbol in coin_map:
                coin_ids.append(coin_map[symbol])
        
        if coin_ids:
            url = "https://api.coingecko.com/api/v3/simple/price"
            params = {
                "ids": ",".join(coin_ids),
                "vs_currencies": "usd"
            }
            
            response = requests.get(url, params=params, timeout=15)
            if response.status_code == 200:
                data = response.json()
                for coin_id, price_data in data.items():
                    # Знаходимо символ по coin_id
                    for sym, cid in coin_map.items():
                        if cid == coin_id:
                            prices[sym] = price_data['usd']
                            break
                
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка отримання спотових цін: {e}")
    
    return prices

# -------------------------
# АРБІТРАЖНА ЛОГІКА
# -------------------------
def calculate_spread(futures_price: float, spot_price: float) -> float:
    """Розрахунок спреду"""
    if not futures_price or not spot_price or spot_price == 0:
        return 0
    return ((futures_price - spot_price) / spot_price) * 100

def calculate_futures_amount(symbol: str, price: float) -> float:
    """Розрахунок кількості контрактів"""
    try:
        market = exchange.market(f"{symbol}/USDT:USDT")
        contract_size = float(market['contractSize'])
        
        if price <= 0 or contract_size <= 0:
            return 0
            
        amount = (TRADE_AMOUNT_USD * LEVERAGE) / (price * contract_size)
        precision = int(market['precision']['amount'])
        
        min_amount = float(market['limits']['amount']['min'])
        if amount < min_amount:
            print(f"{datetime.now()} | ⚠️ Кількість {amount} менша за мінімум {min_amount}")
            return 0
            
        return round(amount, precision)
        
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка розрахунку кількості {symbol}: {e}")
        return 0

def execute_arbitrage_trade(symbol: str, futures_price: float, spot_price: float, spread: float):
    """Виконання арбітражної торгівлі"""
    try:
        if len(active_positions) >= MAX_POSITIONS:
            return
            
        amount = calculate_futures_amount(symbol, futures_price)
        if amount <= 0:
            return
        
        futures_symbol = f"{symbol}/USDT:USDT"
        
        # Встановлюємо плече
        exchange.set_leverage(LEVERAGE, futures_symbol)
        
        if spread > 0:  # Ф'ючерси дорожчі - продаємо
            order = exchange.create_market_sell_order(futures_symbol, amount)
            side = "SHORT"
            reason = "Премія ф'ючерсів"
        else:  # Ф'ючерси дешевші - купуємо
            order = exchange.create_market_buy_order(futures_symbol, amount)
            side = "LONG"
            reason = "Дисконт ф'ючерсів"
        
        # Зберігаємо інформацію про trade
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
        token_blacklist.add(symbol)

def find_arbitrage_opportunities():
    """Пошук арбітражних можливостей"""
    opportunities = []
    
    try:
        symbols = ['BTC', 'ETH', 'SOL', 'XRP', 'ADA', 'AVAX', 'DOT', 'LINK', 'MATIC', 'DOGE']
        
        futures_prices = get_futures_prices(symbols)
        spot_prices = get_spot_prices(symbols)
        
        for symbol in symbols:
            if symbol in active_positions or symbol in token_blacklist:
                continue
                
            futures_price = futures_prices.get(symbol)
            spot_price = spot_prices.get(symbol)
            
            if not futures_price or not spot_price:
                continue
                
            spread = calculate_spread(futures_price, spot_price)
            
            # Реальні спреди (1-10%)
            if abs(spread) >= SPREAD_THRESHOLD and abs(spread) <= MAX_SPREAD:
                opportunities.append((symbol, futures_price, spot_price, spread))
    
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка пошуку арбітражу: {e}")
    
    return opportunities

# -------------------------
# МОНІТОРИНГ ТА УПРАВЛІННЯ
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
                    
                    if position['side'] == 'LONG':
                        pnl_percent = ((current_price - entry_price) / entry_price) * 100 * LEVERAGE
                    else:
                        pnl_percent = ((entry_price - current_price) / entry_price) * 100 * LEVERAGE
                    
                    # Закриття при ±3%
                    if abs(pnl_percent) >= 3.0:
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
    bot.send_message(CHAT_ID, "🚀 Арбітражний бот запущено з усіма функціями!")
    
    monitor_thread = threading.Thread(target=monitor_positions, daemon=True)
    monitor_thread.start()
    
    cycle = 0
    while True:
        cycle += 1
        
        try:
            balance = get_balance()
            print(f"{datetime.now()} | 🔄 Цикл {cycle} | Баланс: ${balance:.2f}")
            
            opportunities = find_arbitrage_opportunities()
            
            if opportunities:
                print(f"{datetime.now()} | 📊 Знайдено {len(opportunities)} арбітражів")
                
                for symbol, futures_price, spot_price, spread in opportunities:
                    if len(active_positions) < MAX_POSITIONS:
                        execute_arbitrage_trade(symbol, futures_price, spot_price, spread)
                        time.sleep(2)
            
            time.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            print(f"{datetime.now()} | ❌ Помилка в циклі: {e}")
            time.sleep(60)

# -------------------------
# TELEGRAM КОМАНДИ (ПОВНИЙ НАБІР)
# -------------------------
@bot.message_handler(commands=['start'])
def send_welcome(message):
    welcome_msg = """
🤖 *Повнофункціональний Арбітражний Бот*

*Доступні команди:*
/status - Статус системи
/balance - Баланс та позиції
/positions - Деталі позицій
/arbitrage - Пошук арбітражу
/profit - Статистика прибутку
/trades - Історія угод
/blacklist - Чорний список
/health - Стан здоров'я
/help - Допомога

*Налаштування:*
• Спред: {}%
• Плече: {}x
• Сума: ${}
• Макс. позицій: {}
    """.format(SPREAD_THRESHOLD, LEVERAGE, TRADE_AMOUNT_USD, MAX_POSITIONS)
    
    bot.reply_to(message, welcome_msg, parse_mode='Markdown')

@bot.message_handler(commands=['status'])
def show_status(message):
    balance = get_balance()
    exchange_positions = get_positions()
    
    msg = f"📊 *Статус Системи*\n\n"
    msg += f"💰 *Баланс:* ${balance:.2f}\n"
    msg += f"📈 *Активні позиції:* {len(active_positions)}\n"
    msg += f"📉 *Позиції на біржі:* {len(exchange_positions)}\n"
    msg += f"⚫ *Чорний список:* {len(token_blacklist)}\n"
    msg += f"💵 *Загальний PnL:* ${profit_loss:.2f}\n"
    msg += f"🔄 *Всього угод:* {len(trade_history)}"
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['balance'])
def show_balance(message):
    balance = get_balance()
    msg = f"💳 *Баланс:* ${balance:.2f}\n"
    msg += f"📊 *Активні позиції:* {len(active_positions)}/{MAX_POSITIONS}"
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['positions'])
def show_positions(message):
    if not active_positions:
        bot.reply_to(message, "📭 Немає активних позицій")
        return
    
    msg = "📋 *Активні позиції:*\n\n"
    for symbol, position in active_positions.items():
        msg += f"• {symbol} {position['side']}\n"
        msg += f"  Ціна: ${position['futures_price']:.6f}\n"
        msg += f"  Спред: {position['spread']:.2f}%\n"
        msg += f"  Час: {position['timestamp'].strftime('%H:%M:%S')}\n\n"
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['arbitrage'])
def find_arbitrage_cmd(message):
    opportunities = find_arbitrage_opportunities()
    
    if opportunities:
        msg = "🎯 *Знайдені арбітражі:*\n\n"
        for symbol, futures, spot, spread in opportunities:
            direction = "📈" if spread > 0 else "📉"
            msg += f"{direction} *{symbol}:* {spread:+.2f}%\n"
            msg += f"   Futures: ${futures:.6f}\n"
            msg += f"   Spot: ${spot:.6f}\n\n"
    else:
        msg = "🔍 *Арбітражі не знайдені*"
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['profit'])
def show_profit(message):
    msg = f"📈 *Статистика прибутку*\n\n"
    msg += f"💵 *Загальний PnL:* ${profit_loss:.2f}\n"
    msg += f"🔄 *Всього угод:* {len(trade_history)}\n"
    msg += f"✅ *Активних позицій:* {len(active_positions)}\n"
    msg += f"❌ *Чорний список:* {len(token_blacklist)}"
    
    if trade_history:
        profitable = sum(1 for t in trade_history if 'spread' in t and t['spread'] > 0)
        msg += f"\n📊 *Успішні угоди:* {profitable}/{len(trade_history)}"
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['trades'])
def show_trades(message):
    if not trade_history:
        bot.reply_to(message, "📭 Немає історії угод")
        return
    
    msg = "📜 *Останні 5 угод:*\n\n"
    for trade in trade_history[-5:]:
        msg += f"• {trade['symbol']} {trade['side']}\n"
        msg += f"  Спред: {trade.get('spread', 0):.2f}%\n"
        msg += f"  Час: {trade['timestamp'].strftime('%H:%M')}\n\n"
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['blacklist'])
def show_blacklist(message):
    if token_blacklist:
        msg = "⚫ *Чорний список:*\n\n"
        for token in list(token_blacklist)[:10]:
            msg += f"• {token}\n"
    else:
        msg = "✅ *Чорний список порожній*"
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['health'])
def show_health(message):
    health = health_check()
    bot.reply_to(message, f"❤️ *Стан здоров'я:* {health['status']}\n🕐 *Час:* {health['timestamp']}")

@bot.message_handler(commands=['help'])
def show_help(message):
    help_msg = """
🆘 *Довідка по командам*

*/start* - Запуск бота
*/status* - Статус системи
*/balance* - Баланс
*/positions* - Активні позиції
*/arbitrage* - Пошук арбітражу
*/profit* - Статистика прибутку
*/trades* - Історія угод
*/blacklist* - Чорний список
*/health* - Стан здоров'я
*/help* - Ця довідка
    """
    
    bot.reply_to(message, help_msg, parse_mode='Markdown')

# -------------------------
# ЗАПУСК СИСТЕМИ
# -------------------------
if __name__ == "__main__":
    print(f"{datetime.now()} | 🚀 Запуск повнофункціонального арбітражного бота...")
    
    # Перевірка ключів
    required_keys = [API_KEY_TELEGRAM, CHAT_ID, GATE_API_KEY, GATE_API_SECRET]
    if not all(required_keys):
        print(f"{datetime.now()} | ❌ Відсутні обов'язкові ключі!")
        exit(1)
    
    # Налаштування вебхука
    setup_webhook()
    
    # Запуск бота
    bot_thread = threading.Thread(target=start_arbitrage_bot, daemon=True)
    bot_thread.start()
    
    print(f"{datetime.now()} | ✅ Бот запущено. Вебхук: {WEBHOOK_URL}")
    
    # Запуск Flask
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)