import mplfinance as mpf
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from binance.client import Client
from telegram import Bot, BotCommand, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import asyncio
from datetime import datetime, time

# ---------- Налаштування ----------
BINANCE_API_KEY = "1tgsJl1kRePaygrLuQnp1VMk2Ot0pKvm8Ba348jY4IDgU26jHvqCgD0DeNFYT5qe"
BINANCE_API_SECRET = "RNoPuhvxljSRQtanB7c6AID4k6fL1EB8at6sVg4AbXmPmM2W5ez0MTRn3E1i2Frl"
TELEGRAM_TOKEN = "8051222216:AAFORHEn1IjWllQyPp8W_1OY3gVxcBNVvZI"
CHAT_ID = "6053907025"

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
bot = Bot(token=TELEGRAM_TOKEN)

# ---------- Отримання свічок ----------
def get_klines(symbol="BTCUSDT", interval="1h", limit=300):
    klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(klines, columns=['open_time','open','high','low','close','volume','close_time',
                                       'quote_asset_volume','number_of_trades','taker_buy_base_asset_volume',
                                       'taker_buy_quote_asset_volume','ignore'])
    df['open'] = df['open'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    df['close'] = df['close'].astype(float)
    df['volume'] = df['volume'].astype(float)
    df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
    return df

# ---------- Smart Money аналіз ----------
def analyze_smc(df):
    df = df.copy()
    df['prev_high'] = df['high'].shift(1)
    df['prev_low'] = df['low'].shift(1)

    # HH / HL / LH / LL
    df['HH'] = (df['high'] > df['prev_high']) & (df['close'] > df['prev_high'])
    df['LL'] = (df['low'] < df['prev_low']) & (df['close'] < df['prev_low'])

    # Break of Structure (BOS)
    df['BOS_up'] = df['high'] > df['prev_high']
    df['BOS_down'] = df['low'] < df['prev_low']

    # Change of Character (ChoCH)
    df['ChoCH_up'] = df['BOS_up'] & df['LL'].shift(1)
    df['ChoCH_down'] = df['BOS_down'] & df['HH'].shift(1)

    # Order Block
    df['OB'] = np.where(df['BOS_up'], df['low'].shift(1),
                        np.where(df['BOS_down'], df['high'].shift(1), np.nan))

    # Fair Value Gap (FVG)
    df['FVG'] = np.where(df['BOS_up'], df['low'] + (df['high']-df['close'])/2,
                         np.where(df['BOS_down'], df['high'] - (df['close']-df['low'])/2, np.nan))

    # Liquidity Pools (BSL / SSL)
    df['BSL'] = df['high'].rolling(window=5).max()
    df['SSL'] = df['low'].rolling(window=5).min()

    # Сигнали
    df['Signal'] = np.where(df['BOS_up'], 'BUY',
                     np.where(df['BOS_down'], 'SELL', None)).astype(object)
    df['SL'] = np.where(df['Signal']=='BUY', df['OB']*0.995,
                 np.where(df['Signal']=='SELL', df['OB']*1.005, np.nan))
    df['TP'] = np.where(df['Signal']=='BUY', df['close']*1.01,
                 np.where(df['Signal']=='SELL', df['close']*0.99, np.nan))

    return df

# ---------- Побудова графіка ----------
def plot_chart(df, symbol="BTCUSDT"):
    df_plot = df[['open_time','open','high','low','close','volume']].copy()
    df_plot.set_index('open_time', inplace=True)

    apds = []

    # Order Blocks
    if 'OB' in df.columns:
        apds.append(mpf.make_addplot(df['OB'], type='scatter', markersize=100, marker='s', color='blue'))

    # Fair Value Gaps
    if 'FVG' in df.columns:
        apds.append(mpf.make_addplot(df['FVG'], type='scatter', markersize=100, marker='^', color='red'))

    # Liquidity Pools
    if 'BSL' in df.columns:
        apds.append(mpf.make_addplot(df['BSL'], color='purple', linestyle='dashed'))
    if 'SSL' in df.columns:
        apds.append(mpf.make_addplot(df['SSL'], color='cyan', linestyle='dashed'))

    # Stop Loss / Take Profit
    if 'SL' in df.columns:
        apds.append(mpf.make_addplot(df['SL'], type='scatter', markersize=60, marker='x', color='red'))
    if 'TP' in df.columns:
        apds.append(mpf.make_addplot(df['TP'], type='scatter', markersize=80, marker='*', color='green'))

    # BOS / ChoCH
    if 'BOS_up' in df.columns:
        apds.append(mpf.make_addplot(df['close'][df['BOS_up']], type='scatter', markersize=100, marker='^', color='lime'))
    if 'BOS_down' in df.columns:
        apds.append(mpf.make_addplot(df['close'][df['BOS_down']], type='scatter', markersize=100, marker='v', color='orange'))
    if 'ChoCH_up' in df.columns:
        apds.append(mpf.make_addplot(df['close'][df['ChoCH_up']], type='scatter', markersize=140, marker='o', color='green'))
    if 'ChoCH_down' in df.columns:
        apds.append(mpf.make_addplot(df['close'][df['ChoCH_down']], type='scatter', markersize=140, marker='o', color='magenta'))

    filename = f"{symbol}_smc.png"
    mpf.plot(
        df_plot,
        type='candle',
        style='yahoo',
        title=f"{symbol} - Smart Money Concept",
        addplot=apds if apds else None,
        volume=True,
        savefig=filename
    )
    return filename

# ---------- Асинхронна відправка ----------
async def send_telegram_image(filename, chat_id=CHAT_ID, caption="SMC Analysis"):
    with open(filename, 'rb') as f:
        await bot.send_photo(chat_id=chat_id, photo=f, caption=caption)

# ---------- Команди ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Привіт! Я бот Smart Money.\n\n"
                                    "Команди:\n"
                                    "/smc SYMBOL TIMEFRAME – повний Smart Money аналіз\n"
                                    "/liqmap SYMBOL TIMEFRAME – карта ліквідності\n"
                                    "/orderflow SYMBOL TIMEFRAME – ордер флоу\n")

async def smc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Генерую повний Smart Money аналіз...")
    try:
        symbol = context.args[0].upper() if len(context.args) >= 1 else "BTCUSDT"
        interval = context.args[1] if len(context.args) >= 2 else "1h"
        df = analyze_smc(get_klines(symbol, interval))
        chart_file = plot_chart(df, symbol)
        latest_signal = df.dropna(subset=['Signal']).tail(1)
        if latest_signal.empty:
            await update.message.reply_text(f"⚠️ Немає сигналу для {symbol} {interval}")
            return
        row = latest_signal.iloc[0]
        caption = (f"📊 Smart Money Concept для *{symbol} {interval}*:\n\n"
                   f"{row['open_time'].strftime('%Y-%m-%d %H:%M')} | {row['Signal']} | "
                   f"Entry: {row['close']:.2f} | SL: {row['SL']:.2f} | TP: {row['TP']:.2f}")
        await send_telegram_image(chart_file, caption=caption)
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка: {e}")

# ---------- Запуск ----------
if __name__ == "__main__":
    import os
    WEBHOOK_URL = "https://quantum-trading-bot-wg5k.onrender.com/"
    PORT = int(os.environ.get("PORT", 10000))

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("smc", smc_command))

    async def set_commands():
        await app.bot.set_my_commands([
            BotCommand("start", "Запустити бота"),
            BotCommand("smc", "Smart Money аналіз"),
            BotCommand("liqmap", "Карта ліквідності"),
            BotCommand("orderflow", "Ордер флоу")
        ])

    asyncio.get_event_loop().run_until_complete(set_commands())
    app.run_webhook(listen="0.0.0.0", port=PORT, webhook_url=WEBHOOK_URL)