# main.py
import requests
from datetime import datetime
import telegram

# -------------------------
# Налаштування
# -------------------------
API_KEY_TELEGRAM = '8063113740:AAGC-9PHzZD65jPad2lxP5mTmlWuQwvKwrU'
CHAT_ID = '6053907025'
SYMBOL = 'BTCUSDT'
INTERVAL = '1h'  # таймфрейм свічки

bot = telegram.Bot(token=API_KEY_TELEGRAM)

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
    # Проста логіка: якщо ціна останньої свічки > середнє останніх 20 свічок → Buy сигнал
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
    bot.send_message(chat_id=CHAT_ID, text=message)

# -------------------------
# Головна функція
# -------------------------
def main():
    ohlc = get_historical_data(SYMBOL, INTERVAL)
    signal = analyze_phase(ohlc)
    send_signal(signal, ohlc[-1]['close'])

if __name__ == "__main__":
    main()
