import requests
import telebot
from flask import Flask, request
from datetime import datetime, timedelta
import threading
import time
import json
import pandas as pd
import numpy as np
from talib import abstract

# -------------------------
# Налаштування
# -------------------------
API_KEY_TELEGRAM = "8051222216:AAFORHEn1IjWllQyPp8W_1OY3gVxcBNVvZI"
CHAT_ID = "6053907025"
TIMEFRAMES = ["5m", "15m", "1h", "4h"]
N_CANDLES = 50  # Збільшено для кращого аналізу
FAST_EMA = 10
SLOW_EMA = 30
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# Додаткові налаштування
MIN_VOLUME = 1_000_000  # Мінімальний обсяг торгів
MIN_PRICE_CHANGE = 2.0  # Мінімальний відсоток зміни ціни для фільтрації
CONFIRMATION_THRESHOLD = 0.75  # 75% таймфреймів мають підтверджувати сигнал

WEBHOOK_HOST = "https://troovy-detective-bot-1-4on4.onrender.com"
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = WEBHOOK_HOST + WEBHOOK_PATH

bot = telebot.TeleBot(API_KEY_TELEGRAM)
app = Flask(__name__)

last_signals = {}   # останні сигнали по монетах
last_status = {}    # останній стан по монетах
performance_stats = {}  # статистика продуктивності сигналів

# -------------------------
# Удосконалений пошук топ монет
# -------------------------
def get_top_symbols(min_volume=MIN_VOLUME, min_price_change=MIN_PRICE_CHANGE):
    url = "https://api.binance.com/api/v3/ticker/24hr"
    try:
        data = requests.get(url, timeout=10).json()
        usdt_pairs = [x for x in data if x["symbol"].endswith("USDT")]
        
        # Фільтрація за обсягом та зміною ціни
        filtered_pairs = [
            x for x in usdt_pairs 
            if float(x["quoteVolume"]) >= min_volume and 
            abs(float(x["priceChangePercent"])) >= min_price_change
        ]
        
        # Сортування за комбінацією обсягу та волатильності
        sorted_pairs = sorted(
            filtered_pairs, 
            key=lambda x: (float(x["quoteVolume"]) * abs(float(x["priceChangePercent"]))), 
            reverse=True
        )
        return [x["symbol"] for x in sorted_pairs[:20]]  # Обмежуємо до 20 монет
    except Exception as e:
        print(f"Помилка отримання топ монет: {e}")
        return ["BTCUSDT", "ETHUSDT", "BNBUSDT"]  # Резервний список

# -------------------------
# Історичні дані з кешуванням
# -------------------------
data_cache = {}
CACHE_DURATION = 30  # секунди

def get_historical_data(symbol, interval, limit=100):
    cache_key = f"{symbol}_{interval}"
    current_time = time.time()
    
    # Перевірка кешу
    if cache_key in data_cache:
        data, timestamp = data_cache[cache_key]
        if current_time - timestamp < CACHE_DURATION:
            return data
    
    # Отримання нових даних
    try:
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
        
        # Оновлення кешу
        data_cache[cache_key] = (ohlc, current_time)
        return ohlc
    except Exception as e:
        print(f"Помилка отримання даних для {symbol}: {e}")
        return []

# -------------------------
# Розширені технічні індикатори
# -------------------------
def calculate_indicators(ohlc):
    closes = np.array([c["close"] for c in ohlc], dtype=float)
    highs = np.array([c["high"] for c in ohlc], dtype=float)
    lows = np.array([c["low"] for c in ohlc], dtype=float)
    volumes = np.array([c["volume"] for c in ohlc], dtype=float)
    
    # EMA
    fast_ema = calculate_ema(closes, FAST_EMA)
    slow_ema = calculate_ema(closes, SLOW_EMA)
    
    # RSI
    rsi = calculate_rsi(closes, RSI_PERIOD)
    
    # MACD
    macd, macd_signal, macd_histogram = calculate_macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    
    # Волатильність (ATR)
    atr = calculate_atr(highs, lows, closes, 14)
    
    # Обсяги
    volume_avg = np.mean(volumes[-5:])  # Середній обсяг за останні 5 періодів
    volume_current = volumes[-1]
    
    return {
        "fast_ema": fast_ema,
        "slow_ema": slow_ema,
        "rsi": rsi,
        "macd": macd,
        "macd_signal": macd_signal,
        "macd_histogram": macd_histogram,
        "atr": atr,
        "volume_avg": volume_avg,
        "volume_current": volume_current,
        "volume_ratio": volume_current / volume_avg if volume_avg > 0 else 1
    }

def calculate_ema(prices, period):
    if len(prices) < period:
        return None
    weights = np.exp(np.linspace(-1., 0., period))
    weights /= weights.sum()
    return np.convolve(prices, weights, mode='valid')[-1]

def calculate_rsi(prices, period):
    if len(prices) < period + 1:
        return None
        
    deltas = np.diff(prices)
    seed = deltas[:period+1]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period
    
    if down == 0:
        return 100
    rs = up / down
    return 100 - (100 / (1 + rs))

def calculate_macd(prices, fast_period, slow_period, signal_period):
    if len(prices) < slow_period + signal_period:
        return None, None, None
        
    ema_fast = calculate_ema(prices, fast_period)
    ema_slow = calculate_ema(prices, slow_period)
    
    if ema_fast is None or ema_slow is None:
        return None, None, None
        
    macd_line = ema_fast - ema_slow
    macd_signal = calculate_ema(prices[-signal_period:], signal_period) if len(prices) >= signal_period else None
    macd_histogram = macd_line - macd_signal if macd_signal is not None else None
    
    return macd_line, macd_signal, macd_histogram

def calculate_atr(highs, lows, closes, period):
    if len(highs) < period + 1:
        return None
        
    tr = np.maximum(highs[-1] - lows[-1], 
                   np.maximum(abs(highs[-1] - closes[-2]), 
                             abs(lows[-1] - closes[-2])))
    return tr

# -------------------------
# Удосконалений аналіз сигналів
# -------------------------
def analyze_phase(ohlc):
    if len(ohlc) < N_CANDLES:
        return "HOLD", 0, 0, {}, False
    
    closes = [c["close"] for c in ohlc]
    highs = [c["high"] for c in ohlc]
    lows = [c["low"] for c in ohlc]
    
    # Розрахунок індикаторів
    indicators = calculate_indicators(ohlc)
    
    # Базові сигнали
    trend_up = closes[-2] < closes[-1]
    trend_down = closes[-2] > closes[-1]
    
    # Сигнали EMA
    ema_bullish = indicators["fast_ema"] > indicators["slow_ema"] if indicators["fast_ema"] and indicators["slow_ema"] else False
    ema_bearish = indicators["fast_ema"] < indicators["slow_ema"] if indicators["fast_ema"] and indicators["slow_ema"] else False
    
    # Сигнали RSI
    rsi = indicators["rsi"]
    rsi_overbought = rsi > 70 if rsi else False
    rsi_oversold = rsi < 30 if rsi else False
    
    # Сигнали MACD
    macd_bullish = indicators["macd_histogram"] > 0 if indicators["macd_histogram"] is not None else False
    macd_bearish = indicators["macd_histogram"] < 0 if indicators["macd_histogram"] is not None else False
    
    # Аналіз обсягів
    volume_spike = indicators["volume_ratio"] > 1.5 if indicators["volume_ratio"] else False
    
    # Оцінка сигналів
    buy_signals = sum([ema_bullish, not rsi_overbought, macd_bullish, trend_up, volume_spike])
    sell_signals = sum([ema_bearish, not rsi_oversold, macd_bearish, trend_down, volume_spike])
    
    # Визначення волатильності через ATR
    volatility = indicators["atr"] or (max(highs[-10:]) - min(lows[-10:])) / 2
    
    # Рішення
    confidence = abs(buy_signals - sell_signals) / 5  # 5 - кількість сигналів
    
    if buy_signals > sell_signals and buy_signals >= 3:
        return "BUY", volatility, confidence, indicators, True
    elif sell_signals > buy_signals and sell_signals >= 3:
        return "SELL", volatility, confidence, indicators, True
    else:
        return "HOLD", volatility, confidence, indicators, False

# -------------------------
# Відправка сигналу з покращеним форматуванням
# -------------------------
def send_signal(symbol, signal, price, volatility, confidence, indicators, timeframe_confirmation):
    global last_signals
    
    if signal == "HOLD":
        return
        
    # Уникаємо дублювання сигналів
    current_time = datetime.now()
    if symbol in last_signals:
        last_signal_time = last_signals[symbol]["time"]
        if (current_time - last_signal_time).total_seconds() < 3600:  # 1 година
            if last_signals[symbol]["signal"] == signal:
                return  # Пропускаємо повторний сигнал
    
    # Розрахунок TP/SL на основі ATR
    atr_multiplier_tp = 1.5 if confidence > 0.7 else 1.0
    atr_multiplier_sl = 1.0 if confidence > 0.7 else 0.7
    
    tp = round(price + volatility * atr_multiplier_tp if signal == "BUY" else price - volatility * atr_multiplier_tp, 4)
    sl = round(price - volatility * atr_multiplier_sl if signal == "BUY" else price + volatility * atr_multiplier_sl, 4)
    
    # Ризик-менеджмент (не більше 2% втрат на угоду)
    risk_percentage = 0.02
    position_size = risk_percentage / ((abs(price - sl)) / price) if price != sl else 0
    
    last_signals[symbol] = {
        "signal": signal,
        "price": price,
        "tp": tp,
        "sl": sl,
        "confidence": confidence,
        "time": current_time,
        "timeframe_confirmation": timeframe_confirmation,
        "indicators": indicators,
        "position_size": position_size
    }
    
    # Формування повідомлення
    emoji = "🚀" if signal == "BUY" else "🔻"
    rsi_status = f"RSI: {indicators['rsi']:.1f}" if indicators['rsi'] else "RSI: N/A"
    macd_status = f"MACD: {'↑' if indicators['macd_histogram'] > 0 else '↓'}" if indicators['macd_histogram'] is not None else "MACD: N/A"
    
    note = "✅ Високе підтвердження" if confidence > 0.7 else "⚠️ Помірне підтвердження"
    if timeframe_confirmation < len(TIMEFRAMES) * CONFIRMATION_THRESHOLD:
        note = f"⚠️ Лише {timeframe_confirmation}/{len(TIMEFRAMES)} ТФ"
    
    msg = (
        f"{emoji} *{symbol}* | {signal}\n"
        f"💰 Ціна: `{price}`\n"
        f"🎯 TP: `{tp}` | 🛑 SL: `{sl}`\n"
        f"📊 {rsi_status} | {macd_status} | Обсяг: x{indicators['volume_ratio']:.1f}\n"
        f"📈 Впевненість: {confidence*100:.1f}%\n"
        f"💼 Розмір позиції: {position_size*100:.1f}% балансу\n"
        f"_{note}_"
    )
    
    try:
        bot.send_message(CHAT_ID, msg, parse_mode="Markdown")
        
        # Логування сигналу
        with open("signals.log", "a", encoding="utf-8") as f:
            log_msg = (
                f"{current_time} | {symbol} | {signal} | {price} | "
                f"TP: {tp} | SL: {sl} | Confidence: {confidence:.2f} | "
                f"RSI: {indicators['rsi'] or 'N/A'} | Volume: x{indicators['volume_ratio']:.1f}\n"
            )
            f.write(log_msg)
            
        # Оновлення статистики
        update_performance_stats(symbol, signal, price)
            
    except Exception as e:
        print(f"Помилка відправки повідомлення: {e}")
        with open("errors.log", "a") as f:
            f.write(f"{datetime.now()} - Помилка відправки: {e}\n")

# -------------------------
# Статистика продуктивності сигналів
# -------------------------
def update_performance_stats(symbol, signal, price):
    if symbol not in performance_stats:
        performance_stats[symbol] = {
            "buy_signals": 0,
            "sell_signals": 0,
            "last_signal": signal,
            "last_price": price,
            "profitability": 0,
            "total_signals": 0,
            "successful_signals": 0
        }
    
    stats = performance_stats[symbol]
    stats["total_signals"] += 1
    
    if signal == "BUY":
        stats["buy_signals"] += 1
    else:
        stats["sell_signals"] += 1
        
    # Оновлення прибутковості (спрощено)
    if stats["last_signal"] and stats["last_price"]:
        price_change = (price - stats["last_price"]) / stats["last_price"] * 100
        
        if (stats["last_signal"] == "BUY" and price_change > 0) or \
           (stats["last_signal"] == "SELL" and price_change < 0):
            stats["successful_signals"] += 1
            
        stats["profitability"] = stats["successful_signals"] / stats["total_signals"] * 100
        
    stats["last_signal"] = signal
    stats["last_price"] = price
    
    # Збереження статистики
    with open("performance_stats.json", "w") as f:
        json.dump(performance_stats, f)

# -------------------------
# Перевірка ринку
# -------------------------
def check_market():
    global last_status
    while True:
        try:
            symbols = get_top_symbols()
            print(f"{datetime.now()} - Перевірка {len(symbols)} монет...")
            
            for symbol in symbols:
                signals = []
                volatilities = []
                confidences = []
                all_indicators = []
                last_prices = []
                
                for tf in TIMEFRAMES:
                    ohlc = get_historical_data(symbol, tf, N_CANDLES)
                    if not ohlc or len(ohlc) < N_CANDLES:
                        continue
                        
                    signal, volatility, confidence, indicators, is_strong = analyze_phase(ohlc)
                    signals.append(signal)
                    volatilities.append(volatility)
                    confidences.append(confidence)
                    all_indicators.append(indicators)
                    last_prices.append(ohlc[-1]["close"])
                
                if not signals:
                    continue
                
                # Агрегація сигналів з різних таймфреймів
                buy_count = signals.count("BUY")
                sell_count = signals.count("SELL")
                total_tfs = len(signals)
                
                # Середня впевненість
                avg_confidence = sum(confidences) / total_tfs if total_tfs > 0 else 0
                
                # Визначення фінального сигналу
                final_signal = "HOLD"
                timeframe_confirmation = 0
                
                if buy_count >= total_tfs * CONFIRMATION_THRESHOLD:
                    final_signal = "BUY"
                    timeframe_confirmation = buy_count
                elif sell_count >= total_tfs * CONFIRMATION_THRESHOLD:
                    final_signal = "SELL"
                    timeframe_confirmation = sell_count
                
                # Відправка сигналу, якщо є підтвердження
                if final_signal != "HOLD":
                    # Використовуємо ціну з найбільшого таймфрейму для кращої точності
                    price = last_prices[-1] if last_prices else 0
                    max_volatility = max(volatilities) if volatilities else 0
                    
                    send_signal(
                        symbol, 
                        final_signal, 
                        price, 
                        max_volatility, 
                        avg_confidence,
                        all_indicators[-1],  # Індикатори з найбільшого TF
                        timeframe_confirmation
                    )
                
                # Оновлення статусу
                last_status[symbol] = {
                    "signals": signals,
                    "confidences": confidences,
                    "timeframes": TIMEFRAMES[:len(signals)],
                    "last_prices": last_prices,
                    "volatilities": volatilities,
                    "timestamp": datetime.now()
                }
                
                time.sleep(0.2)  # Невелика затримка між монетами

        except Exception as e:
            print(f"{datetime.now()} - Помилка: {e}")
            with open("errors.log", "a") as f:
                f.write(f"{datetime.now()} - {e}\n")
        
        time.sleep(30)  # Перевіряємо кожні 30 секунд

# -------------------------
# Вебхук Telegram з додатковими командами
# -------------------------
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    global last_status, performance_stats
    json_str = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])

    message_obj = update.message or update.edited_message
    if not message_obj:
        return "!", 200

    text = message_obj.text.strip()

    if text.startswith("/status"):
        args = text.split()
        if len(args) == 2:
            symbol = args[1].upper()
            if symbol in last_status:
                s = last_status[symbol]
                out = f"📊 *{symbol}*:\n\n"
                
                for i, tf in enumerate(s["timeframes"]):
                    sig = s["signals"][i]
                    conf = s["confidences"][i]
                    price = s["last_prices"][i]
                    vol = s["volatilities"][i]
                    
                    out += f"*{tf}:* {sig} (впевненість: {conf*100:.1f}%)\n"
                    out += f"Ціна: {price} | Волатильність: {vol:.4f}\n\n"
                
                # Додаємо статистику продуктивності
                if symbol in performance_stats:
                    stats = performance_stats[symbol]
                    out += f"📈 *Статистика продуктивності:*\n"
                    out += f"Прибутковість: {stats['profitability']:.1f}%\n"
                    out += f"Сигнали: {stats['total_signals']} (✅{stats['successful_signals']} | ❌{stats['total_signals'] - stats['successful_signals']})\n"
                    out += f"BUY/SELL: {stats['buy_signals']}/{stats['sell_signals']}"
                
                bot.send_message(message_obj.chat.id, out, parse_mode="Markdown")
            else:
                bot.send_message(message_obj.chat.id, f"❌ Немає даних для {symbol}")
        else:
            bot.send_message(message_obj.chat.id, "Використання: /status SYMBOL")

    elif text.startswith("/top"):
        symbols = get_top_symbols()[:10]
        msg = "🔥 *Топ-10 монет за волатильністю та обсягом:*\n\n"
        for i, symbol in enumerate(symbols, 1):
            msg += f"{i}. {symbol}\n"
        bot.send_message(message_obj.chat.id, msg, parse_mode="Markdown")

    elif text.startswith("/last"):
        if not last_signals:
            bot.send_message(message_obj.chat.id, "❌ Немає надісланих сигналів")
        else:
            msg = "📝 *Останні сигнали:*\n\n"
            for sym, info in list(last_signals.items())[-5:]:  # Останні 5 сигналів
                time_diff = (datetime.now() - info["time"]).total_seconds() / 60
                note = "✅ Високе" if info["confidence"] > 0.7 else "⚠️ Помірне"
                msg += (
                    f"*{sym}:* {info['signal']} ({time_diff:.1f} хв тому)\n"
                    f"Ціна: {info['price']} | Впевненість: {info['confidence']*100:.1f}%\n"
                    f"TP: {info['tp']} | SL: {info['sl']}\n\n"
                )
            bot.send_message(message_obj.chat.id, msg, parse_mode="Markdown")
            
    elif text.startswith("/performance"):
        if not performance_stats:
            bot.send_message(message_obj.chat.id, "❌ Немає даних про продуктивність")
        else:
            # Сортуємо за прибутковістю
            sorted_stats = sorted(
                performance_stats.items(), 
                key=lambda x: x[1]["profitability"], 
                reverse=True
            )[:10]  # Топ-10
            
            msg = "🏆 *Топ-10 монет за прибутковістю:*\n\n"
            for symbol, stats in sorted_stats:
                if stats["total_signals"] > 0:  # Показуємо тільки монети з сигналами
                    msg += (
                        f"*{symbol}:* {stats['profitability']:.1f}% "
                        f"({stats['successful_signals']}/{stats['total_signals']})\n"
                    )
            bot.send_message(message_obj.chat.id, msg, parse_mode="Markdown")
            
    elif text.startswith("/help"):
        help_msg = (
            "📖 *Доступні команди:*\n\n"
            "/status SYMBOL - стан монети на різних таймфреймах\n"
            "/top - топ монет за волатильністю\n"
            "/last - останні сигнали\n"
            "/performance - статистика продуктивності сигналів\n"
            "/help - довідка по командам"
        )
        bot.send_message(message_obj.chat.id, help_msg, parse_mode="Markdown")

    return "!", 200

# -------------------------
# Встановлення Webhook
# -------------------------
def setup_webhook():
    try:
        url = f"https://api.telegram.org/bot{API_KEY_TELEGRAM}/setWebhook"
        response = requests.post(url, data={"url": WEBHOOK_URL}, timeout=10)
        print("Webhook setup:", response.json())
    except Exception as e:
        print(f"Помилка налаштування webhook: {e}")

# -------------------------
# Завантаження статистики при запуску
# -------------------------
def load_performance_stats():
    global performance_stats
    try:
        with open("performance_stats.json", "r") as f:
            performance_stats = json.load(f)
    except FileNotFoundError:
        performance_stats = {}
    except Exception as e:
        print(f"Помилка завантаження статистики: {e}")
        performance_stats = {}

# -------------------------
# Запуск
# -------------------------
if __name__ == "__main__":
    load_performance_stats()
    setup_webhook()
    threading.Thread(target=check_market, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)