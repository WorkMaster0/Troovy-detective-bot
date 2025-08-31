# ai_brain.py
import random
from datetime import datetime

class AIBrain:
    def __init__(self):
        self.market_mood = "neutral"
        self.conversations = {}
        
    def analyze_market(self):
        """Аналіз ринку"""
        moods = {
            "bullish": "🚀 <b>Бичий настрій!</b>\nРинок готовий до зростання!",
            "bearish": "📉 <b>Ведмежий настрій!</b>\nБудьте обережні з позиціями.",
            "neutral": "⚖️ <b>Нейтральний настрій!</b>\nРинок невизначений."
        }
        
        # Тимчасово - випадковий настрій
        self.market_mood = random.choice(list(moods.keys()))
        return moods[self.market_mood]
    
    def start_discussion(self, user_id):
        """Початок діалогу"""
        questions = [
            "🧠 <b>Давайте обговоримо стратегію!</b>\nЯкий актив вас цікавить?",
            "🎯 <b>Готовий до дискусії!</b>\nПро яку монету хочете поговорити?",
            "💡 <b>Обговорюємо торгівлю!</b>\nЩо вас цікавить сьогодні?"
        ]
        
        self.conversations[user_id] = {
            'stage': 'awaiting_asset',
            'context': {}
        }
        
        return random.choice(questions)
    
    def continue_discussion(self, user_id, message):
        """Продовження діалогу"""
        if user_id not in self.conversations:
            return "❌ Давайте почнемо з /discuss"
        
        session = self.conversations[user_id]
        
        if session['stage'] == 'awaiting_asset':
            session['context']['asset'] = message.upper()
            session['stage'] = 'awaiting_strategy'
            
            responses = [
                f"📊 <b>{message.upper()}</b> - відмінний вибір!\nЯку стратегію розглядаєте?",
                f"🎯 <b>{message.upper()}</b>! Чудово!\nОпишіть ваш торговий план:",
                f"💎 <b>{message.upper()}</b> - цікавий актив!\nЯк хочете діяти?"
            ]
            return random.choice(responses)
        
        elif session['stage'] == 'awaiting_strategy':
            # Тут буде AI аналіз стратегії
            asset = session['context']['asset']
            
            feedbacks = [
                f"🤖 <b>Аналізую вашу стратегію для {asset}...</b>\nВиглядає перспективно!",
                f"🧠 <b>Ваш план для {asset}:</b>\nЦікавий підхід! Додам свої ідеї...",
                f"📈 <b>Стратегія {asset}:</b>\nМає потенціал! Рекомендую уважно стежити за ринком."
            ]
            
            # Завершуємо діалог
            del self.conversations[user_id]
            return random.choice(feedbacks)
    
    def process_message(self, message):
        """Обробка звичайних повідомлень"""
        responses = [
            "🧠 <b>CortexTrader</b> слухає!\nВикористовйте /analyze для аналізу ринку",
            "💡 <b>Готовий допомогти!</b>\nСпробуйте /discuss для обговорення стратегії",
            "🎯 <b>Чим можу допомогти?</b>\nКоманда /help покаже всі можливості"
        ]
        return random.choice(responses)