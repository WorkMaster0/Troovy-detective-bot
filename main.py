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
TIMEFRAMES = ['15m', '1h', '4h']  # Мульті-фрейм
N_CANDLES = 30
FAST_EMA = 10
SLOW_EMA = 30

bot = telebot.TeleBot(API_KEY_TELEGRAM)
app = Flask(__name__)

last_signal = None  # Для фільтра повторів
last_status = None  # Для команди /status

# -------------------------
# Отримання даних з Binance
# -------------------------
def get_historical_data(symbol, interval, limit=100):
    url = f'https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}'
    response = requests.get(url, timeout=10)
    if response.status_code != 200:
        raise Exception(f"Помилка API Binance: {response.status_code}")
    data = response.json()
    ohlc = []
    for d in data:
        timestamp = datetime.fromtimestamp(d[0]/1000)
        ohlc.append({
            'time': timestamp,
            'open': float(d[1]),
            'high': float(d[2]),
            'low': float(d[3]),
            'close': float(d[4]),
            'volume': float(d[5])
        })
    return ohlc

# -------------------------
# Розрахунок EMA
# -------------------------
def calculate_ema(closes, period):
    ema = closes[0]
    k = 2 / (period + 1)
    for price in closes[1:]:
        ema = price * k + ema * (1 - k)
    return ema

# -------------------------
# Аналіз фаз Вайкоффа + EMA
# -------------------------
def analyze_phase(ohlc):
    closes = [c['close'] for c in ohlc][-N_CANDLES:]
    volumes = [c['volume'] for c in ohlc][-N_CANDLES:]
    highs = [c['high'] for c in ohlc][-N_CANDLES:]
    lows = [c['low'] for c in ohlc][-N_CANDLES:]

    last_close = closes[-1]
    last_volume = volumes[-1]
    avg_volume = sum(volumes)/len(volumes)
    recent_high = max(closes)
    recent_low = min(closes)
    volatility = max(highs) - min(lows)

    # Тренд останніх 3 свічок
    trend_up = closes[-3] < closes[-2] < closes[-1]
    trend_down = closes[-3] > closes[-2] > closes[-1]

    # EMA
    fast_ema = calculate_ema(closes[-FAST_EMA:], FAST_EMA)
    slow_ema = calculate_ema(closes[-SLOW_EMA:], SLOW_EMA)

    ema_confirm = None
    if fast_ema > slow_ema:
        ema_confirm = 'BUY'
    elif fast_ema < slow_ema:
        ema_confirm = 'SELL'

    # Сигнал Вайкоффа + EMA
    if last_close <= recent_low*1.01 and last_volume > avg_volume and trend_up and ema_confirm == 'BUY':
        return 'BUY', volatility, True, ema_confirm, trend_up
    elif last_close >= recent_high*0.99 and last_volume > avg_volume and trend_down and ema_confirm == 'SELL':
        return 'SELL', volatility, True, ema_confirm, trend_down
    else:
        return 'HOLD', volatility, False, ema_confirm, None

# -------------------------
# Відправка сигналу з динамічним TP/SL
# -------------------------
def send_signal(signal, price, max_volatility):
    global last_signal
    if signal == last_signal or signal == "HOLD":
        return
    last_signal = signal

    tp = round(price + max_volatility*0.5 if signal=="BUY" else price - max_volatility*0.5, 2)
    sl = round(price - max_volatility*0.3 if signal=="BUY" else price + max_volatility*0.3, 2)

    message = f"Сигнал: {signal}\nЦіна: {price}\nTake-profit: {tp}\nStop-loss: {sl}"
    bot.send_message(CHAT_ID, message)

    with open("signals.log", "a") as f:
        f.write(f"{datetime.now()} | {signal} | {price} | TP: {tp} | SL: {sl}\n")

# -------------------------
# Перевірка мульті-фрейм
# -------------------------
def check_market():
    global last_status
    while True:
        try:
            signals = []
            volatilities = []
            last_prices = []
            ema_confirms = []
            trends = []
            for tf in TIMEFRAMES:
                ohlc = get_historical_data(SYMBOL, tf)
                signal, volatility, ema_ok, ema_signal, trend = analyze_phase(ohlc)
                signals.append(signal)
                volatilities.append(volatility)
                last_prices.append(ohlc[-1]['close'])
                ema_confirms.append(ema_ok)
                trends.append((ema_signal, trend))

            if len(set(signals)) == 1 and signals[0] != "HOLD" and all(ema_confirms):
                send_signal(signals[0], last_prices[-1], max(volatilities))

            # Збереження останнього статусу для команди /status
            last_status = {
                "signals": signals,
                "ema_confirms": ema_confirms,
                "trends": trends,
                "timeframes": TIMEFRAMES,
                "last_prices": last_prices,
                "volatilities": volatilities
            }

        except Exception as e:
            print(f"{datetime.now()} - Помилка: {e}")
            with open("errors.log", "a") as f:
                f.write(f"{datetime.now()} - {e}\n")
        time.sleep(60)

# -------------------------
# Вебхук для Telegram
# -------------------------
@app.route(f'/{API_KEY_TELEGRAM}', methods=['POST'])
def webhook():
    json_str = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])

    # Обробка команди /status
    if update.message and update.message.text == "/status":
        global last_status
        if not last_status:
            bot.send_message(update.message.chat.id, "Поки немає даних.")
        else:
            text = "Статус сигналів:\n"
            buy_count = 0
            sell_count = 0
            for i, tf in enumerate(last_status["timeframes"]):
                sig = last_status["signals"][i]
                ema_signal = last_status["trends"][i][0]
                trend = last_status["trends"][i][1]
                price = last_status['last_prices'][i]
                vol = last_status['volatilities'][i]
                text += f"{tf}: Сигнал {sig}, EMA {ema_signal}, Тренд {'UP' if trend else 'DOWN' if trend==False else '—'}, Ціна {price}, Волатильність {vol:.2f}\n"
                if sig == "BUY":
                    buy_count += 1
                elif sig == "SELL":
                    sell_count += 1

            total = len(last_status["timeframes"])
            text += f"\nПідтверджено BUY: {buy_count}/{total} ({buy_count/total*100:.0f}%)\n"
            text += f"Підтверджено SELL: {sell_count}/{total} ({sell_count/total*100:.0f}%)"

            bot.send_message(update.message.chat.id, text)

    return "!", 200

# -------------------------
# Запуск сервера
# -------------------------
if __name__ == "__main__":
    threading.Thread(target=check_market, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)