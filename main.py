import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from binance.client import Client
from telegram import Bot
from datetime import datetime

# ---------- Налаштування ----------
BINANCE_API_KEY = "тут_твій_ключ"
BINANCE_API_SECRET = "тут_твій_секрет"
TELEGRAM_TOKEN = "тут_твій_токен"
CHAT_ID = "тут_твій_chat_id"

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
bot = Bot(token=TELEGRAM_TOKEN)

# ---------- Функція отримання свічок ----------
def get_klines(symbol="BTCUSDT", interval="1h", limit=200):
    klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(klines, columns=['open_time','open','high','low','close','volume','close_time',
                                       'quote_asset_volume','number_of_trades','taker_buy_base_asset_volume',
                                       'taker_buy_quote_asset_volume','ignore'])
    df['open'] = df['open'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    df['close'] = df['close'].astype(float)
    df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
    return df

# ---------- Базовий SMC аналіз ----------
def analyze_smc(df):
    signals = []
    
    # BOS/ChoCH: простий приклад – дивимося на послідовні high/low
    df['prev_high'] = df['high'].shift(1)
    df['prev_low'] = df['low'].shift(1)
    df['BOS_up'] = df['high'] > df['prev_high']
    df['BOS_down'] = df['low'] < df['prev_low']
    
    # Order Block: мінімум перед пробоєм
    df['OB'] = np.where(df['BOS_up'] | df['BOS_down'], df['low'], np.nan)
    
    # Fair Value Gap: свічка з імбалансом
    df['FVG'] = np.where(df['BOS_up'], df['low'] + (df['high'] - df['close'])/2, np.nan)
    
    return df

# ---------- Побудова графіка ----------
def plot_chart(df, symbol="BTCUSDT"):
    plt.figure(figsize=(15,7))
    plt.plot(df['open_time'], df['close'], label='Close', color='black')
    
    # Order Blocks
    plt.scatter(df['open_time'], df['OB'], color='blue', label='Order Block', marker='s')
    
    # Fair Value Gaps
    plt.scatter(df['open_time'], df['FVG'], color='red', label='FVG', marker='^')
    
    # BOS/ChoCH стрілки
    plt.scatter(df['open_time'][df['BOS_up']], df['close'][df['BOS_up']], color='green', label='BOS Up', marker='^')
    plt.scatter(df['open_time'][df['BOS_down']], df['close'][df['BOS_down']], color='orange', label='BOS Down', marker='v')
    
    plt.title(f"{symbol} - Smart Money Concept Analysis")
    plt.xlabel("Time")
    plt.ylabel("Price")
    plt.legend()
    
    filename = f"{symbol}_smc.png"
    plt.savefig(filename)
    plt.close()
    return filename

# ---------- Відправка в Telegram ----------
def send_telegram_image(filename, chat_id=CHAT_ID, caption="SMC Analysis"):
    bot.send_photo(chat_id=chat_id, photo=open(filename, 'rb'), caption=caption)

# ---------- Головна функція ----------
def main():
    symbol = "BTCUSDT"
    df = get_klines(symbol)
    df = analyze_smc(df)
    chart_file = plot_chart(df, symbol)
    send_telegram_image(chart_file)

if __name__ == "__main__":
    main()