# ai_signals.py
import requests
import talib
import numpy as np
import pandas as pd
from binance.client import Client

class AISignalGenerator:
    def __init__(self):
        self.binance = Client()
        
    def generate_signals(self, symbol, timeframe='1h'):
        """Генерація торгових сигналів"""
        try:
            # Отримання даних
            klines = self.binance.get_klines(
                symbol=symbol, 
                interval=timeframe, 
                limit=100
            )
            
            df = pd.DataFrame(klines, columns=[
                'time', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_asset_volume', 'trades',
                'taker_buy_base', 'taker_buy_quote', 'ignore'
            ])
            
            # Конвертація типів
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col])
            
            # Технічні індикатори
            df['rsi'] = talib.RSI(df['close'], timeperiod=14)
            df['macd'], df['macd_signal'], _ = talib.MACD(
                df['close'], fastperiod=12, slowperiod=26, signalperiod=9
            )
            
            # Генерація сигналів
            current = df.iloc[-1]
            
            if current['rsi'] < 30 and current['macd'] > current['macd_signal']:
                return 'BUY', 0.85  # Сигнал купівлі з впевненістю 85%
            elif current['rsi'] > 70 and current['macd'] < current['macd_signal']:
                return 'SELL', 0.80  # Сигнал продажу
            else:
                return 'HOLD', 0.60
                
        except Exception as e:
            return 'HOLD', 0.50