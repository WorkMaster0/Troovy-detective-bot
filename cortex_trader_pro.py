# cortex_trader_pro.py
import telebot
import os
import sqlite3
from datetime import datetime

# Ініціалізація бота
TOKEN = os.getenv('TELEGRAM_TOKEN')
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

# Проста база даних
def init_db():
    conn = sqlite3.connect('cortex.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            telegram_id INTEGER UNIQUE,
            balance REAL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    return conn

# Обробники команд
@bot.message_handler(commands=['start'])
def start(message):
    text = """
🎯 <b>Cortex Trading Ecosystem</b>

💡 <i>Децентралізована платформа для заробітку</i>

💰 <b>Доступні стратегії:</b>
🔸 Blue Chip Hodl - 23.4% місячно
🔸 DeFi Yield Farming - 31.2% місячно
🔸 AI Swing Trading - 28.7% місячно

💸 <b>Мінімальна інвестиція:</b> $10
🕒 <b>Виплати:</b> Щоденно
🔒 <b>Страховий фонд:</b> $50,000

Натисніть /invest для початку!
    """
    bot.send_message(message.chat.id, text)

@bot.message_handler(commands=['invest'])
def invest(message):
    text = """
🎯 <b>ОБЕРІТЬ СТРАТЕГІЮ:</b>

1. 🔸 Blue Chip Hodl - 23.4% місячно
2. 🔸 DeFi Yield Farming - 31.2% місячно  
3. 🔸 AI Swing Trading - 28.7% місячно

Введіть номер стратегії (1, 2 або 3):
    """
    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    if message.text in ['1', '2', '3']:
        bot.send_message(message.chat.id, "Введіть суму інвестиції ($):")
    elif message.text.replace('.', '').isdigit():
        amount = float(message.text)
        if amount >= 10:
            bot.send_message(message.chat.id, f"✅ Інвестиція ${amount} прийнята! Очікуйте виплати щодня.")
        else:
            bot.send_message(message.chat.id, "❌ Мінімальна сума - $10")
    else:
        bot.send_message(message.chat.id, "Натисніть /start для початку")

if __name__ == "__main__":
    init_db()
    print("🧠 Cortex Trader запущено...")
    bot.infinity_polling()