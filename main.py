import mplfinance as mpf
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
    df['volume'] = df['volume'].astype(float)
    df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
    return df

# ---------- SMC аналіз ----------
def analyze_smc(df):
    df = df.copy()
    df['prev_high'] = df['high'].shift(1)
    df['prev_low'] = df['low'].shift(1)
    
    df['BOS_up'] = df['high'] > df['prev_high']
    df['BOS_down'] = df['low'] < df['prev_low']
    
    df['OB'] = np.where(df['BOS_up'], df['low'].shift(1),
                        np.where(df['BOS_down'], df['high'].shift(1), np.nan))
    
    df['Signal'] = np.where(df['BOS_up'] & (~df['OB'].isna()), 'BUY',
                         np.where(df['BOS_down'] & (~df['OB'].isna()), 'SELL', None)).astype(object)
    
    df['SL'] = np.where(df['Signal']=='BUY', df['OB']*0.995, 
                        np.where(df['Signal']=='SELL', df['OB']*1.005, np.nan))
    df['TP'] = np.where(df['Signal']=='BUY', df['close']*1.01, 
                        np.where(df['Signal']=='SELL', df['close']*0.99, np.nan))
    
    return df

# ---------- Побудова графіка ----------
def plot_chart(df, symbol="BTCUSDT"):
    df_plot = df[['open_time','open','high','low','close','volume']].copy()
    df_plot.set_index('open_time', inplace=True)

    # --- свічковий графік ---
    apds = [
        mpf.make_addplot(df['OB'], type='scatter', markersize=80, marker='s', color='blue'),
        mpf.make_addplot(df['FVG'], type='scatter', markersize=80, marker='^', color='red'),
        mpf.make_addplot(df['SL'], type='scatter', markersize=50, marker='x', color='red'),
        mpf.make_addplot(df['TP'], type='scatter', markersize=80, marker='*', color='green'),
    ]

    filename = f"{symbol}_smc.png"
    mpf.plot(
        df_plot,
        type='candle',
        style='yahoo',
        title=f"{symbol} - Smart Money Concept Analysis",
        addplot=apds,
        volume=True,
        savefig=filename
    )
    return filename

# ---------- Асинхронна відправка в Telegram ----------
async def send_telegram_image(filename, chat_id=CHAT_ID, caption="SMC Analysis"):
    with open(filename, 'rb') as f:
        await bot.send_photo(chat_id=chat_id, photo=f, caption=caption)

# ---------- Команди ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Привіт! Я бот Smart Money.\n\n"
                                    "Команди:\n"
                                    "/smc SYMBOL TIMEFRAME – сигнал Smart Money\n"
                                    "/liqmap SYMBOL TIMEFRAME – карта ліквідності\n"
                                    "/orderflow SYMBOL TIMEFRAME – ордер флоу\n")

# ---- SMC ----
async def smc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔎 Генерую сигнал...")
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
        time_str = row['open_time'].strftime('%Y-%m-%d %H:%M')
        caption = (f"📊 Smart Money Signal для *{symbol} {interval}*:\n\n"
                   f"{time_str} | {row['Signal']} | "
                   f"Entry: {row['close']:.2f} | SL: {row['SL']:.2f} | TP: {row['TP']:.2f}")
        await send_telegram_image(chart_file, caption=caption)
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка: {e}")

# ---- LIQUIDITY MAP ----
async def liqmap_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📡 Аналізую ліквідність...")
    try:
        symbol = context.args[0].upper() if len(context.args) >= 1 else "BTCUSDT"
        interval = context.args[1] if len(context.args) >= 2 else "1h"
        df = get_klines(symbol, interval)

        highs = df['high'].rolling(window=5).max()
        lows = df['low'].rolling(window=5).min()

        plt.figure(figsize=(15,7))
        plt.plot(df['open_time'], df['close'], color='black', label="Close")
        plt.scatter(df['open_time'], highs, color='red', label="Liquidity Above (Stop Hunt ↑)")
        plt.scatter(df['open_time'], lows, color='blue', label="Liquidity Below (Stop Hunt ↓)")
        plt.title(f"{symbol} - Liquidity Pools")
        plt.xlabel("Time")
        plt.ylabel("Price")
        plt.legend()
        filename = f"{symbol}_liqmap.png"
        plt.savefig(filename)
        plt.close()

        await send_telegram_image(filename, caption=f"🌊 Liquidity Map для *{symbol} {interval}*")
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка: {e}")

# ---- ORDERFLOW ----
async def orderflow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Аналізую Order Flow...")
    try:
        symbol = context.args[0].upper() if len(context.args) >= 1 else "BTCUSDT"
        interval = context.args[1] if len(context.args) >= 2 else "1h"
        df = get_klines(symbol, interval)

        df['buy_vol'] = np.where(df['close'] > df['open'], df['volume'], df['volume']*0.3)
        df['sell_vol'] = np.where(df['close'] < df['open'], df['volume'], df['volume']*0.3)
        df['delta'] = df['buy_vol'] - df['sell_vol']

        plt.figure(figsize=(15,7))
        plt.bar(df['open_time'], df['delta'], color=np.where(df['delta']>0,'green','red'))
        plt.title(f"{symbol} - Order Flow Delta")
        plt.xlabel("Time")
        plt.ylabel("Delta Volume")
        filename = f"{symbol}_orderflow.png"
        plt.savefig(filename)
        plt.close()

        last_delta = df['delta'].iloc[-1]
        trend = "🟢 Покупці домінують" if last_delta > 0 else "🔴 Продавці домінують"

        await send_telegram_image(filename, caption=f"📊 Order Flow для *{symbol} {interval}*\n\n{trend}")
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка: {e}")

# ---------- Ультра секретна команда ----------
async def ultrasecret(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🕵️ Виконую надсекретну операцію... зачекай...")

    try:
        # 🌀 Секретна логіка (навіть в коді виглядає замасковано)
        import base64, random, hashlib

        # Робимо "ключ" із символа та часу
        symbol = "BTCUSDT"
        interval = "1h"
        if len(context.args) >= 1:
            symbol = context.args[0].upper()
        if len(context.args) >= 2:
            interval = context.args[1]

        df = get_klines(symbol, interval, limit=150)

        # Секретна метрика 🤫
        secret_metric = (
            df["close"].pct_change().rolling(10).std().iloc[-1]
            * random.uniform(0.8, 1.2)
        )

        # Робимо «хеш-печатку» щоб виглядало ще більш секретно
        stamp = hashlib.md5(str(secret_metric).encode()).hexdigest()[:8]

        # Відповідь (користь побачиш сам 😉)
        msg = (
            f"🔒 Ультра-секретний аналіз для {symbol} {interval}\n\n"
            f"Секретна метрика: {secret_metric:.5f}\n"
            f"Ідентифікатор: {stamp}\n\n"
            f"(🧩 Розгадати значення можеш лише сам...)"
        )
        await update.message.reply_text(msg)

    except Exception as e:
        await update.message.reply_text(f"❌ Помилка секретної операції: {e}")

# ---------- Запуск вебхука ----------
if __name__ == "__main__":
    import os
    WEBHOOK_URL = "https://quantum-trading-bot-wg5k.onrender.com/"
    PORT = int(os.environ.get("PORT", 10000))

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("smc", smc_command))
    app.add_handler(CommandHandler("liqmap", liqmap_command))
    app.add_handler(CommandHandler("orderflow", orderflow_command))
    app.add_handler(CommandHandler("ultrasecret", ultrasecret))


    async def set_commands():
        await app.bot.set_my_commands([
            BotCommand("start", "Запустити бота"),
            BotCommand("smc", "Smart Money сигнал"),
            BotCommand("liqmap", "Карта ліквідності"),
            BotCommand("orderflow", "Ордер флоу"),
            BotCommand("mystery", "??")
        ])

    asyncio.get_event_loop().run_until_complete(set_commands())
    app.run_webhook(listen="0.0.0.0", port=PORT, webhook_url=WEBHOOK_URL)