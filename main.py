import requests
import telebot
from flask import Flask, request
from datetime import datetime, timedelta
import threading
import time
import json
import pandas as pd
import numpy as np
import os
import logging

# Налаштування логування
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# -------------------------
# Налаштування
# -------------------------
API_KEY_TELEGRAM = "8051222216:AAFORHEn1IjWllQyPp8W_1OY3gVxcBNVvZI"
CHAT_ID = "6053907025"
TIMEFRAMES = ["15m", "1h", "4h"]
N_CANDLES = 50  # Зменшено для швидшої обробки
FAST_EMA = 12
SLOW_EMA = 26
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# Додаткові налаштування
MIN_VOLUME = 2_000_000  # Зменшено для більше монет
MIN_PRICE_CHANGE = 1.5  # Зменшено
MIN_CONFIDENCE_FOR_SIGNAL = 0.55  # Зменшено для тесту
MIN_CONFIDENCE_FOR_HISTORY = 0.5

WEBHOOK_HOST = "https://troovy-detective-bot-1-4on4.onrender.com"
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = WEBHOOK_HOST + WEBHOOK_PATH

BINANCE_API_URLS = [
    "https://api.binance.com",
    "https://api1.binance.com"
]

bot = telebot.TeleBot(API_KEY_TELEGRAM)
app = Flask(__name__)

last_signals = {}
last_status = {}
performance_stats = {}
signal_history = []

SIGNALS_HISTORY_FILE = "signals_history.json"
data_cache = {}
CACHE_DURATION = 30

# -------------------------
# Спрощені функції
# -------------------------
def load_signals_history():
    global signal_history
    try:
        if os.path.exists(SIGNALS_HISTORY_FILE):
            with open(SIGNALS_HISTORY_FILE, "r") as f:
                signal_history = json.load(f)
    except Exception as e:
        logger.error(f"Помилка завантаження історії: {e}")

def save_signals_history():
    try:
        with open(SIGNALS_HISTORY_FILE, "w") as f:
            json.dump(signal_history, f, indent=2)
    except Exception as e:
        logger.error(f"Помилка збереження історії: {e}")

def get_top_symbols():
    try:
        url = "https://api.binance.com/api/v3/ticker/24hr"
        data = requests.get(url, timeout=10).json()
        usdt_pairs = [x for x in data if x["symbol"].endswith("USDT")]
        
        filtered_pairs = [
            x for x in usdt_pairs 
            if float(x["quoteVolume"]) >= MIN_VOLUME
        ]
        
        sorted_pairs = sorted(filtered_pairs, key=lambda x: float(x["quoteVolume"]), reverse=True)
        return [x["symbol"] for x in sorted_pairs[:10]]  # Тільки 10 монет
    except Exception as e:
        logger.error(f"Помилка отримання топ монет: {e}")
        return ["BTCUSDT", "ETHUSDT", "BNBUSDT"]

def get_historical_data(symbol, interval, limit=50):
    cache_key = f"{symbol}_{interval}"
    current_time = time.time()
    
    if cache_key in data_cache:
        data, timestamp = data_cache[cache_key]
        if current_time - timestamp < CACHE_DURATION:
            return data
    
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        response = requests.get(url, timeout=10)
        data = response.json()
        ohlc = []
        for d in data:
            ohlc.append({
                "time": datetime.fromtimestamp(d[0] / 1000),
                "open": float(d[1]),
                "high": float(d[2]),
                "low": float(d[3]),
                "close": float(d[4]),
                "volume": float(d[5])
            })
        
        data_cache[cache_key] = (ohlc, current_time)
        return ohlc
    except Exception as e:
        logger.error(f"Помилка отримання даних для {symbol}: {e}")
        return []

def calculate_ema(prices, period):
    if len(prices) < period:
        return None
    return prices[-1]  # Спрощено для тесту

def calculate_rsi(prices, period):
    if len(prices) < period + 1:
        return 50  # Нейтральне значення
    return 50  # Спрощено

def calculate_indicators(ohlc):
    closes = [c["close"] for c in ohlc]
    return {
        "fast_ema": calculate_ema(closes, FAST_EMA),
        "slow_ema": calculate_ema(closes, SLOW_EMA),
        "rsi": calculate_rsi(closes, RSI_PERIOD),
        "macd_histogram": 0,  # Спрощено
        "volume_ratio": 1.0
    }

def analyze_phase(ohlc):
    if len(ohlc) < 10:
        return "HOLD", 0, 0.1, {}, False
    
    indicators = calculate_indicators(ohlc)
    current_price = ohlc[-1]["close"]
    prev_price = ohlc[-2]["close"] if len(ohlc) >= 2 else current_price
    
    # Простий аналіз тренду
    price_change = ((current_price - prev_price) / prev_price) * 100
    
    if price_change > 1.0:
        return "BUY", abs(price_change), 0.6, indicators, True
    elif price_change < -1.0:
        return "SELL", abs(price_change), 0.6, indicators, True
    else:
        return "HOLD", 0, 0.1, indicators, False

def send_signal(symbol, signal, price, volatility, confidence, indicators, timeframe_confirmation):
    global last_signals
    
    if signal == "HOLD":
        return
        
    current_time = datetime.now()
    if symbol in last_signals:
        last_time = last_signals[symbol]["time"]
        if (current_time - last_time).total_seconds() < 3600:
            return
    
    # Прості розрахунки TP/SL
    if signal == "BUY":
        tp = round(price * 1.02, 4)  # +2%
        sl = round(price * 0.98, 4)  # -2%
    else:
        tp = round(price * 0.98, 4)  # -2%
        sl = round(price * 1.02, 4)  # +2%
    
    signal_data = {
        "symbol": symbol,
        "signal": signal,
        "price": price,
        "tp": tp,
        "sl": sl,
        "confidence": confidence,
        "time": current_time
    }
    
    last_signals[symbol] = signal_data
    
    # Надсилання повідомлення
    emoji = "🚀" if signal == "BUY" else "🔻"
    msg = (
        f"{emoji} *{symbol}* | {signal}\n"
        f"💰 Ціна: `{price}`\n"
        f"🎯 TP: `{tp}`\n"
        f"🛑 SL: `{sl}`\n"
        f"📈 Впевненість: {confidence*100:.1f}%\n"
        f"⏰ {current_time.strftime('%H:%M:%S')}"
    )
    
    try:
        bot.send_message(CHAT_ID, msg, parse_mode="Markdown")
        logger.info(f"Надіслано сигнал: {symbol} {signal}")
    except Exception as e:
        logger.error(f"Помилка відправки: {e}")

# -------------------------
# Основна функція перевірки
# -------------------------
def check_market():
    logger.info("Запуск перевірки ринку...")
    
    while True:
        try:
            symbols = get_top_symbols()
            logger.info(f"Перевіряємо {len(symbols)} монет: {symbols}")
            
            for symbol in symbols:
                try:
                    for tf in TIMEFRAMES:
                        ohlc = get_historical_data(symbol, tf, N_CANDLES)
                        if not ohlc:
                            continue
                            
                        signal, volatility, confidence, indicators, is_strong = analyze_phase(ohlc)
                        
                        if signal != "HOLD" and confidence >= MIN_CONFIDENCE_FOR_SIGNAL:
                            send_signal(symbol, signal, ohlc[-1]["close"], volatility, confidence, indicators, 1)
                            break  # Тільки один сигнал на монету
                            
                    time.sleep(1)  # Затримка між монетами
                    
                except Exception as e:
                    logger.error(f"Помилка перевірки {symbol}: {e}")
                    continue
            
            logger.info(f"Перевірка завершена. Очікування 30 секунд...")
            time.sleep(30)  # Чекаємо 30 секунд
            
        except Exception as e:
            logger.error(f"Критична помилка: {e}")
            time.sleep(60)  # Чекаємо хвилину при помилці

# -------------------------
# Вебхук та Flask
# -------------------------
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    try:
        json_str = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
    except Exception as e:
        logger.error(f"Помилка webhook: {e}")
    return "OK", 200

@app.route('/')
def home():
    return "Bot is running!", 200

@app.route('/status')
def status():
    return {
        "last_signals": len(last_signals),
        "status": "active",
        "timestamp": datetime.now().isoformat()
    }, 200

def setup_webhook():
    try:
        url = f"https://api.telegram.org/bot{API_KEY_TELEGRAM}/setWebhook"
        response = requests.post(url, data={"url": WEBHOOK_URL}, timeout=10)
        logger.info(f"Webhook setup: {response.json()}")
    except Exception as e:
        logger.error(f"Помилка налаштування webhook: {e}")

# -------------------------
# Запуск
# -------------------------
if __name__ == "__main__":
    logger.info("Запуск бота...")
    
    # Завантажуємо дані
    load_signals_history()
    
    # Налаштовуємо webhook
    setup_webhook()
    
    # Запускаємо перевірку ринку в окремому потоці
    market_thread = threading.Thread(target=check_market, daemon=True)
    market_thread.start()
    logger.info("Потік перевірки ринку запущено")
    
    # Запускаємо Flask
    try:
        app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
    except Exception as e:
        logger.error(f"Помилка запуску Flask: {e}")