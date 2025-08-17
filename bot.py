# bot.py
import requests
import os
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- Налаштування ---
TOKEN = "8255365352:AAHqFjtxNo02_b6bQwj2ieoFyDAkXmOW4oQ"  # Замініть на токен з @BotFather

# --- Спрощена версія для стабільної роботи ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🪙 Крипто-бот активовано!\n"
        "Доступні команди:\n"
        "/price [монета] - курс монети\n"
        "/new - нові токени\n"
        "/help - довідка"
    )

async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        coin = context.args[0].lower() if context.args else "bitcoin"
        response = requests.get(
            f"https://api.coingecko.com/api/v3/simple/price?ids={coin}&vs_currencies=usd",
            timeout=10
        )
        data = response.json()
        
        if coin in data:
            await update.message.reply_text(f"💰 {coin.capitalize()}: ${data[coin]['usd']}")
        else:
            await update.message.reply_text("❌ Монету не знайдено. Спробуйте /price bitcoin")
    
    except Exception as e:
        await update.message.reply_text("⚠️ Помилка. Спробуйте пізніше.")

async def get_new_coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        response = requests.get(
            "https://api.coingecko.com/api/v3/coins/list",
            timeout=15
        )
        coins = response.json()[:15]  # Обмежуємо кількість
        
        message = "🆕 Останні додані токени:\n\n"
        for coin in coins:
            message += f"• {coin['name']} ({coin['symbol'].upper()})\n"
        
        await update.message.reply_text(message)
    
    except Exception:
        await update.message.reply_text("🔧 Не вдалося отримати дані. Спробуйте пізніше.")

def main():
    app = Application.builder().token(TOKEN).build()
    
    # Команди
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", get_price))
    app.add_handler(CommandHandler("new", get_new_coins))
    app.add_handler(CommandHandler("help", start))
    
    print("Бот запущено...")
    app.run_polling()

if __name__ == "__main__":
    main()
