import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from binance.client import Client
from telegram import Bot, BotCommand, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import asyncio
from datetime import datetime

# ---------- Налаштування ----------
BINANCE_API_KEY = "1tgsJl1kRePaygrLuQnp1VMk2Ot0pKvm8Ba348jY4IDgU26jHvqCgD0DeNFYT5qe"
BINANCE_API_SECRET = "RNoPuhvxljSRQtanB7c6AID4k6fL1EB8at6sVg4AbXmPmM2W5ez0MTRn3E1i2Frl"
TELEGRAM_TOKEN = "8051222216:AAFORHEn1IjWllQyPp8W_1OY3gVxcBNVvZI"
CHAT_ID = "6053907025"

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
bot = Bot(token=TELEGRAM_TOKEN)

# ---------- Отримання свічок ----------
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
    df = df.copy()
    df['prev_high'] = df['high'].shift(1)
    df['prev_low'] = df['low'].shift(1)
    
    df['BOS_up'] = df['high'] > df['prev_high']
    df['BOS_down'] = df['low'] < df['prev_low']
    
    df['ChoCH_up'] = df['BOS_down'] & df['BOS_up'].shift(1)
    df['ChoCH_down'] = df['BOS_up'] & df['BOS_down'].shift(1)
    
    df['OB'] = np.where(df['BOS_up'], df['low'].shift(1),
                        np.where(df['BOS_down'], df['high'].shift(1), np.nan))
    
    df['FVG'] = np.where(df['BOS_up'], df['low'] + (df['high'] - df['close'])/2,
                         np.where(df['BOS_down'], df['high'] - (df['close'] - df['low'])/2, np.nan))
    
    df['Signal'] = np.where(df['BOS_up'] & (~df['OB'].isna()), 'BUY',
                         np.where(df['BOS_down'] & (~df['OB'].isna()), 'SELL', None)).astype(object)
    
    df['SL'] = np.where(df['Signal']=='BUY', df['OB']*0.995, np.where(df['Signal']=='SELL', df['OB']*1.005, np.nan))
    df['TP'] = np.where(df['Signal']=='BUY', df['close']*1.01, np.where(df['Signal']=='SELL', df['close']*0.99, np.nan))
    
    return df

# ---------- Побудова графіка ----------
def plot_chart(df, symbol="BTCUSDT"):
    plt.figure(figsize=(15,7))
    plt.plot(df['open_time'], df['close'], label='Close', color='black')
    
    plt.scatter(df['open_time'], df['OB'], color='blue', label='Order Block', marker='s')
    plt.scatter(df['open_time'], df['FVG'], color='red', label='FVG', marker='^')
    
    plt.scatter(df['open_time'][df['BOS_up']], df['close'][df['BOS_up']], color='green', label='BOS Up', marker='^')
    plt.scatter(df['open_time'][df['BOS_down']], df['close'][df['BOS_down']], color='orange', label='BOS Down', marker='v')
    plt.scatter(df['open_time'][df['ChoCH_up']], df['close'][df['ChoCH_up']], color='lime', label='ChoCH Up', marker='o')
    plt.scatter(df['open_time'][df['ChoCH_down']], df['close'][df['ChoCH_down']], color='magenta', label='ChoCH Down', marker='o')
    
    plt.scatter(df['open_time'], df['SL'], color='red', label='Stop Loss', marker='x')
    plt.scatter(df['open_time'], df['TP'], color='green', label='Take Profit', marker='*')
    
    plt.title(f"{symbol} - Smart Money Concept Analysis")
    plt.xlabel("Time")
    plt.ylabel("Price")
    plt.legend()
    
    filename = f"{symbol}_smc.png"
    plt.savefig(filename)
    plt.close()
    return filename

# ---------- Асинхронна відправка в Telegram ----------
async def send_telegram_image(filename, chat_id=CHAT_ID, caption="SMC Analysis"):
    with open(filename, 'rb') as f:
        await bot.send_photo(chat_id=chat_id, photo=f, caption=caption)

# ---------- Команди для Telegram ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привіт! Я бот Smart Money. Використовуй /smc щоб отримати сигнали.")

async def smc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Генерую сигнали, зачекай...")
    try:
        df = analyze_smc(get_klines("BTCUSDT"))
        chart_file = plot_chart(df, "BTCUSDT")
        latest_signals = df.dropna(subset=['Signal']).tail(5)
        text_signals = ""
        for idx, row in latest_signals.iterrows():
            time_str = row['open_time'].strftime('%Y-%m-%d %H:%M')
            text_signals += f"{time_str} | {row['Signal']} | Entry: {row['close']:.2f} | SL: {row['SL']:.2f} | TP: {row['TP']:.2f}\n"
        caption = f"Smart Money Signals:\n\n{text_signals}"
        await send_telegram_image(chart_file, caption=caption)
    except Exception as e:
        await update.message.reply_text(f"Помилка: {e}")

# ---------- Запуск вебхука на Render ----------
if __name__ == "__main__":
    WEBHOOK_URL = "https://quantum-trading-bot-wg5k.onrender.com/"
    PORT = 10000  # Render зазвичай дозволяє цей порт, можна замінити os.environ['PORT']

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("smc", smc_command))

    async def main():
        # встановлюємо кнопки-команди
        await app.bot.set_my_commands([
            BotCommand("start", "Запустити бота"),
            BotCommand("smc", "Отримати сигнали Smart Money")
        ])

        # запускаємо вебхук
        await app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=WEBHOOK_URL
        )

    asyncio.run(main())