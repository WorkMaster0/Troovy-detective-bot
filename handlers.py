# handlers.py
import requests
import time
import numpy as np
import pandas as pd
from scipy.signal import argrelextrema
from scipy import stats
import logging
from datetime import datetime, timedelta
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import DBSCAN
import warnings
warnings.filterwarnings('ignore')

logger = logging.getLogger(__name__)

def calculate_ema(prices, period):
    """Власна реалізація EMA"""
    if len(prices) < period:
        return None
    alpha = 2 / (period + 1)
    ema = [prices[0]]
    for i in range(1, len(prices)):
        ema.append(alpha * prices[i] + (1 - alpha) * ema[i-1])
    return ema[-1]

def calculate_sma(prices, period):
    """Власна реалізація SMA"""
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period

def get_klines(symbol, interval="1h", limit=200):
    """Отримання даних з Binance"""
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        result = {
            "t": [item[0] for item in data],
            "o": [float(item[1]) for item in data],
            "h": [float(item[2]) for item in data],
            "l": [float(item[3]) for item in data],
            "c": [float(item[4]) for item in data],
            "v": [float(item[5]) for item in data]
        }
        return result
    except Exception as e:
        logger.error(f"Помилка отримання даних для {symbol}: {e}")
        return None

def find_support_resistance(prices, window=20, delta=0.005):
    """Знаходження рівнів підтримки та опору"""
    if len(prices) < window * 2:
        return []
    
    local_max = argrelextrema(prices, np.greater, order=window)[0]
    local_min = argrelextrema(prices, np.less, order=window)[0]
    
    levels = []
    for i in local_max:
        levels.append(prices[i])
    for i in local_min:
        levels.append(prices[i])
    
    levels = sorted(levels)
    filtered_levels = []
    
    for level in levels:
        if not filtered_levels:
            filtered_levels.append(level)
        else:
            if abs(level - filtered_levels[-1]) / filtered_levels[-1] > delta:
                filtered_levels.append(level)
    
    return filtered_levels

def calculate_volume_profile(closes, volumes, bins=20):
    """Розрахунок Volume Profile"""
    if len(closes) == 0:
        return None
    
    price_min, price_max = min(closes), max(closes)
    price_range = price_max - price_min
    bin_size = price_range / bins
    
    volume_profile = {}
    for i in range(len(closes)):
        price = closes[i]
        volume = volumes[i]
        
        bin_index = int((price - price_min) / bin_size)
        bin_index = min(bin_index, bins-1)
        
        bin_key = round(price_min + bin_index * bin_size, 4)
        volume_profile[bin_key] = volume_profile.get(bin_key, 0) + volume
    
    return volume_profile

def find_high_volume_nodes(volume_profile, top_n=3):
    """Знаходження найвищих Volume Nodes"""
    if not volume_profile:
        return []
    
    sorted_nodes = sorted(volume_profile.items(), key=lambda x: x[1], reverse=True)
    return sorted_nodes[:top_n]

def analyze_volume_heatmap(symbol):
    """Аналіз TVH (Trading Volume Heatmap)"""
    try:
        df = get_klines(symbol, interval="1h", limit=100)
        if not df or len(df["c"]) < 50:
            return None
        
        closes = np.array(df["c"], dtype=float)
        volumes = np.array(df["v"], dtype=float)
        
        # Volume Profile
        volume_profile = calculate_volume_profile(closes, volumes)
        high_volume_nodes = find_high_volume_nodes(volume_profile)
        
        # Current price
        current_price = closes[-1]
        
        # Volume indicators
        avg_volume_20 = np.mean(volumes[-20:]) if len(volumes) >= 20 else np.mean(volumes)
        current_volume = volumes[-1]
        volume_ratio = current_volume / avg_volume_20 if avg_volume_20 > 0 else 1
        
        # Price momentum
        price_change_24h = ((current_price - closes[0]) / closes[0]) * 100
        
        return {
            "symbol": symbol,
            "current_price": current_price,
            "volume_ratio": volume_ratio,
            "price_change_24h": price_change_24h,
            "high_volume_nodes": high_volume_nodes,
            "current_volume": current_volume,
            "avg_volume_20": avg_volume_20
        }
        
    except Exception as e:
        logger.error(f"Помилка аналізу TVH для {symbol}: {e}")
        return None

def analyze_pump_potential(symbol):
    """Аналіз потенціалу пампу"""
    try:
        df_1h = get_klines(symbol, interval="1h", limit=100)
        df_4h = get_klines(symbol, interval="4h", limit=50)
        
        if not df_1h or not df_4h:
            return None
        
        closes_1h = np.array(df_1h["c"], dtype=float)
        volumes_1h = np.array(df_1h["v"], dtype=float)
        closes_4h = np.array(df_4h["c"], dtype=float)
        
        # Знаходимо рівні S/R
        sr_levels = find_support_resistance(closes_1h)
        
        current_price = closes_1h[-1]
        current_volume = volumes_1h[-1]
        avg_volume_20 = np.mean(volumes_1h[-20:]) if len(volumes_1h) >= 20 else np.mean(volumes_1h)
        
        # Перевіряємо пробій рівнів
        breakout_level = None
        for level in sr_levels:
            if current_price > level * 1.01:  # 1% пробій
                breakout_level = level
                break
        
        # Аналіз об'ємів
        volume_spike = current_volume > 2.5 * avg_volume_20
        
        # Моментум
        price_change_1h = ((current_price - closes_1h[-2]) / closes_1h[-2]) * 100 if len(closes_1h) >= 2 else 0
        price_change_4h = ((current_price - closes_4h[-2]) / closes_4h[-2]) * 100 if len(closes_4h) >= 2 else 0
        
        # Визначаємо силу сигналу
        confidence = 0
        if breakout_level and volume_spike:
            confidence = 0.8
        elif volume_spike and price_change_1h > 3:
            confidence = 0.7
        elif breakout_level:
            confidence = 0.6
        
        return {
            "symbol": symbol,
            "breakout_level": breakout_level,
            "current_price": current_price,
            "volume_spike": volume_spike,
            "volume_ratio": current_volume / avg_volume_20,
            "price_change_1h": price_change_1h,
            "price_change_4h": price_change_4h,
            "confidence": confidence,
            "timestamp": datetime.now()
        }
        
    except Exception as e:
        logger.error(f"Помилка аналізу пампу для {symbol}: {e}")
        return None

def get_top_pump_candidates(limit=15):
    """Отримання топ кандидатів для пампу"""
    try:
        url = "https://api.binance.com/api/v3/ticker/24hr"
        data = requests.get(url, timeout=10).json()
        
        usdt_pairs = [
            d for d in data 
            if d["symbol"].endswith("USDT") and float(d["quoteVolume"]) > 2_000_000  # Зменшимо мінімальний об'єм
        ]
        
        # Сортуємо за зміною ціни та об'ємом
        sorted_symbols = sorted(
            usdt_pairs,
            key=lambda x: (abs(float(x["priceChangePercent"])), float(x["quoteVolume"])),
            reverse=True
        )
        
        top_symbols = [s["symbol"] for s in sorted_symbols[:50]]  # 50 монет
        
        pump_candidates = []
        for symbol in top_symbols:
            analysis = analyze_pump_potential(symbol)
            if analysis and analysis["confidence"] > 0.5:
                pump_candidates.append(analysis)
        
        # Сортуємо за впевненістю
        pump_candidates.sort(key=lambda x: x["confidence"], reverse=True)
        return pump_candidates[:limit]
        
    except Exception as e:
        logger.error(f"Помилка отримання топ кандидатів: {e}")
        return []

# ==================== НОВІ УНІКАЛЬНІ ФУНКЦІЇ ====================

def detect_whale_activity(symbol, window=4):
    """Детекція китівської активності через аномальні об'єми"""
    try:
        df = get_klines(symbol, interval="15m", limit=100)
        if not df or len(df["c"]) < 50:
            return None
        
        volumes = np.array(df["v"], dtype=float)
        closes = np.array(df["c"], dtype=float)
        
        # Аналіз аномальних об'ємів
        volume_mean = np.mean(volumes[-window*6:])
        volume_std = np.std(volumes[-window*6:])
        
        current_volume = volumes[-1]
        z_score = (current_volume - volume_mean) / volume_std if volume_std > 0 else 0
        
        # Перевірка на китівську активність
        whale_activity = z_score > 3.0  # 3 sigma - сильна аномалія
        
        # Аналіз крупних ордерів
        large_orders = current_volume > volume_mean * 5
        
        return {
            "symbol": symbol,
            "whale_detected": whale_activity or large_orders,
            "z_score": z_score,
            "volume_ratio": current_volume / volume_mean if volume_mean > 0 else 1,
            "current_volume": current_volume,
            "avg_volume": volume_mean,
            "price": closes[-1]
        }
        
    except Exception as e:
        logger.error(f"Помилка детекції китів для {symbol}: {e}")
        return None

def calculate_liquidity_zones(symbol):
    """Розрахунок ліквідних зон на основі кластеризації цін"""
    try:
        df = get_klines(symbol, interval="1h", limit=200)
        if not df or len(df["c"]) < 100:
            return None
        
        closes = np.array(df["c"], dtype=float)
        volumes = np.array(df["v"], dtype=float)
        
        # Кластеризація цін для знаходження ліквідних зон
        prices = closes.reshape(-1, 1)
        scaler = StandardScaler()
        prices_scaled = scaler.fit_transform(prices)
        
        # DBSCAN для знаходження кластерів (ліквідних зон)
        dbscan = DBSCAN(eps=0.1, min_samples=5)
        clusters = dbscan.fit_predict(prices_scaled)
        
        liquidity_zones = {}
        for i, cluster in enumerate(clusters):
            if cluster != -1:  # Ігноруємо шум
                price = closes[i]
                volume = volumes[i]
                liquidity_zones[cluster] = liquidity_zones.get(cluster, []) + [(price, volume)]
        
        # Аналіз зон
        analyzed_zones = []
        for cluster, data in liquidity_zones.items():
            prices_in_zone = [item[0] for item in data]
            volumes_in_zone = [item[1] for item in data]
            
            zone_center = np.mean(prices_in_zone)
            zone_volume = sum(volumes_in_zone)
            zone_density = len(prices_in_zone)
            
            analyzed_zones.append({
                "center": zone_center,
                "total_volume": zone_volume,
                "density": zone_density,
                "min_price": min(prices_in_zone),
                "max_price": max(prices_in_zone)
            })
        
        # Сортуємо за об'ємом
        analyzed_zones.sort(key=lambda x: x["total_volume"], reverse=True)
        
        return {
            "symbol": symbol,
            "liquidity_zones": analyzed_zones[:5],  # Топ-5 зон
            "current_price": closes[-1]
        }
        
    except Exception as e:
        logger.error(f"Помилка розрахунку ліквідних зон для {symbol}: {e}")
        return None

def predict_volatility_spikes(symbol):
    """Предикція сплесків волатильності на основі історичних даних"""
    try:
        df = get_klines(symbol, interval="1h", limit=100)
        if not df or len(df["c"]) < 80:
            return None
        
        closes = np.array(df["c"], dtype=float)
        highs = np.array(df["h"], dtype=float)
        lows = np.array(df["l"], dtype=float)
        
        # Розрахунок True Range (виправлена версія)
        tr_values = []
        for i in range(1, len(closes)):
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - closes[i-1])
            lc = abs(lows[i] - closes[i-1])
            tr = max(hl, hc, lc)  # Використовуємо Python max замість np.maximum
            tr_values.append(tr)
        
        # ATR (Average True Range)
        atr = np.mean(tr_values[-14:]) if len(tr_values) >= 14 else np.mean(tr_values) if tr_values else 0
        
        # Поточний True Range
        current_tr = 0
        if len(closes) >= 2:
            hl_current = highs[-1] - lows[-1]
            hc_current = abs(highs[-1] - closes[-2])
            lc_current = abs(lows[-1] - closes[-2])
            current_tr = max(hl_current, hc_current, lc_current)
        
        # Аналіз волатильності через стандартне відхилення
        returns = np.diff(np.log(closes))
        current_volatility = np.std(returns[-20:]) if len(returns) >= 20 else 0
        avg_volatility = np.std(returns[-100:]) if len(returns) >= 100 else 0
        
        # Предикція сплеску
        volatility_ratio = current_volatility / avg_volatility if avg_volatility > 0 else 1
        volatility_spike_predicted = volatility_ratio < 0.7 and current_tr > atr * 1.5
        
        return {
            "symbol": symbol,
            "volatility_spike_predicted": volatility_spike_predicted,
            "current_volatility": current_volatility,
            "avg_volatility": avg_volatility,
            "volatility_ratio": volatility_ratio,
            "atr": atr,
            "true_range": current_tr,
            "price": closes[-1]
        }
        
    except Exception as e:
        logger.error(f"Помилка предикції волатильності для {symbol}: {e}")
        return None

def detect_market_manipulation(symbol):
    """Детекція можливих маніпуляцій ринком"""
    try:
        df = get_klines(symbol, interval="5m", limit=200)
        if not df or len(df["c"]) < 100:
            return None
        
        closes = np.array(df["c"], dtype=float)
        volumes = np.array(df["v"], dtype=float)
        highs = np.array(df["h"], dtype=float)
        lows = np.array(df["l"], dtype=float)
        
        # Аналіз співвідношення ціна/об'єм
        price_changes = np.diff(closes) / closes[:-1]
        volume_changes = np.diff(volumes) / volumes[:-1]
        
        # Кореляція між зміною ціни та об'ємом
        if len(price_changes) >= 20 and len(volume_changes) >= 20:
            correlation = np.corrcoef(price_changes[-20:], volume_changes[-20:])[0, 1]
        else:
            correlation = 0
        
        # Аналіз аномальних свічок
        recent_candles = []
        for i in range(-10, 0):
            if abs(i) <= len(closes):
                body_size = abs(closes[i] - df["o"][i])
                total_range = highs[i] - lows[i]
                if total_range > 0:
                    body_ratio = body_size / total_range
                    recent_candles.append(body_ratio)
        
        avg_body_ratio = np.mean(recent_candles) if recent_candles else 0.5
        
        # Ознаки маніпуляції
        manipulation_signs = {
            "low_correlation": abs(correlation) < 0.3,  # Слабка кореляція
            "high_volume_low_move": np.mean(volume_changes[-5:]) > 2 and np.mean(abs(price_changes[-5:])) < 0.01,
            "abnormal_body_ratios": avg_body_ratio < 0.2 or avg_body_ratio > 0.8,
            "wash_trading": False  # Можна додати advanced detection
        }
        
        manipulation_score = sum(manipulation_signs.values())
        
        return {
            "symbol": symbol,
            "manipulation_detected": manipulation_score >= 2,
            "manipulation_score": manipulation_score,
            "correlation": correlation,
            "avg_body_ratio": avg_body_ratio,
            "price": closes[-1]
        }
        
    except Exception as e:
        logger.error(f"Помилка детекції маніпуляцій для {symbol}: {e}")
        return None

def find_rocket_pumps():
    """Пошук монет для мгновенних пампів (як MYX, SOMI)"""
    try:
        url = "https://api.binance.com/api/v3/ticker/24hr"
        data = requests.get(url, timeout=10).json()
        
        # Фільтруємо перспективні монети
        potential_pumps = [
            d for d in data 
            if d["symbol"].endswith("USDT") 
            and 500_000 < float(d["quoteVolume"]) < 5_000_000  # Середній об'єм
            and abs(float(d["priceChangePercent"])) < 10  # Ще не полетіли
            and not d["symbol"].startswith(('BTC', 'ETH', 'BNB'))  # Не мейджори
        ]
        
        rocket_signals = []
        
        for coin in potential_pumps[:50]:  # Перевіряємо топ-50
            symbol = coin["symbol"]
            
            try:
                # Аналізуємо 5m таймфрейм для швидких змін
                df = get_klines(symbol, interval="5m", limit=50)
                if not df or len(df["c"]) < 20:
                    continue
                
                closes = np.array(df["c"], dtype=float)
                volumes = np.array(df["v"], dtype=float)
                
                # Критерії для "ракети":
                current_volume = volumes[-1]
                avg_volume = np.mean(volumes[-20:])
                volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1
                
                price_change_5m = ((closes[-1] - closes[-2]) / closes[-2]) * 100 if len(closes) >= 2 else 0
                price_change_15m = ((closes[-1] - closes[-4]) / closes[-4]) * 100 if len(closes) >= 4 else 0
                
                # Сильні сигнали для пампу
                is_rocket = (
                    volume_ratio > 3.0 and  # Об'єм виріс в 3+ рази
                    price_change_5m > 2.0 and  # +2% за 5 хвилин
                    price_change_15m < 5.0 and  # Ще не напамповано
                    current_volume > 10000  # Мінімальний абсолютний об'єм
                )
                
                if is_rocket:
                    rocket_signals.append({
                        "symbol": symbol,
                        "volume_ratio": volume_ratio,
                        "price_change_5m": price_change_5m,
                        "current_price": closes[-1],
                        "current_volume": current_volume,
                        "timestamp": datetime.now()
                    })
                    
            except Exception as e:
                continue
        
        return rocket_signals
        
    except Exception as e:
        logger.error(f"Помилка пошуку ракет: {e}")
        return []

def find_golden_crosses():
    """Пошук хрестів за зміною ціни - НАЙКРАЩА ВЕРСІЯ"""
    try:
        url = "https://api.binance.com/api/v3/ticker/24hr"
        data = requests.get(url, timeout=10).json()
        
        usdt_pairs = [
            d for d in data 
            if d["symbol"].endswith("USDT") 
            and float(d["quoteVolume"]) > 1_000_000  # Мінімальний об'єм
            and not d["symbol"].startswith('BUSD')   # Виключаємо стейблкоїни
        ]
        
        # ✅ Сортуємо за ЗМІНОЮ ЦІНИ (найважливіше!)
        sorted_symbols = sorted(
            usdt_pairs,
            key=lambda x: abs(float(x["priceChangePercent"])),  # Абсолютна зміна
            reverse=True
        )
        
        # Беремо топ-30 найволатильніших монет
        top_symbols = [s["symbol"] for s in sorted_symbols[:30]]
        
        golden_crosses = []
        
        for symbol in top_symbols:
            try:
                # Використовуємо 4h таймфрейм для кращого виявлення
                df = get_klines(symbol, interval="4h", limit=50)
                if not df or len(df["c"]) < 30:
                    continue
                
                closes = np.array(df["c"], dtype=float)
                
                # Спрощений розрахунок EMA
                def calculate_simple_ema(prices, period):
                    if len(prices) < period:
                        return None
                    alpha = 2 / (period + 1)
                    ema = prices[0]
                    for price in prices[1:]:
                        ema = alpha * price + (1 - alpha) * ema
                    return ema
                
                # EMA для поточного і попереднього періоду
                current_ema20 = calculate_simple_ema(closes, 20)
                current_ema50 = calculate_simple_ema(closes, 50)
                
                # Для попереднього періоду беремо дані без останньої свічки
                prev_ema20 = calculate_simple_ema(closes[:-1], 20) if len(closes) > 20 else None
                prev_ema50 = calculate_simple_ema(closes[:-1], 50) if len(closes) > 50 else None
                
                if None in [current_ema20, current_ema50, prev_ema20, prev_ema50]:
                    continue
                
                # Визначаємо тип хреста
                price_diff_percent = abs((current_ema20 - current_ema50) / current_ema50 * 100)
                
                # Золотий хрест
                if prev_ema20 < prev_ema50 and current_ema20 > current_ema50:
                    golden_crosses.append({
                        "symbol": symbol,
                        "type": "GOLDEN",
                        "ema20": current_ema20,
                        "ema50": current_ema50,
                        "price": closes[-1],
                        "crossover_strength": price_diff_percent
                    })
                
                # Смертельний хрест
                elif prev_ema20 > prev_ema50 and current_ema20 < current_ema50:
                    golden_crosses.append({
                        "symbol": symbol, 
                        "type": "DEATH",
                        "ema20": current_ema20,
                        "ema50": current_ema50,
                        "price": closes[-1],
                        "crossover_strength": price_diff_percent
                    })
                
            except Exception as e:
                continue
        
        # Сортуємо за силою хреста
        golden_crosses.sort(key=lambda x: x["crossover_strength"], reverse=True)
        return golden_crosses[:10]  # Топ-10 найсильніших
        
    except Exception as e:
        logger.error(f"Помилка пошуку хрестів: {e}")
        return []

def get_smart_money_indicators(symbol):
    """Індикатори Smart Money"""
    try:
        df = get_klines(symbol, interval="1h", limit=100)
        if not df or len(df["c"]) < 50:
            return None
        
        closes = np.array(df["c"], dtype=float)
        highs = np.array(df["h"], dtype=float)
        lows = np.array(df["l"], dtype=float)
        volumes = np.array(df["v"], dtype=float)
        
        # Cumulative Volume Delta
        buy_volume = sum([volumes[i] for i in range(len(closes)) if closes[i] > df["o"][i]])
        sell_volume = sum([volumes[i] for i in range(len(closes)) if closes[i] < df["o"][i]])
        volume_delta = (buy_volume - sell_volume) / (buy_volume + sell_volume) if (buy_volume + sell_volume) > 0 else 0
        
        # Price-Volume Divergence
        price_change = (closes[-1] - closes[-20]) / closes[-20] * 100 if len(closes) >= 20 else 0
        volume_change = (volumes[-1] - np.mean(volumes[-20:-1])) / np.mean(volumes[-20:-1]) * 100 if len(volumes) >= 20 else 0
        
        divergence = "BULLISH" if price_change > 0 and volume_change > 0 else \
                    "BEARISH" if price_change < 0 and volume_change < 0 else \
                    "HIDDEN_BULLISH" if price_change < 0 and volume_change > 0 else \
                    "HIDDEN_BEARISH" if price_change > 0 and volume_change < 0 else "NEUTRAL"
        
        return {
            "symbol": symbol,
            "volume_delta": volume_delta,
            "buy_pressure": buy_volume / (buy_volume + sell_volume) if (buy_volume + sell_volume) > 0 else 0.5,
            "divergence": divergence,
            "price_change": price_change,
            "volume_change": volume_change,
            "current_price": closes[-1]
        }
        
    except Exception as e:
        logger.error(f"Помилка Smart Money індикаторів для {symbol}: {e}")
        return None