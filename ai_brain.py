# ai_brain.py
import random
import re
from datetime import datetime
from transformers import pipeline, AutoTokenizer, AutoModelForCausalLM
import torch

class AIBrain:
    def __init__(self):
        self.market_mood = "neutral"
        self.conversations = {}
        self.chatbot = None
        self.setup_ai()
        
    def setup_ai(self):
        """Ініціалізація безкоштовної AI моделі"""
        try:
            # Використовуємо легку модель для чату
            self.chatbot = pipeline(
                "text-generation",
                model="microsoft/DialoGPT-medium",
                tokenizer="microsoft/DialoGPT-medium",
                torch_dtype=torch.float16,
                device_map="auto" if torch.cuda.is_available() else None
            )
            print("✅ AI модель завантажена!")
        except Exception as e:
            print(f"❌ Помилка завантаження AI: {e}")
            self.chatbot = None
    
    def get_ai_response(self, prompt, context=""):
        """Отримання відповіді від безкоштовної AI"""
        if self.chatbot is None:
            return self.get_fallback_response(prompt)
        
        try:
            # Формуємо повний промпт
            full_prompt = f"""
            Ти CortexTrader - AI помічник для трейдингу. 
            Контекст: {context}
            
            Користувач: {prompt}
            CortexTrader:
            """
            
            # Генеруємо відповідь
            response = self.chatbot(
                full_prompt,
                max_length=200,
                num_return_sequences=1,
                temperature=0.8,
                do_sample=True
            )
            
            # Очищаємо відповідь
            ai_text = response[0]['generated_text']
            ai_text = ai_text.replace(full_prompt, "").strip()
            ai_text = re.split(r'[.!?]', ai_text)[0] + "."
            
            return ai_text
            
        except Exception as e:
            return self.get_fallback_response(prompt)
    
    def get_fallback_response(self, prompt):
        """Резервні відповіді без AI"""
        # Аналізуємо запит і даємо відповідну відповідь
        prompt_lower = prompt.lower()
        
        # Визначаємо тему запиту
        if any(word in prompt_lower for word in ['btc', 'bitcoin', 'бітко']):
            return "₿ Bitcoin показує сильну волатильність. Дивіться за рівнем $42K - це ключова зона!"
        
        elif any(word in prompt_lower for word in ['eth', 'ethereum', 'етер']):
            return "🔷 Ethereum має хорошу ліквідність. Слідкуйте за рівнем $2,200 - це важливий опір."
        
        elif any(word in prompt_lower for word in ['купити', 'buy', 'лонг']):
            return "🎯 Для покупки шукайте підтвердження тренду та volume spike. Ризик-менеджмент обов'язковий!"
        
        elif any(word in prompt_lower for word in ['продати', 'sell', 'шорт']):
            return "📉 Для продажу чекайте пробою підтримок або формування дивергенції. Стоп-лос обов'язковий!"
        
        elif any(word in prompt_lower for word in ['ринок', 'market', 'тренд']):
            return "📊 Поточний ринок нестабільний. Рекомендую торгувати обережно та з меншими об'ємами."
        
        else:
            # Генеруємо загальну відповідь
            responses = [
                "🧠 Як AI трейдінговий помічник, рекомендую аналізувати множинні таймфрейми перед входом у позицію.",
                "💡 Пам'ятайте про ризик-менеджмент! Не ризикуйте більше ніж 2% від депозиту на одну угоду.",
                "📈 Для успішного трейдингу важливо мати чіткий план та дотримуватися його.",
                "⚡ Волатильність на ринку висока. Рекомендую чекати ясних сигналів перед входом.",
                "🎯 Звертайте увагу на обсяги - вони часто підтверджують рух ціни."
            ]
            return random.choice(responses)
    
    def analyze_market(self):
        """Аналіз ринку з технічними даними"""
        # Отримуємо реальні дані (спрощено)
        btc_price = self.get_crypto_price("BTCUSDT")
        eth_price = self.get_crypto_price("ETHUSDT")
        
        analysis = f"""
📊 <b>Аналіз ринку:</b>

₿ Bitcoin: ${btc_price:,.2f}
🔷 Ethereum: ${eth_price:,.2f}

<b>Рекомендації:</b>
• Слідкуйте за ключовими рівнями підтримки/опору
• Аналізуйте обсяги для підтвердження рухів
• Використовуйте стоп-лоси для управління ризиками

🎯 <i>Готовий обговорити конкретні стратегії!</i>
        """
        
        return analysis
    
    def get_crypto_price(self, symbol):
        """Отримання ціни криптовалюти (спрощено)"""
        # Тут буде реальний API вызов, поки імітуємо
        prices = {
            "BTCUSDT": random.uniform(40000, 45000),
            "ETHUSDT": random.uniform(2200, 2500),
            "SOLUSDT": random.uniform(90, 120)
        }
        return prices.get(symbol, 0)
    
    def start_discussion(self, user_id):
        """Початок діалогу"""
        self.conversations[user_id] = {
            'stage': 'awaiting_topic',
            'context': {}
        }
        
        questions = [
            "🧠 <b>Давайте обговоримо торгові стратегії!</b>\nЯкий актив вас цікавить найбільше?",
            "🎯 <b>Готовий до діалогу про трейдинг!</b>\nРозкажіть про ваш торговий підхід?",
            "💡 <b>Обговорюємо ринок разом!</b>\nЯкі монети в вашому портфелі?"
        ]
        
        return random.choice(questions)
    
    def continue_discussion(self, user_id, message):
        """Продовження діалогу"""
        if user_id not in self.conversations:
            return self.start_discussion(user_id)
        
        # Використовуємо AI для відповіді
        response = self.get_ai_response(
            message, 
            context="обговорення трейдингових стратегій"
        )
        
        return response

# Додайте ці функції в cortex_trader.py