# ai_brain.py
import random
import openai
import os
from datetime import datetime

class AIBrain:
    def __init__(self):
        self.market_mood = "neutral"
        self.conversations = {}
        # Ініціалізація OpenAI
        openai.api_key = os.getenv('OPENAI_API_KEY')
        
    def get_ai_response(self, prompt, context=""):
        """Отримання відповіді від GPT-4"""
        try:
            response = openai.chat.completions.create(
                model="gpt-4",
                messages=[
                    {
                        "role": "system", 
                        "content": f"""Ти CortexTrader - AI трейдінговий помічник. 
                        Ти експерт з криптовалют, технічного аналізу та ринкових тенденцій.
                        Будь професійним, але дружнім. Давай корисні поради.
                        Контекст: {context}"""
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                max_tokens=500,
                temperature=0.7
            )
            return response.choices[0].message.content
        except Exception as e:
            return f"🤖 Помилка AI: {str(e)}"
    
    def analyze_market(self):
        """AI аналіз ринку з реальними даними"""
        prompt = """
        Проаналізуй поточний стан криптовалютного ринку. 
        Оціни настрій (бичий/ведмежий/нейтральний), ключові рівні, 
        потенційні можливості та ризики. Будь конкретним.
        """
        
        return self.get_ai_response(prompt, "market_analysis")
    
    def start_discussion(self, user_id):
        """Початок діалогу з AI"""
        prompt = """
        Ти - AI трейдінговий помічник CortexTrader. 
        Почати діалог з трейдером про торгові стратегії.
        Запитай про активи які цікавлять, стратегії чи питання.
        Будь професійним та заохочуй до діалогу.
        """
        
        self.conversations[user_id] = {
            'stage': 'awaiting_topic',
            'context': {},
            'history': []
        }
        
        response = self.get_ai_response(prompt)
        return response
    
    def continue_discussion(self, user_id, message):
        """Продовження діалогу з AI"""
        if user_id not in self.conversations:
            return self.start_discussion(user_id)
        
        session = self.conversations[user_id]
        session['history'].append(f"User: {message}")
        
        # Формуємо контекст для AI
        context = f"""
        Історія діалогу: {' | '.join(session['history'][-5:])}
        Поточний етап: {session['stage']}
        Контекст: {session['context']}
        """
        
        prompt = f"""
        Продовж діалог з трейдером. Його останнє повідомлення: "{message}"
        Будь корисним трейдінговим помічником. Аналізуй, ради, обговорюй.
        """
        
        response = self.get_ai_response(prompt, context)
        session['history'].append(f"AI: {response}")
        
        return response
    
    def process_message(self, message):
        """Обробка звичайних повідомлень з AI"""
        prompt = f"""
        Користувач написав: "{message}"
        Ти - CortexTrader, AI трейдінговий помічник. 
        Відповідь корисно та по ділу. Запропонуй свої послуги.
        """
        
        return self.get_ai_response(prompt)

    def get_market_insight(self, asset):
        """Отримання інсайтів по конкретному активу"""
        prompt = f"""
        Проаналізуй криптовалюту {asset}. 
        Технічний аналіз, фундаментальні фактори, 
        потенційні точки входу/виходу, ризики.
        Будь конкретним та професійним.
        """
        
        return self.get_ai_response(prompt, f"analysis_{asset}")