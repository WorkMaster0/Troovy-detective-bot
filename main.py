import ccxt
import requests
import time
import os
from datetime import datetime, timedelta
from flask import Flask, request
import telebot
import threading
import json
import pandas as pd
from collections import deque

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

TRADE_AMOUNT_USD = float(os.getenv("TRADE_AMOUNT_USD", 50))
FUNDING_THRESHOLD = float(os.getenv("FUNDING_THRESHOLD", 0.001))  # 0.1%
CORRELATION_THRESHOLD = float(os.getenv("CORRELATION_THRESHOLD", 5.0))  # 5%
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))  # 60 секунд

bot = telebot.TeleBot(API_KEY_TELEGRAM)
app = Flask(__name__)

# Ініціалізація біржі
try:
    gate = ccxt.gateio({
        "apiKey": GATE_API_KEY,
        "secret": GATE_API_SECRET,
        "options": {"defaultType": "swap"}  # Змінили на swap для ф'ючерсів
    })
    gate.load_markets()
    print(f"{datetime.now()} | ✅ Успішно підключено до Gate.io Futures")
except Exception as e:
    print(f"{datetime.now()} | ❌ Помилка підключення до Gate.io: {e}")
    gate = None

# Історичні дані для аналізу
historical_data = {}
correlation_pairs = [
    ('BTC/USDT:USDT', 'ETH/USDT:USDT'),
    ('SOL/USDT:USDT', 'APT/USDT:USDT'), 
    ('BNB/USDT:USDT', 'BTC/USDT:USDT'),
    ('XRP/USDT:USDT', 'ADA/USDT:USDT')
]

# -------------------------
# ФУНКЦІЇ ДЛЯ Ф'ЮЧЕРСНОГО АРБІТРАЖУ
# -------------------------

def get_funding_rate(symbol):
    """Отримання поточного funding rate для ф'ючерсу"""
    try:
        # Отримуємо ім'я контракту для Gate.io API
        contract_name = symbol.replace('/USDT:USDT', '_USDT')
        
        url = "https://api.gateio.ws/api/v4/futures/usdt/funding_rate"
        response = requests.get(url, timeout=10)
        
        if response.status_code == 200:
            funding_data = response.json()
            for item in funding_data:
                if item['name'] == contract_name:
                    return float(item['rate']), float(item.get('predicted_rate', 0))
            
        return None, None
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка отримання funding rate: {e}")
        return None, None

def calculate_annualized_funding(funding_rate):
    """Розрахунок річного funding rate"""
    if funding_rate is None:
        return 0
    # Funding кожні 8 годин (3 рази на день)
    return funding_rate * 3 * 365 * 100  # У відсотках

def detect_funding_arbitrage():
    """
    Знаходимо моменти, коли funding rate на ф'ючерсах настільки високий,
    що можна відкривати позицію з гарантованим прибутком
    """
    symbols = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'XRP/USDT:USDT']
    
    opportunities = []
    
    for symbol in symbols:
        try:
            # Отримуємо funding rate
            current_funding, predicted_funding = get_funding_rate(symbol)
            
            if current_funding is None:
                continue
                
            # Розрахунок річного funding rate
            annualized = calculate_annualized_funding(abs(current_funding))
            
            # Перевіряємо чи funding rate перевищує поріг
            if abs(current_funding) > FUNDING_THRESHOLD and annualized > 30:
                # Отримуємо ціни для інформації
                ticker = gate.fetch_ticker(symbol)
                
                opportunity = {
                    'symbol': symbol,
                    'current_funding': current_funding,
                    'predicted_funding': predicted_funding,
                    'annualized': annualized,
                    'price': ticker['last'],
                    'signal': 'LONG' if current_funding < 0 else 'SHORT',
                    'confidence': min(100, int(annualized / 3)),
                    'timestamp': datetime.now().isoformat()
                }
                
                opportunities.append(opportunity)
                print(f"{datetime.now()} | 📊 Funding opportunity: {symbol} - {annualized:.1f}% річних")
                
        except Exception as e:
            print(f"{datetime.now()} | ❌ Помилка перевірки {symbol}: {e}")
    
    return opportunities

def update_historical_data():
    """Оновлення історичних даних для кореляційного аналізу"""
    global historical_data
    
    for pair1, pair2 in correlation_pairs:
        try:
            # Отримуємо поточні ціни
            price1 = gate.fetch_ticker(pair1)['last']
            price2 = gate.fetch_ticker(pair2)['last']
            
            ratio = price1 / price2
            
            # Зберігаємо в історичних даних
            key = f"{pair1}_{pair2}"
            if key not in historical_data:
                historical_data[key] = deque(maxlen=100)  # Останні 100 точок
            
            historical_data[key].append({
                'timestamp': datetime.now(),
                'ratio': ratio,
                'price1': price1,
                'price2': price2
            })
            
        except Exception as e:
            print(f"{datetime.now()} | ❌ Помилка оновлення історичних даних: {e}")

def detect_correlation_arbitrage():
    """
    Знаходимо розриви в кореляції між пов'язаними активами
    """
    opportunities = []
    
    for pair1, pair2 in correlation_pairs:
        try:
            key = f"{pair1}_{pair2}"
            if key not in historical_data or len(historical_data[key]) < 20:
                continue
                
            # Останнє співвідношення
            current_data = historical_data[key][-1]
            current_ratio = current_data['ratio']
            
            # Історичне середнє співвідношення
            historical_ratios = [data['ratio'] for data in historical_data[key]]
            mean_ratio = sum(historical_ratios) / len(historical_ratios)
            
            # Відхилення у відсотках
            deviation = abs((current_ratio - mean_ratio) / mean_ratio) * 100
            
            if deviation > CORRELATION_THRESHOLD:
                # Визначаємо напрямок сигналу
                if current_ratio > mean_ratio:
                    signal = f"BUY {pair2} / SELL {pair1}"
                else:
                    signal = f"BUY {pair1} / SELL {pair2}"
                
                opportunity = {
                    'pairs': (pair1, pair2),
                    'deviation': deviation,
                    'current_ratio': current_ratio,
                    'mean_ratio': mean_ratio,
                    'signal': signal,
                    'confidence': min(95, int(deviation * 2)),
                    'timestamp': datetime.now().isoformat()
                }
                
                opportunities.append(opportunity)
                print(f"{datetime.now()} | 📊 Correlation opportunity: {deviation:.1f}% deviation")
                
        except Exception as e:
            print(f"{datetime.now()} | ❌ Помилка кореляційного аналізу: {e}")
    
    return opportunities

def execute_futures_trade(signal):
    """Виконання торгівлі на ф'ючерсах"""
    if not gate:
        print(f"{datetime.now()} | ❌ Біржа не підключена")
        return False
    
    try:
        symbol = signal.get('symbol')
        trade_type = signal.get('signal', '').upper()
        
        if not symbol or trade_type not in ['LONG', 'SHORT']:
            return False
        
        # Визначаємо розмір позиції
        ticker = gate.fetch_ticker(symbol)
        price = ticker['last']
        amount = TRADE_AMOUNT_USD / price
        
        if trade_type == 'LONG':
            order = gate.create_market_buy_order(symbol, amount)
            print(f"{datetime.now()} | ✅ LONG позиція: {amount:.6f} {symbol}")
        else:
            order = gate.create_market_sell_order(symbol, amount)
            print(f"{datetime.now()} | ✅ SHORT позиція: {amount:.6f} {symbol}")
        
        # Надсилаємо повідомлення в Telegram
        msg = f"🎯 100% СИГНАЛ! {symbol}\n"
        msg += f"Тип: {trade_type}\n"
        msg += f"Funding rate: {signal.get('current_funding', 0)*100:.3f}%\n"
        msg += f"Річний: {signal.get('annualized', 0):.1f}%\n"
        msg += f"Впевненість: {signal.get('confidence', 0)}%\n"
        msg += f"Розмір: {TRADE_AMOUNT_USD} USDT"
        
        bot.send_message(CHAT_ID, msg)
        return True
        
    except Exception as e:
        error_msg = f"❌ Помилка виконання торгівлі: {e}"
        print(f"{datetime.now()} | {error_msg}")
        bot.send_message(CHAT_ID, error_msg)
        return False

# -------------------------
# ОСНОВНИЙ ЦИКЛ ТОРГІВЛІ
# -------------------------

def start_arbitrage():
    """Основний цикл арбітражу"""
    bot.send_message(CHAT_ID, "🚀 Ф'ючерсний арбітраж-бот запущено!")
    bot.send_message(CHAT_ID, f"📊 Моніторинг funding rate > {FUNDING_THRESHOLD*100:.3f}%")
    
    last_correlation_update = datetime.now()
    
    while True:
        try:
            print(f"{datetime.now()} | 🔄 Перевірка арбітражних можливостей...")
            
            # 1. Оновлюємо історичні дані для кореляції кожні 5 хвилин
            if (datetime.now() - last_correlation_update).seconds > 300:
                update_historical_data()
                last_correlation_update = datetime.now()
            
            # 2. Шукаємо funding rate арбітраж
            funding_opportunities = detect_funding_arbitrage()
            for opportunity in funding_opportunities:
                if opportunity['confidence'] > 80:  # Мінімум 80% впевненості
                    execute_futures_trade(opportunity)
                    time.sleep(2)  # Зачекати між угодами
            
            # 3. Шукаємо кореляційний арбітраж
            correlation_opportunities = detect_correlation_arbitrage()
            for opportunity in correlation_opportunities:
                if opportunity['confidence'] > 85:  # Мінімум 85% впевненості
                    # Для кореляційного арбітражу потрібна парна торгівля
                    msg = f"📊 Кореляційний сигнал!\n"
                    msg += f"Пари: {opportunity['pairs'][0]} / {opportunity['pairs'][1]}\n"
                    msg += f"Відхилення: {opportunity['deviation']:.1f}%\n"
                    msg += f"Сигнал: {opportunity['signal']}\n"
                    msg += f"Впевненість: {opportunity['confidence']}%"
                    
                    bot.send_message(CHAT_ID, msg)
            
            time.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            print(f"{datetime.now()} | ❌ Критична помилка в головному циклі: {e}")
            time.sleep(60)

# -------------------------
# TELEGRAM КОМАНДИ
# -------------------------

@bot.message_handler(commands=['start'])
def send_welcome(message):
    """Команда старту"""
    bot.reply_to(message, "🤖 Ф'ючерсний арбітраж-бот активовано!\n\n"
                         "Доступні команди:\n"
                         "/status - Статус системи\n"
                         "/funding - Поточні funding rates\n"
                         "/opportunities - Пошук можливостей\n"
                         "/stats - Статистика торгівлі")

@bot.message_handler(commands=['funding'])
def check_funding(message):
    """Перевірка поточних funding rates"""
    symbols = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT']
    
    msg = "📊 Поточні Funding Rates:\n\n"
    
    for symbol in symbols:
        try:
            funding_rate, predicted_rate = get_funding_rate(symbol)
            if funding_rate is not None:
                annualized = calculate_annualized_funding(abs(funding_rate))
                msg += f"{symbol}: {funding_rate*100:.3f}%"
                msg += f" (річних: {annualized:.1f}%)\n"
        except:
            msg += f"{symbol}: помилка отримання\n"
    
    bot.reply_to(message, msg)

@bot.message_handler(commands=['opportunities'])
def find_opportunities(message):
    """Миттєвий пошук можливостей"""
    funding_ops = detect_funding_arbitrage()
    correlation_ops = detect_correlation_arbitrage()
    
    if not funding_ops and not correlation_ops:
        bot.reply_to(message, "🔍 Можливостей не знайдено")
        return
    
    msg = "🎯 Знайдені можливості:\n\n"
    
    for op in funding_ops:
        msg += f"💰 Funding: {op['symbol']}\n"
        msg += f"   Rate: {op['current_funding']*100:.3f}%\n"
        msg += f"   Річних: {op['annualized']:.1f}%\n"
        msg += f"   Сигнал: {op['signal']}\n"
        msg += f"   Впевненість: {op['confidence']}%\n\n"
    
    for op in correlation_ops:
        msg += f"📈 Кореляція: {op['pairs'][0]}/{op['pairs'][1]}\n"
        msg += f"   Відхилення: {op['deviation']:.1f}%\n"
        msg += f"   Сигнал: {op['signal']}\n"
        msg += f"   Впевненість: {op['confidence']}%\n\n"
    
    bot.reply_to(message, msg)

@bot.message_handler(commands=['status'])
def send_status(message):
    """Статус системи"""
    try:
        if gate:
            balance = gate.fetch_balance()
            usdt_balance = balance['total'].get('USDT', 0)
            msg = f"✅ Система працює\n💰 Баланс: {usdt_balance:.2f} USDT\n"
            msg += f"📊 Історичні дані: {len(historical_data)} пар\n"
            msg += f"⏰ Оновлення кожні: {CHECK_INTERVAL}с"
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
    print(f"{datetime.now()} | 🚀 Запуск ф'ючерсного арбітраж-бота...")
    
    # Перевірка обов'язкових ключів
    required_keys = [API_KEY_TELEGRAM, CHAT_ID, GATE_API_KEY, GATE_API_SECRET]
    if not all(required_keys):
        print(f"{datetime.now()} | ❌ Відсутні обов'язкові API ключі!")
        exit(1)
    
    setup_webhook()
    threading.Thread(target=start_arbitrage, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)