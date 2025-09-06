import ccxt
import requests
import time
import os
import numpy as np
from datetime import datetime, timedelta
from flask import Flask, request
import telebot
import threading
import json
import pandas as pd
from collections import deque
import talib
from scipy import stats
import warnings
warnings.filterwarnings('ignore')

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

TRADE_AMOUNT_USD = float(os.getenv("TRADE_AMOUNT_USD", 100))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 30))  # 30 секунд

bot = telebot.TeleBot(API_KEY_TELEGRAM)
app = Flask(__name__)

# Ініціалізація біржі
try:
    gate = ccxt.gateio({
        "apiKey": GATE_API_KEY,
        "secret": GATE_API_SECRET,
        "options": {"defaultType": "swap"},
        "enableRateLimit": True
    })
    gate.load_markets()
    print(f"{datetime.now()} | ✅ Успішно підключено до Gate.io Futures")
except Exception as e:
    print(f"{datetime.now()} | ❌ Помилка підключення до Gate.io: {e}")
    gate = None

# Глобальні змінні для унікальних алгоритмів
quantum_signals = {}
temporal_anomalies = {}
liquidity_vortex = {}
dark_pool_detector = {}
market_memory = {}

# -------------------------
# КВАНТОВО-МЕМНІЧНИЙ АНАЛІЗ (НОВА ТЕХНОЛОГІЯ)
# -------------------------

def quantum_memory_analysis(symbol, timeframe='5m', memory_depth=50):
    """
    Аналізує "пам'ять" ринку через квантово-механічні аналоги
    Знаходить паттерни, які не видно звичайним методам
    """
    try:
        # Отримуємо історичні дані
        ohlcv = gate.fetch_ohlcv(symbol, timeframe, limit=memory_depth)
        if len(ohlcv) < memory_depth:
            return None
            
        closes = np.array([x[4] for x in ohlcv])
        volumes = np.array([x[5] for x in ohlcv])
        
        # Квантово-подібне перетворення даних
        wave_function = np.fft.fft(closes)
        probability_density = np.abs(wave_function) ** 2
        
        # Аналіз "квантової когерентності"
        coherence = np.std(probability_density) / np.mean(probability_density)
        
        # Механічна пам'ять ринку
        memory_decay = self_correlation_analysis(closes)
        
        # Ентропія інформації
        entropy = calculate_market_entropy(closes, volumes)
        
        signal = {
            'symbol': symbol,
            'coherence': coherence,
            'memory_decay': memory_decay,
            'entropy': entropy,
            'quantum_score': coherence * memory_decay * (1 - entropy),
            'timestamp': datetime.now()
        }
        
        return signal
        
    except Exception as e:
        print(f"Quantum analysis error for {symbol}: {e}")
        return None

def self_correlation_analysis(data):
    """Аналіз самокореляції для виявлення пам'яті ринку"""
    lags = range(1, min(20, len(data)//2))
    correlations = []
    
    for lag in lags:
        if lag < len(data):
            corr = np.corrcoef(data[:-lag], data[lag:])[0, 1]
            if not np.isnan(corr):
                correlations.append(abs(corr))
    
    return np.mean(correlations) if correlations else 0

def calculate_market_entropy(prices, volumes):
    """Розрахунок ентропії ринку"""
    price_changes = np.diff(prices) / prices[:-1]
    volume_changes = np.diff(volumes) / volumes[:-1]
    
    # Комбінована ентропія
    combined = price_changes * volume_changes
    entropy = stats.entropy(np.abs(combined))
    
    return entropy / 10  # Нормалізація

# -------------------------
# ТЕМПОРАЛЬНІ АНОМАЛІЇ (МАШИНА ЧАСУ)
# -------------------------

def detect_temporal_anomalies(symbol):
    """
    Виявляє аномалії в часових рядах, які передують великим рухам
    """
    try:
        # Отримуємо дані різних таймфреймів
        timeframes = ['1m', '5m', '15m', '1h']
        anomalies = []
        
        for tf in timeframes:
            ohlcv = gate.fetch_ohlcv(symbol, tf, limit=100)
            if len(ohlcv) < 50:
                continue
                
            # Перетворюємо в numpy
            highs = np.array([x[2] for x in ohlcv])
            lows = np.array([x[3] for x in ohlcv])
            closes = np.array([x[4] for x in ohlcv])
            
            # Детектуємо аномалії через Z-score
            z_scores = np.abs(stats.zscore(closes[-20:]))
            temporal_anomaly = np.any(z_scores > 2.5)
            
            if temporal_anomaly:
                anomaly_strength = np.max(z_scores)
                anomalies.append({
                    'timeframe': tf,
                    'strength': anomaly_strength,
                    'position': np.argmax(z_scores)
                })
        
        if anomalies:
            return {
                'symbol': symbol,
                'anomalies': anomalies,
                'composite_score': sum(a['strength'] for a in anomalies) / len(anomalies),
                'signal': 'BULLISH' if closes[-1] > closes[-2] else 'BEARISH'
            }
            
    except Exception as e:
        print(f"Temporal anomaly detection error: {e}")
    
    return None

# -------------------------
# ВИЯВЛЕННЯ ТЕМНИХ ПУЛІВ (DARK POOL DETECTION)
# -------------------------

def detect_dark_pool_activity(symbol):
    """
    Виявляє активність темних пулів через аномалії в об'ємах
    """
    try:
        orderbook = gate.fetch_order_book(symbol, limit=1000)
        trades = gate.fetch_trades(symbol, limit=500)
        
        if not orderbook or not trades:
            return None
        
        # Аналіз дисбалансу об'ємів
        bids_volume = sum(bid[1] for bid in orderbook['bids'][:20])
        asks_volume = sum(ask[1] for ask in orderbook['asks'][:20])
        volume_imbalance = (bids_volume - asks_volume) / (bids_volume + asks_volume)
        
        # Аналіз великих торгів
        large_trades = [t for t in trades if t['amount'] * t['price'] > 50000]  > 50k USDT
        large_buys = sum(1 for t in large_trades if t['side'] == 'buy')
        large_sells = sum(1 for t in large_trades if t['side'] == 'sell')
        
        # Детекція "стелен" купівлі/продажу
        bid_walls = detect_liquidity_walls(orderbook['bids'])
        ask_walls = detect_liquidity_walls(orderbook['asks'])
        
        dark_pool_score = (abs(volume_imbalance) + 
                          (large_buys - large_sells) / len(large_trades) if large_trades else 0 +
                          len(bid_walls) - len(ask_walls))
        
        return {
            'symbol': symbol,
            'volume_imbalance': volume_imbalance,
            'large_trades_ratio': len(large_trades) / len(trades) if trades else 0,
            'bid_walls': bid_walls,
            'ask_walls': ask_walls,
            'dark_pool_score': dark_pool_score,
            'signal': 'BULLISH' if volume_imbalance > 0.1 else 'BEARISH' if volume_imbalance < -0.1 else 'NEUTRAL'
        }
        
    except Exception as e:
        print(f"Dark pool detection error: {e}")
        return None

def detect_liquidity_walls(orders, threshold=100000):
    """Виявляє великі стіни ліквідності"""
    walls = []
    for price, amount in orders:
        if amount * price > threshold:  # Стіна більше 100k USDT
            walls.append({'price': price, 'amount': amount, 'value': amount * price})
    return walls

# -------------------------
# ВОРТЕКС ЛІКВІДНОСТІ (LIQUIDITY VORTEX)
# -------------------------

def analyze_liquidity_vortex(symbol):
    """
    Аналізує "вихори" ліквідності, які утворюються перед великими рухами
    """
    try:
        # Отримуємо глибину ринку
        orderbook = gate.fetch_order_book(symbol, limit=1000)
        if not orderbook:
            return None
        
        # Аналіз динаміки ліквідності
        bid_volatility = liquidity_volatility(orderbook['bids'])
        ask_volatility = liquidity_volatility(orderbook['asks'])
        
        # Вихорний ефект
        vortex_strength = abs(bid_volatility - ask_volatility)
        
        # Прогнозування напрямку
        if bid_volatility > ask_volatility * 1.5:
            direction = 'BULLISH_VORTEX'
        elif ask_volatility > bid_volatility * 1.5:
            direction = 'BEARISH_VORTEX'
        else:
            direction = 'CALM'
        
        return {
            'symbol': symbol,
            'vortex_strength': vortex_strength,
            'direction': direction,
            'bid_volatility': bid_volatility,
            'ask_volatility': ask_volatility,
            'forecast_confidence': min(95, vortex_strength * 100)
        }
        
    except Exception as e:
        print(f"Liquidity vortex analysis error: {e}")
        return None

def liquidity_volatility(orders, lookback=10):
    """Аналізує волатильність ліквідності"""
    if len(orders) < lookback:
        return 0
    
    volumes = [amount for _, amount in orders[:lookback]]
    returns = np.diff(volumes) / volumes[:-1]
    return np.std(returns) if len(returns) > 0 else 0

# -------------------------
# НЕЙРОННА МЕРЕЖА В РЕАЛЬНОМУ ЧАСІ (СИМУЛЯЦІЯ)
# -------------------------

def neural_market_sentiment(symbol):
    """
    Симвулює роботу нейронної мережі для аналізу ринкових настроїв
    """
    try:
        # Отримуємо різні види даних
        ohlcv = gate.fetch_ohlcv(symbol, '5m', limit=100)
        orderbook = gate.fetch_order_book(symbol, limit=200)
        trades = gate.fetch_trades(symbol, limit=200)
        
        if len(ohlcv) < 50 or not orderbook or not trades:
            return None
        
        # Складові нейронного аналізу
        technical_score = analyze_technical_patterns(ohlcv)
        orderbook_score = analyze_orderbook_dynamics(orderbook)
        trade_flow_score = analyze_trade_flow(trades)
        
        # Комбінований сигнал нейронної мережі
        neural_signal = (technical_score * 0.4 + 
                        orderbook_score * 0.3 + 
                        trade_flow_score * 0.3)
        
        return {
            'symbol': symbol,
            'neural_score': neural_signal,
            'technical': technical_score,
            'orderbook': orderbook_score,
            'trade_flow': trade_flow_score,
            'signal': 'BULLISH' if neural_signal > 0.7 else 'BEARISH' if neural_signal < 0.3 else 'NEUTRAL',
            'confidence': abs(neural_signal - 0.5) * 200
        }
        
    except Exception as e:
        print(f"Neural sentiment analysis error: {e}")
        return None

def analyze_technical_patterns(ohlcv):
    """Аналіз технічних паттернів"""
    closes = np.array([x[4] for x in ohlcv])
    rsi = talib.RSI(closes, timeperiod=14)[-1]
    macd, signal, _ = talib.MACD(closes)
    
    # Нормалізація в [0, 1]
    rsi_score = 1 - abs(rsi - 50) / 50 if not np.isnan(rsi) else 0.5
    macd_score = 0.5
    if len(macd) > 1 and not np.isnan(macd[-1]) and not np.isnan(signal[-1]):
        macd_score = 0.5 + (macd[-1] - signal[-1]) / (2 * np.std(macd[-20:]) if np.std(macd[-20:]) > 0 else 1)
    
    return (rsi_score + macd_score) / 2

def analyze_orderbook_dynamics(orderbook):
    """Аналіз динаміки стакану"""
    bids = orderbook['bids'][:20]
    asks = orderbook['asks'][:20]
    
    bid_volume = sum(amount for _, amount in bids)
    ask_volume = sum(amount for _, amount in asks)
    
    imbalance = (bid_volume - ask_volume) / (bid_volume + ask_volume)
    return 0.5 + imbalance / 2

def analyze_trade_flow(trades):
    """Аналіз потоку торгів"""
    if not trades:
        return 0.5
        
    buy_volume = sum(t['amount'] for t in trades if t['side'] == 'buy')
    sell_volume = sum(t['amount'] for t in trades if t['side'] == 'sell')
    
    total_volume = buy_volume + sell_volume
    if total_volume == 0:
        return 0.5
        
    flow = (buy_volume - sell_volume) / total_volume
    return 0.5 + flow / 2

# -------------------------
# КВАНТОВИЙ ТРЕЙДИНГ (ОСНОВНА ФУНКЦІЯ)
# -------------------------

def quantum_trading_engine():
    """
    Основний двигун квантового трейдингу - комбінує всі методи
    """
    symbols = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 
               'XRP/USDT:USDT', 'ADA/USDT:USDT', 'DOT/USDT:USDT']
    
    all_signals = []
    
    for symbol in symbols:
        try:
            # Запускаємо всі аналізи паралельно
            quantum_signal = quantum_memory_analysis(symbol)
            temporal_signal = detect_temporal_anomalies(symbol)
            dark_pool_signal = detect_dark_pool_activity(symbol)
            vortex_signal = analyze_liquidity_vortex(symbol)
            neural_signal = neural_market_sentiment(symbol)
            
            # Комбінуємо сигнали з вагами
            signals = [s for s in [quantum_signal, temporal_signal, dark_pool_signal, 
                                  vortex_signal, neural_signal] if s is not None]
            
            if signals:
                composite_score = calculate_composite_score(signals)
                
                if abs(composite_score) > 0.8:  # Сильний сигнал
                    signal_data = {
                        'symbol': symbol,
                        'composite_score': composite_score,
                        'signals': signals,
                        'timestamp': datetime.now(),
                        'action': 'BUY' if composite_score > 0 else 'SELL',
                        'confidence': abs(composite_score) * 100
                    }
                    all_signals.append(signal_data)
                    
        except Exception as e:
            print(f"Quantum trading error for {symbol}: {e}")
    
    return sorted(all_signals, key=lambda x: abs(x['composite_score']), reverse=True)

def calculate_composite_score(signals):
    """Розрахунок комбінованого скору з різних методів"""
    total_score = 0
    weights = {
        'quantum': 0.25,
        'temporal': 0.20,
        'dark_pool': 0.20,
        'vortex': 0.15,
        'neural': 0.20
    }
    
    for signal in signals:
        if 'quantum_score' in signal:
            total_score += signal['quantum_score'] * weights['quantum']
        elif 'composite_score' in signal:
            total_score += (signal['composite_score'] - 0.5) * 2 * weights['temporal']
        elif 'dark_pool_score' in signal:
            total_score += np.tanh(signal['dark_pool_score']) * weights['dark_pool']
        elif 'vortex_strength' in signal:
            direction = 1 if signal['direction'] == 'BULLISH_VORTEX' else -1
            total_score += direction * signal['vortex_strength'] * weights['vortex']
        elif 'neural_score' in signal:
            total_score += (signal['neural_score'] - 0.5) * 2 * weights['neural']
    
    return np.tanh(total_score)  # Нормалізація до [-1, 1]

# -------------------------
# ВИКОНАННЯ ТОРГІВЛІ
# -------------------------

def execute_quantum_trade(signal):
    """Виконання торгівлі на основі квантових сигналів"""
    if not gate:
        return False
    
    try:
        symbol = signal['symbol']
        action = signal['action']
        confidence = signal['confidence']
        
        if confidence < 85:  # Мінімум 85% впевненості
            return False
        
        # Розмір позиції на основі впевненості
        size_multiplier = min(1.0, confidence / 100)
        amount_usd = TRADE_AMOUNT_USD * size_multiplier
        
        ticker = gate.fetch_ticker(symbol)
        price = ticker['last']
        amount = amount_usd / price
        
        if action == 'BUY':
            order = gate.create_market_buy_order(symbol, amount)
            print(f"{datetime.now()} | ✅ QUANTUM BUY: {amount:.6f} {symbol}")
        else:
            order = gate.create_market_sell_order(symbol, amount)
            print(f"{datetime.now()} | ✅ QUANTUM SELL: {amount:.6f} {symbol}")
        
        # Детальне повідомлення
        msg = f"⚛️ КВАНТОВИЙ СИГНАЛ! {symbol}\n"
        msg += f"Дія: {action}\n"
        msg += f"Впевненість: {confidence:.1f}%\n"
        msg += f"Розмір: {amount_usd:.2f} USDT\n"
        msg += f"Ціна: {price:.6f}\n"
        msg += f"Час: {datetime.now().strftime('%H:%M:%S')}\n"
        msg += f"---\nКомпоненти:\n"
        
        for s in signal['signals']:
            if 'quantum_score' in s:
                msg += f"• Квант: {s['quantum_score']:.3f}\n"
            elif 'composite_score' in s:
                msg += f"• Аномалія: {s['composite_score']:.3f}\n"
            elif 'dark_pool_score' in s:
                msg += f"• Dark Pool: {s['dark_pool_score']:.3f}\n"
            elif 'vortex_strength' in s:
                msg += f"• Вихор: {s['vortex_strength']:.3f}\n"
            elif 'neural_score' in s:
                msg += f"• Нейромережа: {s['neural_score']:.3f}\n"
        
        bot.send_message(CHAT_ID, msg)
        return True
        
    except Exception as e:
        error_msg = f"❌ Помилка квантової торгівлі: {e}"
        print(f"{datetime.now()} | {error_msg}")
        bot.send_message(CHAT_ID, error_msg)
        return False

# -------------------------
# ОСНОВНИЙ ЦИКЛ КВАНТОВОГО ТРЕЙДИНГУ
# -------------------------

def start_quantum_trading():
    """Основний цикл квантового трейдингу"""
    bot.send_message(CHAT_ID, "⚛️ Запуск КВАНТОВОГО ТРЕЙДИНГУ...")
    bot.send_message(CHAT_ID, "🔭 Сканування часових аномалій...")
    bot.send_message(CHAT_ID, "🌌 Моніторинг темних пулів...")
    bot.send_message(CHAT_ID, "🌀 Аналіз вихорів ліквідності...")
    
    while True:
        try:
            print(f"{datetime.now()} | ⚛️ Запуск квантового аналізу...")
            
            # Знаходимо квантові сигнали
            signals = quantum_trading_engine()
            
            if signals:
                best_signal = signals[0]
                print(f"{datetime.now()} | 🎯 Найкращий сигнал: {best_signal['symbol']} - {best_signal['confidence']:.1f}%")
                
                # Виконуємо торгівлю
                if best_signal['confidence'] > 90:
                    execute_quantum_trade(best_signal)
                    time.sleep(10)  # Пауза між угодами
            
            time.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            print(f"{datetime.now()} | ❌ Критична помилка: {e}")
            time.sleep(60)

# -------------------------
# УНІКАЛЬНІ TELEGRAM КОМАНДИ
# -------------------------

@bot.message_handler(commands=['quantum_scan'])
def quantum_scan(message):
    """Миттєвий квантовий сканер"""
    bot.reply_to(message, "🔭 Запуск глибокого квантового сканування...")
    
    signals = quantum_trading_engine()
    if not signals:
        bot.reply_to(message, "⚡ Квантові сигнали не знайдені")
        return
    
    msg = "⚛️ РЕЗУЛЬТАТИ КВАНТОВОГО СКАНУ:\n\n"
    for i, signal in enumerate(signals[:5]):  # Топ-5 сигналів
        msg += f"{i+1}. {signal['symbol']} - {signal['action']}\n"
        msg += f"   Впевненість: {signal['confidence']:.1f}%\n"
        msg += f"   Скор: {signal['composite_score']:.3f}\n\n"
    
    bot.reply_to(message, msg)

@bot.message_handler(commands=['dark_pool_check'])
def dark_pool_check(message):
    """Перевірка активності темних пулів"""
    symbols = ['BTC/USDT:USDT', 'ETH/USDT:USDT']
    msg = "🌌 АКТИВНІСТЬ ТЕМНИХ ПУЛІВ:\n\n"
    
    for symbol in symbols:
        signal = detect_dark_pool_activity(symbol)
        if signal:
            msg += f"{symbol}:\n"
            msg += f"• Імбаланс: {signal['volume_imbalance']:.3f}\n"
            msg += f"• Великі торгі: {signal['large_trades_ratio']:.3f}\n"
            msg += f"• Стен: {len(signal['bid_walls'])}B/{len(signal['ask_walls'])}S\n"
            msg += f"• Сигнал: {signal['signal']}\n\n"
    
    bot.reply_to(message, msg)

@bot.message_handler(commands=['vortex_analysis'])
def vortex_analysis(message):
    """Аналіз вихорів ліквідності"""
    symbols = ['BTC/USDT:USDT', 'ETH/USDT:USDT']
    msg = "🌀 АНАЛІЗ ВИХОРІВ ЛІКВІДНОСТІ:\n\n"
    
    for symbol in symbols:
        signal = analyze_liquidity_vortex(symbol)
        if signal:
            msg += f"{symbol}:\n"
            msg += f"• Сила вихору: {signal['vortex_strength']:.3f}\n"
            msg += f"• Напрямок: {signal['direction']}\n"
            msg += f"• Впевненість: {signal['forecast_confidence']:.1f}%\n\n"
    
    bot.reply_to(message, msg)

@bot.message_handler(commands=['temporal_anomalies'])
def temporal_anomalies_cmd(message):
    """Пошук часових аномалій"""
    symbols = ['BTC/USDT:USDT', 'ETH/USDT:USDT']
    msg = "⏰ ЧАСОВІ АНОМАЛІЇ:\n\n"
    
    for symbol in symbols:
        signal = detect_temporal_anomalies(symbol)
        if signal:
            msg += f"{symbol}:\n"
            msg += f"• Скор: {signal['composite_score']:.3f}\n"
            msg += f"• Сигнал: {signal['signal']}\n"
            msg += f"• Аномалій: {len(signal['anomalies'])}\n\n"
    
    bot.reply_to(message, msg)

@bot.message_handler(commands=['neural_sentiment'])
def neural_sentiment_cmd(message):
    """Нейромережевий аналіз настроїв"""
    symbols = ['BTC/USDT:USDT', 'ETH/USDT:USDT']
    msg = "🧠 НЕЙРОМЕРЕЖЕВІ НАСТРОЇ:\n\n"
    
    for symbol in symbols:
        signal = neural_market_sentiment(symbol)
        if signal:
            msg += f"{symbol}:\n"
            msg += f"• Скор: {signal['neural_score']:.3f}\n"
            msg += f"• Сигнал: {signal['signal']}\n"
            msg += f"• Впевненість: {signal['confidence']:.1f}%\n\n"
    
    bot.reply_to(message, msg)

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
    print(f"{datetime.now()} | ⚛️ Запуск КВАНТОВОГО ТРЕЙДИНГ-БОТА...")
    
    # Перевірка обов'язкових ключів
    required_keys = [API_KEY_TELEGRAM, CHAT_ID, GATE_API_KEY, GATE_API_SECRET]
    if not all(required_keys):
        print(f"{datetime.now()} | ❌ Відсутні обов'язкові API ключі!")
        exit(1)
    
    setup_webhook()
    threading.Thread(target=start_quantum_trading, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)