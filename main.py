import requests
import telebot
from flask import Flask, request
from datetime import datetime
import threading
import time
import ccxt   # <-- для торгівлі на Gate.io

# -------------------------
# Налаштування
# -------------------------
API_KEY_TELEGRAM = "8051222216:AAFORHEn1IjWllQyPp8W_1OY3gVxcBNVvZI"
CHAT_ID = "6053907025"
TIMEFRAMES = ["5m", "15m", "1h", "4h"]
N_CANDLES = 30
FAST_EMA = 10
SLOW_EMA = 30

WEBHOOK_HOST = "https://your-app-name.onrender.com"
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = WEBHOOK_HOST + WEBHOOK_PATH

# Gate API (фʼючерси USDT)
GATE_API_KEY = "cf99af3f8c0c1a711408f1a1970be8be"
GATE_API_SECRET = "4bd0a51eac2133386e60f4c5e1a78ea9c364e542399bc1865e679f509e93f72e"

gate = ccxt.gateio({
    "apiKey": GATE_API_KEY,
    "secret": GATE_API_SECRET,
    "options": {"defaultType": "swap"}   # дуже важливо: тільки тоді буде futures
})

TRADE_AMOUNT_USD = 10   # розмір тестової позиції

bot = telebot.TeleBot(API_KEY_TELEGRAM)
app = Flask(__name__)

last_signals = {}   # останні сигнали по монетах
last_status = {}    # останній стан по монетах

# -------------------------
# Топ монет по волатильності
# -------------------------
def get_top_symbols(min_volume=1_000_000):
    url = "https://api.binance.com/api/v3/ticker/24hr"
    data = requests.get(url, timeout=10).json()
    usdt_pairs = [x for x in data if x["symbol"].endswith("USDT")]
    filtered_pairs = [x for x in usdt_pairs if float(x["quoteVolume"]) >= min_volume]
    sorted_pairs = sorted(filtered_pairs, key=lambda x: abs(float(x["priceChangePercent"])), reverse=True)
    return [x["symbol"] for x in sorted_pairs]

# -------------------------
# Історичні дані (з Binance для аналізу)
# -------------------------
def get_historical_data(symbol, interval, limit=100):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    data = response.json()
    ohlc = []
    for d in data:
        timestamp = datetime.fromtimestamp(d[0] / 1000)
        ohlc.append({
            "time": timestamp,
            "open": float(d[1]),
            "high": float(d[2]),
            "low": float(d[3]),
            "close": float(d[4]),
            "volume": float(d[5])
        })
    return ohlc

# -------------------------
# EMA
# -------------------------
def calculate_ema(closes, period):
    ema = closes[0]
    k = 2 / (period + 1)
    for price in closes[1:]:
        ema = price * k + ema * (1 - k)
    return ema

# -------------------------
# Аналіз сигналів
# -------------------------
def analyze_phase(ohlc):
    closes = [c["close"] for c in ohlc][-N_CANDLES:]
    highs = [c["high"] for c in ohlc][-N_CANDLES:]
    lows = [c["low"] for c in ohlc][-N_CANDLES:]

    last_close = closes[-1]
    volatility = max(highs) - min(lows)

    trend_up = closes[-2] < closes[-1]
    trend_down = closes[-2] > closes[-1]

    fast_ema = calculate_ema(closes[-FAST_EMA:], FAST_EMA)
    slow_ema = calculate_ema(closes[-SLOW_EMA:], SLOW_EMA)

    ema_confirm = None
    if fast_ema > slow_ema:
        ema_confirm = "BUY"
    elif fast_ema < slow_ema:
        ema_confirm = "SELL"

    if trend_up and ema_confirm == "BUY":
        return "BUY", volatility, True, ema_confirm, trend_up
    elif trend_down and ema_confirm == "SELL":
        return "SELL", volatility, True, ema_confirm, trend_down
    else:
        return "HOLD", volatility, False, ema_confirm, None

# -------------------------
# Торгівля на Gate Futures
# -------------------------
def place_order(symbol, side, amount_usd, tp, sl):
    try:
        # Binance дає формат BTCUSDT → для Gate Futures треба BTC/USDT:USDT
        pair = symbol.replace("USDT", "/USDT:USDT")

        ticker = gate.fetch_ticker(pair)
        coin_price = ticker["last"]
        amount = amount_usd / coin_price

        # Ринковий ордер
        order = gate.create_order(
            symbol=pair,
            type="market",
            side=side.lower(),
            amount=amount
        )

        print("✅ Ордер виконано:", order)

        # Gate futures TP/SL можна виставляти окремими ордерами
        tp_order = gate.create_order(
            symbol=pair,
            type="limit",
            side="sell" if side == "BUY" else "buy",
            amount=amount,
            price=tp,
            params={"reduceOnly": True}
        )
        sl_order = gate.create_order(
            symbol=pair,
            type="stop",
            side="sell" if side == "BUY" else "buy",
            amount=amount,
            price=sl,
            params={"stopPrice": sl, "reduceOnly": True}
        )

        print("🎯 TP виставлено:", tp_order)
        print("🛑 SL виставлено:", sl_order)

    except Exception as e:
        print("❌ Помилка ордера:", e)

# -------------------------
# Відправка сигналу
# -------------------------
def send_signal(symbol, signal, price, max_volatility, confidence):
    global last_signals
    if signal == "HOLD":
        return

    total_tfs = len(TIMEFRAMES)
    last_signals[symbol] = {
        "signal": signal,
        "price": price,
        "tp": round(price + max_volatility * 0.5 if signal == "BUY" else price - max_volatility * 0.5, 4),
        "sl": round(price - max_volatility * 0.3 if signal == "BUY" else price + max_volatility * 0.3, 4),
        "confidence": confidence,
        "time": datetime.now()
    }

    note = "✅ Підтверджено всіма ТФ" if confidence == total_tfs else f"⚠️ Лише {confidence}/{total_tfs} ТФ співпали"
    msg = (
        f"📢 {symbol}\nСигнал: {signal}\n💰 Ціна: {price}\n"
        f"🎯 TP: {last_signals[symbol]['tp']}\n🛑 SL: {last_signals[symbol]['sl']}\n{note}"
    )
    bot.send_message(CHAT_ID, msg)

    # 🚀 Автоматична торгівля (тільки якщо всі ТФ збігаються)
    if confidence == total_tfs:
        place_order(symbol, signal, TRADE_AMOUNT_USD, last_signals[symbol]["tp"], last_signals[symbol]["sl"])

# -------------------------
# Перевірка ринку
# -------------------------
def check_market():
    global last_status
    while True:
        try:
            symbols = get_top_symbols()
            for symbol in symbols:
                signals, volatilities, last_prices = [], [], []
                for tf in TIMEFRAMES:
                    ohlc = get_historical_data(symbol, tf)
                    signal, volatility, _, ema_signal, trend = analyze_phase(ohlc)
                    signals.append(signal)
                    volatilities.append(volatility)
                    last_prices.append(ohlc[-1]["close"])

                buy_count = signals.count("BUY")
                sell_count = signals.count("SELL")
                total_tfs = len(TIMEFRAMES)

                if len(set(signals)) == 1 and signals[0] != "HOLD":
                    send_signal(symbol, signals[0], last_prices[-1], max(volatilities), total_tfs)
                elif buy_count >= total_tfs - 1:
                    send_signal(symbol, "BUY", last_prices[-1], max(volatilities), buy_count)
                elif sell_count >= total_tfs - 1:
                    send_signal(symbol, "SELL", last_prices[-1], max(volatilities), sell_count)

                time.sleep(0.5)

        except Exception as e:
            print(f"{datetime.now()} - Помилка: {e}")
            with open("errors.log", "a") as f:
                f.write(f"{datetime.now()} - {e}\n")
        time.sleep(10)

# -------------------------
# Webhook Telegram
# -------------------------
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    json_str = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "!", 200

# -------------------------
# Setup Webhook
# -------------------------
def setup_webhook():
    url = f"https://api.telegram.org/bot{API_KEY_TELEGRAM}/setWebhook"
    response = requests.post(url, data={"url": WEBHOOK_URL})
    print("Webhook setup:", response.json())

# -------------------------
# Запуск
# -------------------------
if __name__ == "__main__":
    setup_webhook()
    threading.Thread(target=check_market, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)