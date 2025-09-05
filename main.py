# main.py
import requests
import telebot
from flask import Flask, request
from datetime import datetime
import threading
import time

# -------------------------
# Налаштування
# -------------------------
API_KEY_TELEGRAM = '8051222216:AAFORHEn1IjWllQyPp8W_1OY3gVxcBNVvZI'
CHAT_ID = '6053907025'
SYMBOL = 'BTCUSDT'
INTERVAL = '1h'  # таймфрейм свічки

bot = telebot.TeleBot(API_KEY_TELEGRAM)
app = Flask(__name__)

# -------------------------
# Отримання даних з Binance
# -------------------------
def get_historical_data(symbol, interval, limit=100):
    url = f'https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}'
    data = requests.get(url).json()
    ohlc = []
    for d in data:
        timestamp = datetime.fromtimestamp(d[0]/1000)
        open_price = float(d[1])
        high = float(d[2])
        low = float(d[3])
        close = float(d[4])
        volume = float(d[5])
        ohlc.append({'time': timestamp, 'open': open_price, 'high': high, 'low': low, 'close': close, 'volume': volume})
    return ohlc

# -------------------------
# Базовий аналіз фаз Вайкоффа
# -------------------------
def analyze_phase(ohlc):
    closes = [c['close'] for c in ohlc]
    avg_close = sum(closes[-20:])/20
    last_close = closes[-1]

    if last_close > avg_close:
        return 'BUY'
    elif last_close < avg_close:
        return 'SELL'
    else:
        return 'HOLD'

# -------------------------
# Відправка сигналу у Telegram
# -------------------------
def send_signal(signal, price):
    message = f"Сигнал: {signal}\nЦіна: {price}"
    bot.send_message(CHAT_ID, message)

# -------------------------
# Функція перевірки кожну хвилину
# -------------------------
def check_market():
    while True:
        try:
            ohlc = get_historical_data(SYMBOL, INTERVAL)
            signal = analyze_phase(ohlc)
            send_signal(signal, ohlc[-1]['close'])
        except Exception as e:
            print("Помилка:", e)
        time.sleep(60)  # перевірка кожну хвилину

# -------------------------
# Вебхук для Telegram
# -------------------------
@app.route(f'/{API_KEY_TELEGRAM}', methods=['POST'])
def webhook():
    json_str = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "!", 200

# -------------------------
# Запуск сервера і перевірки ринку
# -------------------------
if __name__ == "__main__":
    # Старт циклічної перевірки в окремому потоці
    threading.Thread(target=check_market, daemon=True).start()
    # Старт Flask сервера
    app.run(host="0.0.0.0", port=5000)