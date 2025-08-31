# cortex_trader.py
import telebot
import requests
import numpy as np
from datetime import datetime
import json
import os
import time
from ai_brain import AIBrain  # Наш AI модуль

class CortexTrader:
    def __init__(self, token):
        self.bot = telebot.TeleBot(token, parse_mode="HTML")
        self.brain = AIBrain()
        self.user_sessions = {}
        
        # Реєструємо обробники
        self.register_handlers()
    
    def register_handlers(self):
        """Реєстрація команд бота"""
        
        @self.bot.message_handler(commands=['start', 'cortex'])
        def welcome(message):
            welcome_text = """
            🧠 <b>CortexTrader</b> - ваш AI трейдінговий помічник!
            
            <b>Доступні команди:</b>
            /analyze - Аналіз ринку
            /discuss - Обговорення стратегії
            /mood - Настрій ринку
            /help - Допомога
            
            <i>Я готовий до роботи! 🚀</i>
            """
            self.bot.reply_to(message, welcome_text)
        
        @self.bot.message_handler(commands=['analyze'])
        def analyze_market(message):
            """Аналіз поточного ринку"""
            analysis = self.brain.analyze_market()
            self.bot.reply_to(message, analysis)
        
        @self.bot.message_handler(commands=['discuss'])
        def discuss_strategy(message):
            """Обговорення торгової стратегії"""
            user_id = message.from_user.id
            
            # Початок діалогу
            response = self.brain.start_discussion(user_id)
            self.bot.reply_to(message, response)
            
            # Зберігаємо сесію
            self.user_sessions[user_id] = {
                'in_discussion': True,
                'last_message': datetime.now()
            }
        
        @self.bot.message_handler(func=lambda message: True)
        def handle_all_messages(message):
            """Обробка всіх повідомлень"""
            user_id = message.from_user.id
            
            if user_id in self.user_sessions and self.user_sessions[user_id].get('in_discussion'):
                # Продовжуємо діалог
                response = self.brain.continue_discussion(user_id, message.text)
                self.bot.reply_to(message, response)
            else:
                # Звичайна відповідь
                response = self.brain.get_ai_response(message.text)
                self.bot.reply_to(message, response)

    def run(self):
        """Запуск бота з обробкою помилок"""
        print("🧠 CortexTrader запускається...")
        while True:
            try:
                self.bot.infinity_polling()
            except Exception as e:
                print(f"Помилка: {e}. Перезапуск через 5 секунд...")
                time.sleep(5)

# Запуск бота
if __name__ == "__main__":
    TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_TOKEN_HERE')
    if TOKEN == 'YOUR_TOKEN_HERE':
        print("Помилка: Встановіть TELEGRAM_TOKEN у змінні оточення")
    else:
        trader_bot = CortexTrader(TOKEN)
        trader_bot.run()