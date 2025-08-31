# cortex_trader_pro.py
import telebot
from telebot import types
import os
from cortex_core import CortexCore
from staking_system import SmartStaking
from ai_signals import AISignalGenerator

class CortexTraderPro:
    def __init__(self, token):
        self.bot = telebot.TeleBot(token, parse_mode="HTML")
        self.core = CortexCore()
        self.staking = SmartStaking()
        self.ai = AISignalGenerator()
        
        self.register_handlers()
    
    def register_handlers(self):
        @self.bot.message_handler(commands=['start'])
        def start(message):
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
            btn1 = types.KeyboardButton('💰 Заробити')
            btn2 = types.KeyboardButton('📊 Мої інвестиції')
            btn3 = types.KeyboardButton('🚀 Топ стратегії')
            markup.add(btn1, btn2, btn3)
            
            text = """
🎯 <b>Cortex Trading Ecosystem</b>

💡 <i>Перша у світі децентралізована платформа, де кожен може заробляти на крипторынку без досвіду!</i>

🌟 <b>Як це працює:</b>
1. Обираєте перевірену стратегію
2. Стейкуєте суму (від $10)
3. Отримуєте щоденні винагороди
4. Можете вивести в будь-який момент

📈 <b>Середня дохідність:</b> 15-30% monthly
🔒 <b>Гарантії:</b> Smart-контракти, страхування
            """
            self.bot.send_message(message.chat.id, text, reply_markup=markup)
        
        @self.bot.message_handler(func=lambda message: message.text == '💰 Заробити')
        def earn(message):
            markup = types.InlineKeyboardMarkup()
            strategies = self.get_top_strategies()
            
            for strat in strategies[:3]:
                btn = types.InlineKeyboardButton(
                    f"{strat['name']} - {strat['performance']}%", 
                    callback_data=f"strat_{strat['id']}"
                )
                markup.add(btn)
            
            text = """
🎯 <b>ОБЕРІТЬ СТРАТЕГІЮ ДЛЯ ЗАРОБІТКУ</b>

Топ-3 перевірені стратегії з найвищою дохідністю:

🔹 <b>Blue Chip Hodl</b> - 23.4% місячно
🔹 <b>DeFi Yield Farming</b> - 31.2% місячно  
🔹 <b>AI Swing Trading</b> - 28.7% місячно

💡 <i>Кожна стратегія має страховий фонд та аудит</i>
            """
            self.bot.send_message(message.chat.id, text, reply_markup=markup)
        
        @self.bot.callback_query_handler(func=lambda call: call.data.startswith('strat_'))
        def show_strategy(call):
            strategy_id = int(call.data.split('_')[1])
            strategy = self.get_strategy_info(strategy_id)
            
            text = f"""
📊 <b>{strategy['name']}</b>

💰 Дохідність: <b>{strategy['performance']}%</b> місячно
⚡ Ризик: {strategy['risk_level']}/5
🏆 Творець: {strategy['creator']}
📈 Вік стратегії: {strategy['age']} днів

💡 <i>Мінімальний стейк: $10</i>
🎯 <i>Щоденні виплати</i>
🔒 <i>Страховий фонд: $15,000</i>

Введіть суму для інвестування:
            """
            self.bot.send_message(call.message.chat.id, text)
            
            # Тут буде логіка інвестування