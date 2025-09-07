# handlers.py
import requests
import numpy as np
from scipy.signal import argrelextrema
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

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

def get_top_pump_candidates(limit=10):
    """Отримання топ кандидатів для пампу"""
    try:
        url = "https://api.binance.com/api/v3/ticker/24hr"
        data = requests.get(url, timeout=10).json()
        
        usdt_pairs = [
            d for d in data 
            if d["symbol"].endswith("USDT") and float(d["quoteVolume"]) > 3_000_000
        ]
        
        # Сортуємо за зміною ціни та об'ємом
        sorted_symbols = sorted(
            usdt_pairs,
            key=lambda x: (abs(float(x["priceChangePercent"])), float(x["quoteVolume"])),
            reverse=True
        )
        
        top_symbols = [s["symbol"] for s in sorted_symbols[:30]]
        
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
