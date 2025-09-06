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
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 30))
DEMO_MODE = os.getenv("DEMO_MODE", "True").lower() == "true"

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

# Механізми безпеки
SAFETY_MECHANISMS = {
    'max_position_size': 0.1,
    'daily_loss_limit': -0.05,
    'min_confidence': 0.85,
    'cooldown_period': 60,
    'symbol_blacklist': ['SHIB/USDT:USDT', 'PEPE/USDT:USDT', 'DOGE/USDT:USDT']
}

# -------------------------
# ВЛАСНІ ФУНКЦІЇ ТЕХНІЧНОГО АНАЛІЗУ (замість TA-Lib)
# -------------------------

def calculate_rsi(prices, period=14):
    """Власна реалізація RSI без TA-Lib"""
    if len(prices) < period + 1:
        return 50
    
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    
    if avg_loss == 0:
        return 100 if avg_gain > 0 else 50
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    
    return rsi

def calculate_macd(prices, fast_period=12, slow_period=26, signal_period=9):
    """Власна реалізація MACD без TA-Lib"""
    if len(prices) < slow_period + signal_period:
        return 0, 0, 0
    
    # EMA для швидкого періоду
    ema_fast = calculate_ema(prices, fast_period)
    
    # EMA для повільного періоду
    ema_slow = calculate_ema(prices, slow_period)
    
    # MACD лінія
    macd_line = ema_fast - ema_slow
    
    # Signal лінія (EMA від MACD)
    signal_line = calculate_ema(macd_line[-signal_period:], signal_period)
    
    # Histogram
    histogram = macd_line[-1] - signal_line[-1] if len(signal_line) > 0 else 0
    
    return macd_line[-1], signal_line[-1] if len(signal_line) > 0 else 0, histogram

def calculate_ema(prices, period):
    """Розрахунок Exponential Moving Average"""
    if len(prices) < period:
        return np.array([np.mean(prices)] * len(prices))
    
    ema = np.zeros(len(prices))
    ema[period-1] = np.mean(prices[:period])
    
    multiplier = 2 / (period + 1)
    
    for i in range(period, len(prices)):
        ema[i] = (prices[i] - ema[i-1]) * multiplier + ema[i-1]
    
    return ema

def calculate_bollinger_bands(prices, period=20, num_std=2):
    """Розрахунок смуг Боллінджера"""
    if len(prices) < period:
        return np.nan, np.nan, np.nan
    
    middle_band = np.mean(prices[-period:])
    std_dev = np.std(prices[-period:])
    
    upper_band = middle_band + (std_dev * num_std)
    lower_band = middle_band - (std_dev * num_std)
    
    return upper_band, middle_band, lower_band

# -------------------------
# ФУНКЦІЇ БЕЗПЕКИ
# -------------------------

def safety_check(symbol, amount_usd, confidence):
    """Перевірка безпеки перед угодою"""
    if confidence < SAFETY_MECHANISMS['min_confidence']:
        return False
        
    if symbol in SAFETY_MECHANISMS['symbol_blacklist']:
        return False
        
    if DEMO_MODE:
        return True
        
    try:
        balance = gate.fetch_balance()
        total_usdt = balance['total'].get('USDT', 0)
        
        if amount_usd > total_usdt * SAFETY_MECHANISMS['max_position_size']:
            print(f"❌ Перевищено максимальний розмір позиції")
            return False
    except:
        return False
        
    return True

# -------------------------
# КВАНТОВО-МЕМНІЧНИЙ АНАЛІЗ
# -------------------------

def quantum_memory_analysis(symbol, timeframe='5m', memory_depth=50):
    """Аналізує пам'ять ринку через спектральний аналіз"""
    try:
        ohlcv = gate.fetch_ohlcv(symbol, timeframe, limit=memory_depth)
        if len(ohlcv) < memory_depth:
            return None
            
        closes = np.array([x[4] for x in ohlcv])
        volumes = np.array([x[5] for x in ohlcv])
        
        # Спектральний аналіз
        wave_function = np.fft.fft(closes)
        probability_density = np.abs(wave_function) ** 2
        
        # Аналіз когерентності
        coherence = np.std(probability_density) / np.mean(probability_density) if np.mean(probability_density) > 0 else 0
        
        # Самокореляція
        memory_decay = self_correlation_analysis(closes)
        
        # Ентропія інформації
        entropy = calculate_market_entropy(closes, volumes)
        
        signal = {
            'symbol': symbol,
            'coherence': coherence,
            'memory_decay': memory_decay,
            'entropy': entropy,
            'quantum_score': coherence * memory_decay * (1 - entropy) if entropy < 1 else 0,
            'timestamp': datetime.now()
        }
        
        return signal
        
    except Exception as e:
        print(f"Quantum analysis error for {symbol}: {e}")
        return None

def self_correlation_analysis(data):
    """Аналіз самокореляції"""
    if len(data) < 10:
        return 0
        
    lags = range(1, min(10, len(data)//2))
    correlations = []
    
    for lag in lags:
        if lag < len(data):
            corr = np.corrcoef(data[:-lag], data[lag:])[0, 1]
            if not np.isnan(corr):
                correlations.append(abs(corr))
    
    return np.mean(correlations) if correlations else 0

def calculate_market_entropy(prices, volumes):
    """Розрахунок ентропії ринку"""
    if len(prices) < 2 or len(volumes) < 2:
        return 0
        
    price_changes = np.diff(prices) / prices[:-1]
    volume_changes = np.diff(volumes) / volumes[:-1]
    
    combined = price_changes * volume_changes
    if len(combined) > 0:
        # Проста ентропія на основі дисперсії
        entropy = np.var(np.abs(combined))
        return min(1.0, entropy * 10)  # Нормалізація
    return 0

# -------------------------
# ТЕМПОРАЛЬНІ АНОМАЛІЇ
# -------------------------

def detect_temporal_anomalies(symbol):
    """Виявляє аномалії в часових рядах"""
    try:
        timeframes = ['5m', '15m', '1h']  # Зменшено кількість таймфреймів
        anomalies = []
        
        for tf in timeframes:
            try:
                ohlcv = gate.fetch_ohlcv(symbol, tf, limit=50)
                if len(ohlcv) < 30:
                    continue
                    
                closes = np.array([x[4] for x in ohlcv])
                
                # Детектуємо аномалії через Z-score
                if len(closes) >= 20:
                    z_scores = np.abs(stats.zscore(closes[-20:]))
                    temporal_anomaly = np.any(z_scores > 2.5)
                    
                    if temporal_anomaly:
                        anomaly_strength = np.max(z_scores)
                        anomalies.append({
                            'timeframe': tf,
                            'strength': anomaly_strength
                        })
            except:
                continue
        
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
# ВИЯВЛЕННЯ ТЕМНИХ ПУЛІВ
# -------------------------

def detect_dark_pool_activity(symbol):
    """Виявляє активність темних пулів"""
    try:
        orderbook = gate.fetch_order_book(symbol, limit=200)  # Зменшено ліміт
        trades = gate.fetch_trades(symbol, limit=100)         # Зменшено ліміт
        
        if not orderbook or not trades:
            return None
        
        # Аналіз дисбалансу об'ємів
        bids = orderbook['bids'][:10] if len(orderbook['bids']) > 10 else orderbook['bids']
        asks = orderbook['asks'][:10] if len(orderbook['asks']) > 10 else orderbook['asks']
        
        bids_volume = sum(bid[1] for bid in bids) if bids else 1
        asks_volume = sum(ask[1] for ask in asks) if asks else 1
        
        volume_imbalance = (bids_volume - asks_volume) / (bids_volume + asks_volume)
        
        # Аналіз великих торгів
        large_trades = [t for t in trades if t['amount'] * t['price'] > 50000]
        large_buys = sum(1 for t in large_trades if t.get('side') == 'buy')
        large_sells = sum(1 for t in large_trades if t.get('side') == 'sell')
        
        dark_pool_score = volume_imbalance
        
        if large_trades:
            dark_pool_score += (large_buys - large_sells) / len(large_trades)
        
        return {
            'symbol': symbol,
            'volume_imbalance': volume_imbalance,
            'large_trades_ratio': len(large_trades) / len(trades) if trades else 0,
            'dark_pool_score': dark_pool_score,
            'signal': 'BULLISH' if volume_imbalance > 0.1 else 'BEARISH' if volume_imbalance < -0.1 else 'NEUTRAL'
        }
        
    except Exception as e:
        print(f"Dark pool detection error: {e}")
        return None

# -------------------------
# ВОРТЕКС ЛІКВІДНОСТІ
# -------------------------

def analyze_liquidity_vortex(symbol):
    """Аналізує динаміку ліквідності"""
    try:
        orderbook = gate.fetch_order_book(symbol, limit=100)
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

def liquidity_volatility(orders, lookback=5):
    """Аналізує волатильність ліквідності"""
    if len(orders) < lookback:
        return 0
    
    volumes = [amount for _, amount in orders[:lookback]]
    if len(volumes) < 2:
        return 0
        
    returns = np.diff(volumes) / volumes[:-1]
    return np.std(returns) if len(returns) > 0 else 0

# -------------------------
# НЕЙРОННА МЕРЕЖА В РЕАЛЬНОМУ ЧАСІ
# -------------------------

def neural_market_sentiment(symbol):
    """Аналіз ринкових настроїв"""
    try:
        ohlcv = gate.fetch_ohlcv(symbol, '5m', limit=50)
        orderbook = gate.fetch_order_book(symbol, limit=50)
        trades = gate.fetch_trades(symbol, limit=50)
        
        if len(ohlcv) < 20 or not orderbook:
            return None
        
        # Складові аналізу
        technical_score = analyze_technical_patterns(ohlcv)
        orderbook_score = analyze_orderbook_dynamics(orderbook)
        trade_flow_score = analyze_trade_flow(trades)
        
        # Комбінований сигнал
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
    
    # RSI
    rsi = calculate_rsi(closes)
    rsi_score = 1 - abs(rsi - 50) / 50
    
    # MACD
    macd, signal, histogram = calculate_macd(closes)
    macd_score = 0.5 + (macd - signal) / (2 * max(1, abs(macd))) if macd != 0 else 0.5
    
    return (rsi_score + max(0, min(1, macd_score))) / 2

def analyze_orderbook_dynamics(orderbook):
    """Аналіз динаміки стакану"""
    bids = orderbook['bids'][:10] if len(orderbook['bids']) > 10 else orderbook['bids']
    asks = orderbook['asks'][:10] if len(orderbook['asks']) > 10 else orderbook['asks']
    
    bid_volume = sum(amount for _, amount in bids) if bids else 1
    ask_volume = sum(amount for _, amount in asks) if asks else 1
    
    imbalance = (bid_volume - ask_volume) / (bid_volume + ask_volume)
    return 0.5 + imbalance / 2

def analyze_trade_flow(trades):
    """Аналіз потоку торгів"""
    if not trades or len(trades) == 0:
        return 0.5
        
    buy_volume = sum(t['amount'] for t in trades if t.get('side') == 'buy')
    sell_volume = sum(t['amount'] for t in trades if t.get('side') == 'sell')
    
    total_volume = buy_volume + sell_volume
    if total_volume == 0:
        return 0.5
        
    flow = (buy_volume - sell_volume) / total_volume
    return 0.5 + flow / 2

# -------------------------
# КВАНТОВИЙ ТРЕЙДИНГ
# -------------------------

def quantum_trading_engine():
    """Основний двигун квантового трейдингу"""
    symbols = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT']
    
    all_signals = []
    
    for symbol in symbols:
        try:
            # Запускаємо аналізи
            quantum_signal = quantum_memory_analysis(symbol, memory_depth=30)
            temporal_signal = detect_temporal_anomalies(symbol)
            dark_pool_signal = detect_dark_pool_activity(symbol)
            vortex_signal = analyze_liquidity_vortex(symbol)
            neural_signal = neural_market_sentiment(symbol)
            
            # Комбінуємо сигнали
            signals = [s for s in [quantum_signal, temporal_signal, dark_pool_signal, 
                                  vortex_signal, neural_signal] if s is not None]
            
            if signals:
                composite_score = calculate_composite_score(signals)
                
                if abs(composite_score) > 0.7:  # Зменшено поріг
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
    """Розрахунок комбінованого скору"""
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
    
    return np.tanh(total_score)

# -------------------------
# ВИКОНАННЯ ТОРГІВЛІ
# -------------------------

def execute_quantum_trade(signal):
    """Виконання торгівлі"""
    if not gate:
        return False
    
    try:
        symbol = signal['symbol']
        action = signal['action']
        confidence = signal['confidence'] / 100
        
        if not safety_check(symbol, TRADE_AMOUNT_USD, confidence):
            return False
        
        if DEMO_MODE:
            # Симуляція торгівлі
            msg = f"📊 ДЕМО УГОДА: {action} {symbol}\n"
            msg += f"Впевненість: {signal['confidence']:.1f}%\n"
            msg += f"Розмір: {TRADE_AMOUNT_USD:.2f} USDT\n"
            msg += f"Час: {datetime.now().strftime('%H:%M:%S')}"
            
            bot.send_message(CHAT_ID, msg)
            print(f"{datetime.now()} | 📊 DEMO: {action} {symbol}")
            return True
        else:
            # Реальна торгівля
            ticker = gate.fetch_ticker(symbol)
            price = ticker['last']
            amount = TRADE_AMOUNT_USD / price
            
            if action == 'BUY':
                order = gate.create_market_buy_order(symbol, amount)
                print(f"{datetime.now()} | ✅ QUANTUM BUY: {amount:.6f} {symbol}")
            else:
                order = gate.create_market_sell_order(symbol, amount)
                print(f"{datetime.now()} | ✅ QUANTUM SELL: {amount:.6f} {symbol}")
            
            msg = f"⚛️ КВАНТОВИЙ СИГНАЛ! {symbol}\n"
            msg += f"Дія: {action}\n"
            msg += f"Впевненість: {signal['confidence']:.1f}%\n"
            msg += f"Розмір: {TRADE_AMOUNT_USD:.2f} USDT\n"
            msg += f"Ціна: {price:.6f}"
            
            bot.send_message(CHAT_ID, msg)
            return True
            
    except Exception as e:
        error_msg = f"❌ Помилка торгівлі: {e}"
        print(f"{datetime.now()} | {error_msg}")
        bot.send_message(CHAT_ID, error_msg)
        return False

# -------------------------
# ОСНОВНИЙ ЦИКЛ
# -------------------------

def start_quantum_trading():
    """Основний цикл квантового трейдингу"""
    mode = "ДЕМО-РЕЖИМ" if DEMO_MODE else "РЕАЛЬНИЙ РЕЖИМ"
    bot.send_message(CHAT_ID, f"⚛️ Запуск КВАНТОВОГО ТРЕЙДИНГУ ({mode})...")
    
    last_trade_time = datetime.now() - timedelta(seconds=SAFETY_MECHANISMS['cooldown_period'])
    
    while True:
        try:
            current_time = datetime.now()
            
            if (current_time - last_trade_time).seconds < SAFETY_MECHANISMS['cooldown_period']:
                time.sleep(1)
                continue
            
            print(f"{datetime.now()} | ⚛️ Запуск квантового аналізу...")
            
            signals = quantum_trading_engine()
            
            if signals:
                best_signal = signals[0]
                print(f"{datetime.now()} | 🎯 Найкращий сигнал: {best_signal['symbol']} - {best_signal['confidence']:.1f}%")
                
                if best_signal['confidence'] > 80:  # Зменшено поріг
                    if execute_quantum_trade(best_signal):
                        last_trade_time = datetime.now()
                        time.sleep(SAFETY_MECHANISMS['cooldown_period'])
            
            time.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            print(f"{datetime.now()} | ❌ Критична помилка: {e}")
            time.sleep(60)

# -------------------------
# TELEGRAM КОМАНДИ
# -------------------------

@bot.message_handler(commands=['start'])
def send_welcome(message):
    """Команда старту"""
    mode = "ДЕМО-РЕЖИМ" if DEMO_MODE else "РЕАЛЬНИЙ РЕЖИМ"
    bot.reply_to(message, f"🤖 КВАНТОВИЙ ТРЕЙДИНГ-БОТ ({mode})\n\n"
                         "Доступні команди:\n"
                         "/quantum_scan - Миттєвий аналіз\n"
                         "/status - Статус системи")

@bot.message_handler(commands=['quantum_scan'])
def quantum_scan(message):
    """Миттєвий квантовий сканер"""
    bot.reply_to(message, "🔭 Запуск глибокого квантового сканування...")
    
    signals = quantum_trading_engine()
    if not signals:
        bot.reply_to(message, "⚡ Квантові сигнали не знайдені")
        return
    
    msg = "⚛️ РЕЗУЛЬТАТИ КВАНТОВОГО СКАНУ:\n\n"
    for i, signal in enumerate(signals[:3]):
        msg += f"{i+1}. {signal['symbol']} - {signal['action']}\n"
        msg += f"   Впевненість: {signal['confidence']:.1f}%\n"
        msg += f"   Скор: {signal['composite_score']:.3f}\n\n"
    
    bot.reply_to(message, msg)

@bot.message_handler(commands=['status'])
def send_status(message):
    """Статус системи"""
    try:
        status_msg = f"⚡ СТАТУС СИСТЕМИ:\n\n"
        status_msg += f"• Режим: {'ДЕМО' if DEMO_MODE else 'РЕАЛЬНИЙ'}\n"
        status_msg += f"• Інтервал: {CHECK_INTERVAL}с\n"
        status_msg += f"• Розмір угоди: {TRADE_AMOUNT_USD} USDT\n"
        
        if gate and not DEMO_MODE:
            try:
                balance = gate.fetch_balance()
                usdt_balance = balance['total'].get('USDT', 0)
                status_msg += f"• Баланс: {usdt_balance:.2f} USDT\n"
            except:
                status_msg += "• Баланс: Недоступний\n"
        
        status_msg += f"• Мін. впевненість: {SAFETY_MECHANISMS['min_confidence']*100}%"
        
        bot.reply_to(message, status_msg)
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
    print(f"{datetime.now()} | ⚛️ Запуск КВАНТОВОГО ТРЕЙДИНГ-БОТА...")
    print(f"Режим: {'ДЕМО' if DEMO_MODE else 'РЕАЛЬНИЙ'}")
    
    # Перевірка обов'язкових ключів
    required_keys = [API_KEY_TELEGRAM, CHAT_ID]
    if not all(required_keys):
        print(f"{datetime.now()} | ❌ Відсутні обов'язкові API ключі!")
        exit(1)
    
    if not DEMO_MODE and (not GATE_API_KEY or not GATE_API_SECRET):
        print(f"{datetime.now()} | ❌ Відсутні ключі Gate.io для реального режиму!")
        print("Переходжу в демо-режим...")
        DEMO_MODE = True
    
    setup_webhook()
    threading.Thread(target=start_quantum_trading, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)