# ai_brain.py
import os
import openai
from datetime import datetime

class AIBrain:
    def __init__(self):
        self.market_mood = "neutral"
        self.conversations = {}
        openai.api_key = os.getenv("OPENAI_API_KEY")  # ключ з Render secrets

    def ai_answer(self, user_id, prompt):
        """Відповідь від OpenAI"""
        try:
            # Дістаємо історію діалогу
            history = self.conversations.get(user_id, {}).get("history", [])
            history.append({"role": "user", "content": prompt})

            response = openai.chat.completions.create(
                model="gpt-3.5-turbo",  # можеш замінити на "gpt-4o-mini"
                messages=history,
                max_tokens=500,
                temperature=0.7
            )

            answer = response.choices[0].message.content

            # Зберігаємо у контекст
            if user_id not in self.conversations:
                self.conversations[user_id] = {"history": []}
            self.conversations[user_id]["history"].append({"role": "assistant", "content": answer})

            return answer
        except Exception as e:
            return f"⚠️ Помилка AI: {e}"

    def analyze_market(self):
        """Тепер теж можна через AI"""
        return self.ai_answer("system", "Дай короткий аналіз ринку криптовалют у 2-3 реченнях.")

    def start_discussion(self, user_id):
        """Початок діалогу"""
        self.conversations[user_id] = {"history": [{"role": "system", "content": "Ти виступаєш як трейдінговий асистент."}]}
        return "🧠 Починаємо дискусію! Який актив вас цікавить?"

    def continue_discussion(self, user_id, message):
        """Продовження діалогу з AI"""
        return self.ai_answer(user_id, message)

    def process_message(self, message):
        """Відповідь на звичайні повідомлення"""
        return self.ai_answer("general", message)