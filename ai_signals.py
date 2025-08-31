# ai_signals.py
import requests
import numpy as np
import pandas as pd
from datetime import datetime

class AISignalGenerator:
    def __init__(self):
        self.api_url = "https://api.binance.com/api/v3"
        
    def calculate_rsi(self, prices, period=14):
        """Власна реалізація RSI"""
        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        
        avg_gain = np.mean(gains[:period])
        avg_loss = np.mean(losses[:period])
        
        for i in range(period, len(prices)-1):
            avg_gain = (avg_gain * (period-1) + gains[i]) / period
            avg_loss = (avg_loss * (period-1) + losses[i]) / period
        
        if avg_loss == 0:
            return 100
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
    
    def calculate_macd(self, prices, fast=12, slow=26, signal=9):
        """Власна реалізація MACD"""
        ema_fast = pd.Series(prices).ewm(span=fast).mean().values
        ema_slow = pd.Series(prices).ewm(span=slow).mean().values
        macd_line = ema_fast - ema_slow
        signal_line = pd.Series(macd_line).ewm(span=signal).mean().values
        return macd_line[-1], signal_line[-1]
    
    def get_klines(self, symbol, interval='1h', limit=100):
        """Отримання даних з Binance"""
        try:
            url = f"{self.api_url}/klines?symbol={symbol}&interval={interval}&limit={limit}"
            response = requests.get(url, timeout=10)
            data = response.json()
            
            closes = [float(k[4]) for k in data]  # Close prices
            highs = [float(k[2]) for k in data]   # High prices
            lows = [float(k[3]) for k in data]    # Low prices
            
            return closes, highs, lows
            
        except Exception as e:
            print(f"Помилка отримання даних: {e}")
            return [], [], []
    
    def generate_signals(self, symbol='BTCUSDT', timeframe='1h'):
        """Генерація торгових сигналів"""
        try:
            closes, highs, lows = self.get_klines(symbol, timeframe)
            
            if len(closes) < 30:
                return 'HOLD', 0.5
                
            # Власні індикатори
            rsi = self.calculate_rsi(closes[-15:])
            macd, signal = self.calculate_macd(closes)
            
            # Генерація сигналів
            current_price = closes[-1]
            prev_price = closes[-2]
            
            # Прості торгові правила
            if rsi < 30 and macd > signal and current_price > prev_price:
                return 'BUY', 0.8
            elif rsi > 70 and macd < signal and current_price < prev_price:
                return 'SELL', 0.75
            else:
                return 'HOLD', 0.6
                
        except Exception as e:
            print(f"Помилка генерації сигналів: {e}")
            return 'HOLD', 0.5