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

# -------------------------
# Налаштування
# -------------------------
API_KEY_TELEGRAM = "8051222216:AAFORHEn1IjWllQyPp8W_1OY3gVxcBNVvZI"
CHAT_ID = "6053907025"
TIMEFRAMES = ["15m", "1h", "4h"]  # Видалено 5m - занадто шумний
N_CANDLES = 100  # Збільшено для кращої точності
FAST_EMA = 12
SLOW_EMA = 26
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# Додаткові налаштування
MIN_VOLUME = 5_000_000  # Збільшено мінімальний об'єм
MIN_PRICE_CHANGE = 3.0  # Збільшено мінімальну зміну ціни
CONFIRMATION_THRESHOLD = 0.8  # Збільшено поріг підтвердження
MIN_CONFIDENCE_FOR_SIGNAL = 0.65  # Мінімальна впевненість для сигналу
MIN_CONFIDENCE_FOR_HISTORY = 0.6  # Збільшено для історії

WEBHOOK_HOST = "https://troovy-detective-bot-1-4on4.onrender.com"
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = WEBHOOK_HOST + WEBHOOK_PATH

# Додаткові API endpoints для ротації
BINANCE_API_URLS = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com"
]

bot = telebot.TeleBot(API_KEY_TELEGRAM)
app = Flask(__name__)

last_signals = {}
last_status = {}
performance_stats = {}
signal_history = []  # Історія всіх сигналів

# Файл для збереження історії сигналів
SIGNALS_HISTORY_FILE = "signals_history.json"

# Налаштування кешу
data_cache = {}
CACHE_DURATION = 30
CACHE_CLEANUP_INTERVAL = 300

# -------------------------
# Завантаження та збереження історії сигналів
# -------------------------
def load_signals_history():
    global signal_history
    try:
        if os.path.exists(SIGNALS_HISTORY_FILE):
            with open(SIGNALS_HISTORY_FILE, "r") as f:
                signal_history = json.load(f)
                for signal in signal_history:
                    if isinstance(signal["time"], str):
                        signal["time"] = datetime.fromisoformat(signal["time"])
    except Exception as e:
        print(f"Помилка завантаження історії сигналів: {e}")
        signal_history = []

def save_signals_history():
    try:
        history_to_save = []
        for signal in signal_history:
            signal_copy = signal.copy()
            if isinstance(signal_copy["time"], datetime):
                signal_copy["time"] = signal_copy["time"].isoformat()
            history_to_save.append(signal_copy)
            
        with open(SIGNALS_HISTORY_FILE, "w") as f:
            json.dump(history_to_save, f, indent=2)
    except Exception as e:
        print(f"Помилка збереження історії сигналів: {e}")

# -------------------------
# Удосконалений пошук топ монет
# -------------------------
def get_top_symbols(min_volume=MIN_VOLUME, min_price_change=MIN_PRICE_CHANGE):
    url = "https://api.binance.com/api/v3/ticker/24hr"
    try:
        data = requests.get(url, timeout=10).json()
        usdt_pairs = [x for x in data if x["symbol"].endswith("USDT")]
        
        filtered_pairs = [
            x for x in usdt_pairs 
            if float(x["quoteVolume"]) >= min_volume and 
            abs(float(x["priceChangePercent"])) >= min_price_change and
            not x["symbol"].startswith(('BUSD', 'USDC', 'TUSD'))  # Виключаємо стейблкоїни
        ]
        
        sorted_pairs = sorted(
            filtered_pairs, 
            key=lambda x: (float(x["quoteVolume"]) * abs(float(x["priceChangePercent"]))), 
            reverse=True
        )
        return [x["symbol"] for x in sorted_pairs[:15]]  # Зменшено кількість монет
    except Exception as e:
        print(f"Помилка отримання топ монет: {e}")
        return ["BTCUSDT", "ETHUSDT", "BNBUSDT", "ADAUSDT", "SOLUSDT"]

# -------------------------
# Історичні дані з кешуванням та ротацією API
# -------------------------
def get_historical_data(symbol, interval, limit=100):
    cache_key = f"{symbol}_{interval}"
    current_time = time.time()
    
    if cache_key in data_cache:
        data, timestamp = data_cache[cache_key]
        if current_time - timestamp < CACHE_DURATION:
            return data
    
    for api_url in BINANCE_API_URLS:
        try:
            url = f"{api_url}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
            response = requests.get(url, timeout=5)
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
            
            data_cache[cache_key] = (ohlc, current_time)
            return ohlc
            
        except Exception as e:
            print(f"Помилка отримання даних з {api_url} для {symbol}: {e}")
            continue
    
    print(f"Всі API endpoints не відповідають для {symbol}")
    return []

# -------------------------
# Функція очищення застарілого кешу
# -------------------------
def cleanup_cache():
    while True:
        try:
            current_time = time.time()
            keys_to_remove = []
            
            for key, (data, timestamp) in data_cache.items():
                if current_time - timestamp > CACHE_DURATION * 2:
                    keys_to_remove.append(key)
            
            for key in keys_to_remove:
                del data_cache[key]
                
            time.sleep(CACHE_CLEANUP_INTERVAL)
        except Exception as e:
            print(f"Помилка очищення кешу: {e}")

# -------------------------
# ПОКРАЩЕНІ технічні індикатори
# -------------------------
def calculate_ema(prices, period):
    if len(prices) < period:
        return None
    
    alpha = 2 / (period + 1)
    ema = [prices[0]]
    
    for i in range(1, len(prices)):
        ema.append(alpha * prices[i] + (1 - alpha) * ema[i-1])
    
    return ema[-1]

def calculate_rsi(prices, period):
    if len(prices) < period + 1:
        return None
        
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    
    if avg_loss == 0:
        return 100
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    
    return rsi

def calculate_macd(prices, fast_period, slow_period, signal_period):
    if len(prices) < slow_period + signal_period:
        return None, None, None
    
    ema_fast = calculate_ema(prices, fast_period)
    ema_slow = calculate_ema(prices, slow_period)
    
    if ema_fast is None or ema_slow is None:
        return None, None, None
    
    macd_line = ema_fast - ema_slow
    
    # Для сигнальної лінії потрібно більше даних
    macd_prices = prices[-signal_period*2:] if len(prices) >= signal_period*2 else prices
    macd_signal = calculate_ema(macd_prices, signal_period) if len(macd_prices) >= signal_period else None
    
    macd_histogram = macd_line - macd_signal if macd_signal is not None else None
    
    return macd_line, macd_signal, macd_histogram

def calculate_atr(highs, lows, closes, period):
    if len(highs) < period + 1:
        return None
        
    tr = []
    for i in range(1, len(highs)):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i-1])
        lc = abs(lows[i] - closes[i-1])
        tr.append(max(hl, hc, lc))
    
    atr = np.mean(tr[:period])
    return atr

def calculate_bollinger_bands(prices, period=20, num_std=2):
    if len(prices) < period:
        return None, None, None
    
    sma = np.mean(prices[-period:])
    std = np.std(prices[-period:])
    
    upper_band = sma + (std * num_std)
    lower_band = sma - (std * num_std)
    
    return upper_band, sma, lower_band

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
    
    # ATR
    atr = calculate_atr(highs, lows, closes, 14)
    
    # Bollinger Bands
    bb_upper, bb_middle, bb_lower = calculate_bollinger_bands(closes, 20, 2)
    
    # Обсяги
    volume_avg = np.mean(volumes[-10:]) if len(volumes) >= 10 else np.mean(volumes) if len(volumes) > 0 else 0
    volume_current = volumes[-1] if len(volumes) > 0 else 0
    
    # Відносна сила тренду
    trend_strength = 0
    if fast_ema is not None and slow_ema is not None:
        trend_strength = abs((fast_ema - slow_ema) / slow_ema) * 100
    
    return {
        "fast_ema": fast_ema,
        "slow_ema": slow_ema,
        "rsi": rsi,
        "macd": macd,
        "macd_signal": macd_signal,
        "macd_histogram": macd_histogram,
        "atr": atr,
        "bb_upper": bb_upper,
        "bb_middle": bb_middle,
        "bb_lower": bb_lower,
        "volume_avg": volume_avg,
        "volume_current": volume_current,
        "volume_ratio": volume_current / volume_avg if volume_avg > 0 else 1,
        "trend_strength": trend_strength
    }

# -------------------------
# Аналіз результатів минулих сигналів
# -------------------------
def analyze_signal_performance(symbol, current_price):
    symbol_signals = [s for s in signal_history if s["symbol"] == symbol]
    
    if not symbol_signals:
        return 0, 0, 0, 0
    
    successful = 0
    unsuccessful = 0
    total_profit = 0
    
    for signal in symbol_signals:
        if (datetime.now() - signal["time"]).total_seconds() < 4 * 3600:
            continue
            
        price_change = ((current_price - signal["price"]) / signal["price"]) * 100
        
        if signal["signal"] == "BUY":
            if price_change > 2.0:  # Мінімальний прибуток 2%
                successful += 1
                total_profit += price_change
            elif price_change < -2.0:  # Збиток більше 2%
                unsuccessful += 1
        else:
            if price_change < -2.0:
                successful += 1
                total_profit += abs(price_change)
            elif price_change > 2.0:
                unsuccessful += 1
    
    total_signals = successful + unsuccessful
    success_rate = (successful / total_signals * 100) if total_signals > 0 else 0
    avg_profit = (total_profit / total_signals) if total_signals > 0 else 0
    
    return successful, unsuccessful, success_rate, avg_profit

# -------------------------
# ПОКРАЩЕНИЙ аналіз сигналів
# -------------------------
def analyze_phase(ohlc):
    if len(ohlc) < N_CANDLES:
        return "HOLD", 0, 0, {}, False
    
    closes = [c["close"] for c in ohlc]
    highs = [c["high"] for c in ohlc]
    lows = [c["low"] for c in ohlc]
    
    indicators = calculate_indicators(ohlc)
    
    # Базові перевірки
    if any(v is None for v in [indicators["fast_ema"], indicators["slow_ema"], 
                             indicators["rsi"], indicators["macd_histogram"]]):
        return "HOLD", 0, 0, indicators, False
    
    # Тренд
    ema_bullish = indicators["fast_ema"] > indicators["slow_ema"]
    ema_bearish = indicators["fast_ema"] < indicators["slow_ema"]
    
    # RSI
    rsi = indicators["rsi"]
    rsi_overbought = rsi > 65
    rsi_oversold = rsi < 35
    
    # MACD
    macd_bullish = indicators["macd_histogram"] > 0
    macd_bearish = indicators["macd_histogram"] < 0
    
    # Ціна відносно Bollinger Bands
    price_above_bb_middle = closes[-1] > indicators["bb_middle"]
    price_below_bb_middle = closes[-1] < indicators["bb_middle"]
    price_near_bb_lower = closes[-1] <= indicators["bb_lower"] * 1.02
    price_near_bb_upper = closes[-1] >= indicators["bb_upper"] * 0.98
    
    # Обсяги
    volume_spike = indicators["volume_ratio"] > 2.0
    
    # Сила тренду
    strong_trend = indicators["trend_strength"] > 1.0
    
    # СИЛЬНІ критерії для BUY
    buy_criteria = [
        ema_bullish and strong_trend,
        rsi_oversold or (rsi < 45 and not rsi_overbought),
        macd_bullish,
        price_near_bb_lower,
        volume_spike,
        price_above_bb_middle
    ]
    
    # СИЛЬНІ критерії для SELL
    sell_criteria = [
        ema_bearish and strong_trend,
        rsi_overbought or (rsi > 55 and not rsi_oversold),
        macd_bearish,
        price_near_bb_upper,
        volume_spike,
        price_below_bb_middle
    ]
    
    buy_signals = sum(buy_criteria)
    sell_signals = sum(sell_criteria)
    
    # Волатильність на основі ATR
    volatility = indicators["atr"] or (max(highs[-20:]) - min(lows[-20:])) / 2
    
    # ВПЕВНЕНІСТЬ на основі якості сигналів
    confidence = max(buy_signals, sell_signals) / len(buy_criteria)
    
    # ДОДАТКОВІ ФІЛЬТРИ
    current_price = closes[-1]
    prev_price = closes[-2] if len(closes) >= 2 else current_price
    price_change = ((current_price - prev_price) / prev_price) * 100
    
    # Фільтр проти фальшивих пробоїв
    if abs(price_change) > 8.0:  # Дуже різкі зміни - ймовірно шум
        return "HOLD", volatility, confidence * 0.7, indicators, False
    
    # Мінімальна впевненість
    if confidence < MIN_CONFIDENCE_FOR_SIGNAL:
        return "HOLD", volatility, confidence, indicators, False
    
    if buy_signals > sell_signals and buy_signals >= 4:  # Мінімум 4 з 6 критеріїв
        return "BUY", volatility, confidence, indicators, True
    elif sell_signals > buy_signals and sell_signals >= 4:
        return "SELL", volatility, confidence, indicators, True
    else:
        return "HOLD", volatility, confidence, indicators, False

# -------------------------
# Відправка сигналу з ПОКРАЩЕНИМИ TP/SL
# -------------------------
def send_signal(symbol, signal, price, volatility, confidence, indicators, timeframe_confirmation):
    global last_signals, signal_history
    
    if signal == "HOLD" or confidence < MIN_CONFIDENCE_FOR_SIGNAL:
        return
        
    current_time = datetime.now()
    if symbol in last_signals:
        last_signal_time = last_signals[symbol]["time"]
        if (current_time - last_signal_time).total_seconds() < 7200:  # 2 години між сигналами
            return
    
    # ПОКРАЩЕНІ розрахунки TP/SL
    if signal == "BUY":
        # TP: 2.5-3.5x ATR, SL: 1.5-2x ATR
        tp_distance = volatility * (2.5 + confidence * 1.0)
        sl_distance = volatility * (1.5 + (1 - confidence) * 0.5)
        tp = round(price + tp_distance, 4)
        sl = round(price - sl_distance, 4)
    else:
        tp_distance = volatility * (2.5 + confidence * 1.0)
        sl_distance = volatility * (1.5 + (1 - confidence) * 0.5)
        tp = round(price - tp_distance, 4)
        sl = round(price + sl_distance, 4)
    
    # Risk/Reward ratio перевірка
    risk_reward = abs(tp - price) / abs(sl - price)
    if risk_reward < 1.5:  # Мінімальне співвідношення 1.5:1
        # Коригуємо TP для кращого R/R
        if signal == "BUY":
            tp = round(price + abs(sl - price) * 1.8, 4)
        else:
            tp = round(price - abs(sl - price) * 1.8, 4)
    
    tp_percent = round(((tp - price) / price) * 100, 2)
    sl_percent = round(((sl - price) / price) * 100, 2)
    
    # Аналізуємо результати минулих сигналів
    successful, unsuccessful, success_rate, avg_profit = analyze_signal_performance(symbol, price)
    
    signal_data = {
        "symbol": symbol,
        "signal": signal,
        "price": price,
        "tp": tp,
        "sl": sl,
        "confidence": confidence,
        "time": current_time,
        "timeframe_confirmation": timeframe_confirmation,
        "indicators": indicators,
        "risk_reward": risk_reward
    }
    
    last_signals[symbol] = signal_data
    
    if confidence >= MIN_CONFIDENCE_FOR_HISTORY:
        signal_history.append(signal_data)
        if len(signal_history) > 1000:
            signal_history = signal_history[-1000:]
        save_signals_history()
    
    # Формування повідомлення
    emoji = "🚀🔥" if signal == "BUY" else "🔻📉"
    
    confidence_level = "✅ ВИСОКА впевненість" if confidence > 0.8 else "⚠️ Помірна впевненість"
    
    history_info = ""
    if successful + unsuccessful > 0:
        history_info = f"📊 Історія: ✅{successful} | ❌{unsuccessful} | Успішність: {success_rate:.1f}%"
    
    # Додаємо інформацію про індикатори
    indicators_info = f"📈 RSI: {indicators.get('rsi', 'N/A'):.1f} | MACD: {'↑' if indicators.get('macd_histogram', 0) > 0 else '↓'}"
    
    msg = (
        f"{emoji} *{symbol}* | {signal}\n"
        f"💰 Ціна: `{price}`\n"
        f"🎯 TP: `{tp}` (+{tp_percent}%)\n"
        f"🛑 SL: `{sl}` ({sl_percent}%)\n"
        f"📊 R/R: {risk_reward:.2f}:1\n"
        f"📈 Впевненість: {confidence*100:.1f}% - {confidence_level}\n"
        f"{indicators_info}\n"
        f"{history_info}\n"
        f"⏰ {current_time.strftime('%H:%M:%S')}"
    )
    
    try:
        bot.send_message(CHAT_ID, msg, parse_mode="Markdown")
        
        with open("signals.log", "a", encoding="utf-8") as f:
            log_msg = (
                f"{current_time} | {symbol} | {signal} | {price} | "
                f"TP: {tp} | SL: {sl} | Confidence: {confidence:.2f} | "
                f"RSI: {indicators.get('rsi', 'N/A'):.1f} | R/R: {risk_reward:.2f}:1\n"
            )
            f.write(log_msg)
            
        update_performance_stats(symbol, signal, price)
            
    except Exception as e:
        print(f"Помилка відправки повідомлення: {e}")

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
        
    if stats["last_signal"] and stats["last_price"]:
        price_change = (price - stats["last_price"]) / stats["last_price"] * 100
        
        if (stats["last_signal"] == "BUY" and price_change > 2.0) or \
           (stats["last_signal"] == "SELL" and price_change < -2.0):
            stats["successful_signals"] += 1
            
        stats["profitability"] = stats["successful_signals"] / stats["total_signals"] * 100 if stats["total_signals"] > 0 else 0
        
    stats["last_signal"] = signal
    stats["last_price"] = price
    
    with open("performance_stats.json", "w") as f:
        json.dump(performance_stats, f)

# -------------------------
# Перевірка ринку з паралельною обробкою
# -------------------------
def check_symbol(symbol, results):
    try:
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
        
        if signals:
            results.append({
                'symbol': symbol,
                'signals': signals,
                'volatilities': volatilities,
                'confidences': confidences,
                'indicators': all_indicators,
                'last_prices': last_prices
            })
    except Exception as e:
        print(f"Помилка перевірки {symbol}: {e}")

def check_market():
    global last_status
    check_interval = 45  # секунд між перевірками
    
    while True:
        try:
            symbols = get_top_symbols()
            print(f"{datetime.now()} - Перевірка {len(symbols)} монет...")
            
            threads = []
            results = []
            
            for symbol in symbols:
                thread = threading.Thread(target=check_symbol, args=(symbol, results))
                threads.append(thread)
                thread.start()
                time.sleep(0.1)  # Невелика затримка між потоками
            
            for thread in threads:
                thread.join()
            
            for result in results:
                symbol = result['symbol']
                signals = result['signals']
                volatilities = result['volatilities']
                confidences = result['confidences']
                all_indicators = result['indicators']
                last_prices = result['last_prices']
                
                buy_count = signals.count("BUY")
                sell_count = signals.count("SELL")
                total_tfs = len(signals)
                
                if total_tfs == 0:
                    continue
                
                avg_confidence = sum(confidences) / total_tfs
                
                # СУВОРИЙ фільтр - потрібна підтримка всіх таймфреймів
                if buy_count == total_tfs and avg_confidence >= MIN_CONFIDENCE_FOR_SIGNAL:
                    price = last_prices[-1]
                    max_volatility = max(volatilities)
                    send_signal(symbol, "BUY", price, max_volatility, avg_confidence, all_indicators[-1], buy_count)
                elif sell_count == total_tfs and avg_confidence >= MIN_CONFIDENCE_FOR_SIGNAL:
                    price = last_prices[-1]
                    max_volatility = max(volatilities)
                    send_signal(symbol, "SELL", price, max_volatility, avg_confidence, all_indicators[-1], sell_count)
                
                last_status[symbol] = {
                    "signals": signals,
                    "confidences": confidences,
                    "timeframes": TIMEFRAMES[:len(signals)],
                    "last_prices": last_prices,
                    "volatilities": volatilities,
                    "timestamp": datetime.now()
                }

        except Exception as e:
            print(f"{datetime.now()} - Критична помилка: {e}")
        
        time.sleep(check_interval)

# -------------------------
# Додаткові сервісні функції
# -------------------------
def health_check():
    while True:
        try:
            response = requests.get("https://api.binance.com/api/v3/ping", timeout=5)
            if response.status_code != 200:
                print("⚠️ Проблема з з'єднанням Binance")
            bot.get_me()
        except Exception as e:
            print(f"⚠️ Health check failed: {e}")
        time.sleep(60)

def backup_data():
    while True:
        try:
            save_signals_history()
            with open("performance_stats.json", "w") as f:
                json.dump(performance_stats, f)
        except Exception as e:
            print(f"❌ Помилка резервного копіювання: {e}")
        time.sleep(300)

# -------------------------
# Вебхук Telegram
# -------------------------
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    json_str = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
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

def load_performance_stats():
    global performance_stats
    try:
        with open("performance_stats.json", "r") as f:
            performance_stats = json.load(f)
    except:
        performance_stats = {}

# -------------------------
# Запуск
# -------------------------
if __name__ == "__main__":
    load_performance_stats()
    load_signals_history()
    setup_webhook()
    
    threading.Thread(target=check_market, daemon=True).start()
    threading.Thread(target=cleanup_cache, daemon=True).start()
    threading.Thread(target=health_check, daemon=True).start()
    threading.Thread(target=backup_data, daemon=True).start()
    
    app.run(host="0.0.0.0", port=5000)