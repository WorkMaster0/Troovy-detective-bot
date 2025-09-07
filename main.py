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
SPREAD_THRESHOLD = float(os.getenv("SPREAD_THRESHOLD", 0.8))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 30))
LEVERAGE = int(os.getenv("LEVERAGE", 3))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", 5))

bot = telebot.TeleBot(API_KEY_TELEGRAM)
app = Flask(__name__)

# Ініціалізація бірж
exchanges = {}
trade_history = []

try:
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
    exchanges['binance'] = ccxt.binance({
        "options": {"defaultType": "future"}
    })
    exchanges['binance'].load_markets()
    print(f"{datetime.now()} | ✅ Успішно підключено до Binance Futures")
except Exception as e:
    print(f"{datetime.now()} | ❌ Помилка підключення до Binance Futures: {e}")

active_positions = {}
token_blacklist = set()
profit_loss = 0.0

# -------------------------
# Ф'ЮЧЕРСНІ ФУНКЦІЇ
# -------------------------
def set_leverage(symbol: str, leverage: int = LEVERAGE):
    """Встановлення кредитного плеча"""
    try:
        exchanges['gate'].set_leverage(leverage, symbol)
        print(f"{datetime.now()} | ⚙️ Встановлено плече {leverage}x для {symbol}")
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка встановлення плеча {symbol}: {e}")

def get_futures_balance() -> float:
    """Отримання балансу ф'ючерсного рахунку"""
    try:
        balance = exchanges['gate'].fetch_balance()
        return balance['USDT']['total']
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка отримання балансу: {e}")
        return 0

def get_futures_positions():
    """Отримання поточних позицій"""
    try:
        positions = exchanges['gate'].fetch_positions()
        return [p for p in positions if p['contracts'] > 0]
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка отримання позицій: {e}")
        return []

# -------------------------
# ПОКРАЩЕНЕ ОТРИМАННЯ ЦІН
# -------------------------
def get_futures_prices(exchange_name: str, symbols: List[str] = None) -> Dict[str, float]:
    """Отримання цін ф'ючерсів для конкретних символів"""
    prices = {}
    try:
        exchange = exchanges.get(exchange_name)
        if not exchange:
            return prices
            
        if not symbols:
            # Якщо символи не вказані, беремо топ 20
            markets = exchange.load_markets()
            symbols = []
            for symbol, market in markets.items():
                if market.get('swap', False) and market.get('active', False) and symbol.endswith('/USDT:USDT'):
                    clean_symbol = symbol.replace(':USDT', '').replace('/USDT', '')
                    symbols.append(clean_symbol)
                    if len(symbols) >= 20:
                        break
        
        for symbol in symbols:
            try:
                ticker = exchange.fetch_ticker(symbol + '/USDT:USDT')
                if ticker['last'] and ticker['last'] > 0:
                    prices[symbol] = ticker['last']
            except:
                continue
                
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка отримання цін з {exchange_name}: {e}")
    
    return prices

def get_volatile_tokens() -> List[str]:
    """Отримання волатильних токенів"""
    try:
        url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
        response = requests.get(url, timeout=10)
        
        if response.status_code == 200:
            tickers = response.json()
            volatile_tokens = []
            
            for ticker in tickers:
                symbol = ticker['symbol'].replace('USDT', '')
                price_change = float(ticker['priceChangePercent'])
                volume = float(ticker['volume'])
                
                if abs(price_change) > 3.0 and volume > 2000000:
                    volatile_tokens.append(symbol)
                    if len(volatile_tokens) >= 15:
                        break
            
            return volatile_tokens
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка отримання волатильних токенів: {e}")
    
    return ['BTC', 'ETH', 'SOL', 'XRP', 'ADA', 'DOT', 'LINK', 'AVAX', 'MATIC', 'DOGE']

# -------------------------
# ТОРГОВА ЛОГІКА
# -------------------------
def calculate_futures_amount(symbol: str, price: float) -> float:
    """Розрахунок кількості контрактів"""
    try:
        market = exchanges['gate'].market(symbol + '/USDT:USDT')
        contract_size = float(market['contractSize'])
        
        if price <= 0 or contract_size <= 0:
            return 0
            
        # Розраховуємо кількість контрактів
        amount = (TRADE_AMOUNT_USD * LEVERAGE) / (price * contract_size)
        
        # Отримуємо precision та конвертуємо в int
        precision = int(market['precision']['amount'])
        
        # Перевіряємо мінімальну кількість
        min_amount = float(market['limits']['amount']['min'])
        if amount < min_amount:
            print(f"{datetime.now()} | ⚠️ Кількість {amount} менша за мінімум {min_amount} для {symbol}")
            return 0
            
        return round(amount, precision)
        
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка розрахунку кількості {symbol}: {e}")
        return 0

def execute_futures_trade(symbol: str, gate_price: float, binance_price: float, spread: float):
    """Виконання торгової операції"""
    try:
        if len(active_positions) >= MAX_POSITIONS:
            print(f"{datetime.now()} | ⚠️ Досягнуто максимум позицій ({MAX_POSITIONS})")
            return
            
        futures_symbol = symbol + '/USDT:USDT'
        
        # Встановлюємо плече
        set_leverage(futures_symbol, LEVERAGE)
        
        # Розраховуємо кількість
        amount = calculate_futures_amount(symbol, gate_price)
        if amount <= 0:
            print(f"{datetime.now()} | ⚠️ Нульова кількість для {symbol}")
            return
        
        # Додаткова перевірка мінімальної кількості
        market = exchanges['gate'].market(futures_symbol)
        min_amount = float(market['limits']['amount']['min'])
        if amount < min_amount:
            print(f"{datetime.now()} | ⚠️ Кількість {amount} менша за мінімум {min_amount} для {symbol}")
            return
        
        if spread > 0:  # Binance ціна вища - купуємо на Gate
            order = exchanges['gate'].create_market_buy_order(futures_symbol, amount)
            side = "LONG"
        else:  # Gate ціна вища - продаємо на Gate
            order = exchanges['gate'].create_market_sell_order(futures_symbol, amount)
            side = "SHORT"
        
        # Зберігаємо інформацію про trade
        trade_info = {
            'symbol': symbol,
            'side': side,
            'price': gate_price,
            'amount': amount,
            'spread': spread,
            'timestamp': datetime.now(),
            'order_id': order['id']
        }
        trade_history.append(trade_info)
        
        msg = f"🎯 {side} {symbol}\n"
        msg += f"💰 Ціна: ${gate_price:.4f}\n"
        msg += f"📊 Spread: {abs(spread):.2f}%\n"
        msg += f"📦 Кількість: {amount:.6f}\n"
        msg += f"⚖️ Плече: {LEVERAGE}x\n"
        msg += f"🆔 Order: {order['id']}"
        
        bot.send_message(CHAT_ID, msg)
        print(f"{datetime.now()} | {msg}")
        
        # Додаємо в активні позиції
        active_positions[symbol] = trade_info
        
    except Exception as e:
        error_msg = f"❌ ПОМИЛКА торгівлі {symbol}: {e}"
        bot.send_message(CHAT_ID, error_msg)
        print(f"{datetime.now()} | {error_msg}")

# -------------------------
# АРБІТРАЖ ТА МОНІТОРИНГ
# -------------------------
def find_arbitrage_opportunities():
    """Пошук арбітражних можливостей"""
    opportunities = []
    
    try:
        # Отримуємо волатильні токени
        volatile_tokens = get_volatile_tokens()
        
        # Отримуємо ціни для цих токенів
        gate_prices = get_futures_prices('gate', volatile_tokens)
        binance_prices = get_futures_prices('binance', volatile_tokens)
        
        for symbol in volatile_tokens:
            if symbol in active_positions or symbol in token_blacklist:
                continue
                
            gate_price = gate_prices.get(symbol)
            binance_price = binance_prices.get(symbol)
            
            if not gate_price or not binance_price or gate_price == 0:
                continue
                
            spread = ((binance_price - gate_price) / gate_price) * 100
            
            if abs(spread) >= SPREAD_THRESHOLD:
                opportunities.append((symbol, gate_price, binance_price, spread))
    
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка пошуку арбітражу: {e}")
    
    return opportunities

def monitor_positions():
    """Моніторинг та закриття позицій"""
    while True:
        try:
            for symbol in list(active_positions.keys()):
                try:
                    position = active_positions[symbol]
                    ticker = exchanges['gate'].fetch_ticker(symbol + '/USDT:USDT')
                    current_price = ticker['last']
                    entry_price = position['price']
                    
                    # Розраховуємо PnL
                    if position['side'] == 'LONG':
                        pnl_percent = ((current_price - entry_price) / entry_price) * 100 * LEVERAGE
                    else:
                        pnl_percent = ((entry_price - current_price) / entry_price) * 100 * LEVERAGE
                    
                    # Автоматичне закриття
                    if abs(pnl_percent) >= 5.0:  # 5% прибуток/збиток
                        close_position(symbol, current_price, pnl_percent)
                        
                except Exception as e:
                    print(f"{datetime.now()} | ❌ Помилка моніторингу {symbol}: {e}")
            
            time.sleep(20)
            
        except Exception as e:
            print(f"{datetime.now()} | ❌ Помилка моніторингу позицій: {e}")
            time.sleep(30)

def close_position(symbol: str, current_price: float, pnl_percent: float):
    """Закриття позиції"""
    try:
        position = active_positions[symbol]
        futures_symbol = symbol + '/USDT:USDT'
        
        if position['side'] == 'LONG':
            order = exchanges['gate'].create_market_sell_order(futures_symbol, position['amount'])
        else:
            order = exchanges['gate'].create_market_buy_order(futures_symbol, position['amount'])
        
        # Оновлюємо PnL
        global profit_loss
        profit_loss += (pnl_percent / 100) * TRADE_AMOUNT_USD
        
        # Видаляємо позицію
        del active_positions[symbol]
        
        msg = f"🔒 ЗАКРИТО {symbol}\n"
        msg += f"📈 PnL: {pnl_percent:.2f}%\n"
        msg += f"💰 Ціна: ${current_price:.4f}\n"
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
def start_futures_arbitrage():
    """Основний цикл арбітражу"""
    bot.send_message(CHAT_ID, "🚀 Ф'ючерсний арбітражний бот запущено!")
    
    # Запускаємо моніторинг
    monitoring_thread = threading.Thread(target=monitor_positions, daemon=True)
    monitoring_thread.start()
    
    cycle = 0
    while True:
        cycle += 1
        
        try:
            balance = get_futures_balance()
            positions_count = len(active_positions)
            
            print(f"{datetime.now()} | 🔄 Цикл {cycle} | Баланс: ${balance:.2f} | Позиції: {positions_count}")
            
            # Шукаємо арбітраж
            opportunities = find_arbitrage_opportunities()
            
            if opportunities:
                print(f"{datetime.now()} | 📊 Знайдено {len(opportunities)} арбітражів")
                
                # Сортуємо за спредом
                opportunities.sort(key=lambda x: abs(x[3]), reverse=True)
                
                for symbol, gate_price, binance_price, spread in opportunities[:3]:
                    if positions_count < MAX_POSITIONS:
                        execute_futures_trade(symbol, gate_price, binance_price, spread)
                        time.sleep(1)
                    else:
                        print(f"{datetime.now()} | ⚠️ Максимум позицій досягнуто")
                        break
            
            time.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            print(f"{datetime.now()} | ❌ Помилка в головному циклі: {e}")
            time.sleep(60)

# -------------------------
# TELEGRAM КОМАНДИ
# -------------------------
@bot.message_handler(commands=['start'])
def send_welcome(message):
    """Команда старту"""
    welcome_msg = """
🤖 *Ф\'ючерсний Арбітражний Бот*

*Доступні команди:*
/status - Статус системи
/balance - Баланс та позиції
/arbitrage - Миттєвий пошук арбітражу
/tokens - Список токенів
/profit - Прибуток та статистика
/blacklist - Чорний список
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
    """Статус системи"""
    balance = get_futures_balance()
    positions = get_futures_positions()
    
    msg = f"📊 *Статус Системы*\n\n"
    msg += f"💰 *Баланс:* ${balance:.2f}\n"
    msg += f"📈 *Активні позиції:* {len(active_positions)}\n"
    msg += f"📉 *Позиції на біржі:* {len(positions)}\n"
    msg += f"⚫ *Чорний список:* {len(token_blacklist)}\n"
    msg += f"💵 *Загальний PnL:* ${profit_loss:.2f}\n"
    msg += f"🔄 *Цикл:* {len(trade_history)} trades"
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['balance'])
def show_balance(message):
    """Баланс та позиції"""
    balance = get_futures_balance()
    
    msg = f"💳 *Баланс:* ${balance:.2f}\n\n"
    msg += f"📊 *Активні позиції:* {len(active_positions)}\n"
    
    if active_positions:
        msg += "\n*Деталі позицій:*\n"
        for symbol, pos in active_positions.items():
            msg += f"• {symbol} {pos['side']} - ${pos['price']:.4f}\n"
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['arbitrage'])
def find_arbitrage_cmd(message):
    """Миттєвий пошук арбітражу"""
    opportunities = find_arbitrage_opportunities()
    
    if opportunities:
        msg = "🎯 *Знайдені арбітражі:*\n\n"
        for symbol, gate_price, binance_price, spread in opportunities[:5]:
            direction = "📈" if spread > 0 else "📉"
            msg += f"{direction} *{symbol}:* {spread:+.2f}%\n"
            msg += f"   Gate: ${gate_price:.4f} | Binance: ${binance_price:.4f}\n\n"
    else:
        msg = "🔍 *Арбітражі не знайдені*"
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['profit'])
def show_profit(message):
    """Прибуток та статистика"""
    msg = f"📈 *Статистика прибутку*\n\n"
    msg += f"💵 *Загальний PnL:* ${profit_loss:.2f}\n"
    msg += f"🔄 *Всього угод:* {len(trade_history)}\n"
    msg += f"✅ *Активних позицій:* {len(active_positions)}\n\n"
    
    if trade_history:
        msg += "*Останні 5 угод:*\n"
        for trade in trade_history[-5:]:
            msg += f"• {trade['symbol']} {trade['side']} - {trade['spread']:.2f}%\n"
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['blacklist'])
def show_blacklist(message):
    """Чорний список"""
    if token_blacklist:
        msg = "⚫ *Чорний список:*\n\n"
        for token in list(token_blacklist)[:10]:
            msg += f"• {token}\n"
    else:
        msg = "✅ *Чорний список порожній*"
    
    bot.reply_to(message, msg, parse_mode='Markdown')

@bot.message_handler(commands=['help'])
def show_help(message):
    """Допомога"""
    help_msg = """
🆘 *Довідка по командам*

*/start* - Запуск бота
*/status* - Статус системи
*/balance* - Баланс та позиції
*/arbitrage* - Миттєвий пошук арбітражу
*/profit* - Статистика прибутку
*/tokens* - Список токенів
*/blacklist* - Чорний список
*/help* - Ця довідка

*Налаштування через змінні оточення:*
• SPREAD_THRESHOLD - Мінімальний спред
• TRADE_AMOUNT_USD - Сума торгівлі
• LEVERAGE - Кредитне плече
• MAX_POSITIONS - Макс. позицій
    """
    
    bot.reply_to(message, help_msg, parse_mode='Markdown')

# -------------------------
# WEBHOOK ТА ЗАПУСК
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
    return {'status': 'healthy', 'timestamp': datetime.now().isoformat()}

def setup_webhook():
    """Налаштування вебхука"""
    try:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=WEBHOOK_URL)
        print(f"{datetime.now()} | ✅ Вебхук налаштовано: {WEBHOOK_URL}")
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка налаштування вебхука: {e}")

if __name__ == "__main__":
    print(f"{datetime.now()} | 🚀 Запуск ф'ючерсного арбітражного бота...")
    
    # Перевірка обов'язкових ключів
    required_keys = [API_KEY_TELEGRAM, CHAT_ID, GATE_API_KEY, GATE_API_SECRET]
    if not all(required_keys):
        print(f"{datetime.now()} | ❌ Відсутні обов'язкові API ключі!")
        exit(1)
    
    # Налаштовуємо вебхук
    setup_webhook()
    
    # Запускаємо арбітраж
    arbitrage_thread = threading.Thread(target=start_futures_arbitrage, daemon=True)
    arbitrage_thread.start()
    
    print(f"{datetime.now()} | ✅ Бот запущено. Очікую команди...")
    
    # Запускаємо Flask
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)