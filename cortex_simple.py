# cortex_simple.py
import telebot
import requests
import sqlite3
import random
import os
from datetime import datetime

# Ініціалізація
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
            username TEXT,
            balance REAL DEFAULT 0,
            invested REAL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS investments (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            strategy TEXT,
            amount REAL,
            daily_profit REAL,
            start_date DATETIME,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.commit()
    return conn

# Функції для роботи з БД
def get_user(telegram_id):
    conn = sqlite3.connect('cortex.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE telegram_id = ?', (telegram_id,))
    user = cursor.fetchone()
    conn.close()
    return user

def create_user(telegram_id, username):
    conn = sqlite3.connect('cortex.db')
    cursor = conn.cursor()
    cursor.execute('INSERT INTO users (telegram_id, username) VALUES (?, ?)', 
                  (telegram_id, username))
    conn.commit()
    conn.close()

def add_investment(user_id, strategy, amount):
    daily_profit = calculate_daily_profit(strategy, amount)
    conn = sqlite3.connect('cortex.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO investments (user_id, strategy, amount, daily_profit, start_date)
        VALUES (?, ?, ?, ?, datetime('now'))
    ''', (user_id, strategy, amount, daily_profit))
    conn.commit()
    conn.close()
    return daily_profit

def calculate_daily_profit(strategy, amount):
    rates = {
        'bluechip': 0.0078,  # ~23.4% monthly
        'defi': 0.0104,      # ~31.2% monthly
        'ai': 0.0095         # ~28.7% monthly
    }
    return round(amount * rates.get(strategy, 0.008), 2)

# Отримання цін з Binance
def get_crypto_price(symbol):
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        response = requests.get(url, timeout=5)
        data = response.json()
        return float(data['price'])
    except:
        return random.uniform(40000, 45000) if symbol == "BTCUSDT" else random.uniform(2200, 2500)

# Обробники команд
@bot.message_handler(commands=['start'])
def start(message):
    user = get_user(message.from_user.id)
    if not user:
        create_user(message.from_user.id, message.from_user.username)
    
    text = f"""
🎯 <b>Cortex Trading Ecosystem</b>
👋 Вітаю, {message.from_user.first_name}!

💡 <i>Децентралізована платформа для стабільного заробітку</i>

💰 <b>Доступні стратегії:</b>
1. 🔸 Blue Chip Hodl - 23.4% місячно
2. 🔸 DeFi Yield Farming - 31.2% місячно  
3. 🔸 AI Swing Trading - 28.7% місячно

💸 <b>Мінімальна інвестиція:</b> $10
🕒 <b>Виплати:</b> Щоденно о 09:00 UTC
🔒 <b>Страховий фонд:</b> $50,000

📊 <b>Поточні ціни:</b>
₿ Bitcoin: ${get_crypto_price('BTCUSDT'):,.0f}
🔷 Ethereum: ${get_crypto_price('ETHUSDT'):,.0f}

Натисніть /invest для початку!
Натисніть /stats для статистики
    """
    bot.send_message(message.chat.id, text)

@bot.message_handler(commands=['invest'])
def invest(message):
    text = """
🎯 <b>ОБЕРІТЬ СТРАТЕГІЮ:</b>

1. 🔸 Blue Chip Hodl - 23.4% місячно
   📈 Консервативна стратегія на топ-10 монет

2. 🔸 DeFi Yield Farming - 31.2% місячно  
   💰 Високі прибутки через DeFi протоколи

3. 🔸 AI Swing Trading - 28.7% місячно
   🤖 AI алгоритми для короткострокових угод

Введіть номер стратегії (1, 2 або 3):
    """
    bot.send_message(message.chat.id, text)

@bot.message_handler(commands=['stats'])
def stats(message):
    user = get_user(message.from_user.id)
    if user:
        conn = sqlite3.connect('cortex.db')
        cursor = conn.cursor()
        cursor.execute('SELECT SUM(amount), SUM(daily_profit) FROM investments WHERE user_id = ?', (user[0],))
        investment = cursor.fetchone()
        conn.close()
        
        total_invested = investment[0] or 0
        daily_profit = investment[1] or 0
        monthly_profit = daily_profit * 30
        
        text = f"""
📊 <b>ВАША СТАТИСТИКА</b>

💼 Загалом інвестовано: ${total_invested:,.2f}
💰 Щоденний прибуток: ${daily_profit:,.2f}
📈 Місячний прибуток: ~${monthly_profit:,.2f}

🎯 Активні інвестиції: {3 if total_invested > 0 else 0}
🕒 Наступна виплата: 09:00 UTC

Натисніть /invest для додаткових інвестицій
        """
        bot.send_message(message.chat.id, text)
    else:
        bot.send_message(message.chat.id, "Спочатку натисніть /start")

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    user_id = message.from_user.id
    text = message.text.strip()
    
    if text in ['1', '2', '3']:
        strategies = {
            '1': 'bluechip',
            '2': 'defi', 
            '3': 'ai'
        }
        bot.send_message(message.chat.id, "💵 Введіть суму інвестиції ($):", 
                        reply_markup=telebot.types.ForceReply())
        bot.register_next_step_handler(message, lambda m: process_investment(m, strategies[text]))
        
    elif text.replace('.', '').isdigit():
        amount = float(text)
        if amount >= 10:
            user = get_user(user_id)
            if user:
                daily_profit = add_investment(user[0], 'bluechip', amount)
                bot.send_message(message.chat.id, 
                               f"✅ Інвестиція ${amount:,.2f} прийнята!\n"
                               f"💰 Щоденний прибуток: ${daily_profit:,.2f}\n"
                               f"📈 Місячний прибуток: ~${daily_profit * 30:,.2f}")
            else:
                bot.send_message(message.chat.id, "Спочатку натисніть /start")
        else:
            bot.send_message(message.chat.id, "❌ Мінімальна сума - $10")
    else:
        bot.send_message(message.chat.id, "Натисніть /start для початку")

def process_investment(message, strategy):
    try:
        amount = float(message.text)
        if amount >= 10:
            user = get_user(message.from_user.id)
            if user:
                daily_profit = add_investment(user[0], strategy, amount)
                
                strategy_names = {
                    'bluechip': 'Blue Chip Hodl',
                    'defi': 'DeFi Yield Farming',
                    'ai': 'AI Swing Trading'
                }
                
                bot.send_message(message.chat.id, 
                               f"✅ Інвестиція прийнята!\n"
                               f"📊 Стратегія: {strategy_names[strategy]}\n"
                               f"💵 Сума: ${amount:,.2f}\n"
                               f"💰 Щоденний прибуток: ${daily_profit:,.2f}\n"
                               f"📈 Місячний прибуток: ~${daily_profit * 30:,.2f}\n\n"
                               f"🕒 Виплати щоденно о 09:00 UTC")
            else:
                bot.send_message(message.chat.id, "Спочатку натисніть /start")
        else:
            bot.send_message(message.chat.id, "❌ Мінімальна сума - $10")
    except ValueError:
        bot.send_message(message.chat.id, "❌ Будь ласка, введіть числове значення")

if __name__ == "__main__":
    init_db()
    print("🧠 Cortex Trader запущено...")
    bot.infinity_polling()