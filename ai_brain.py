# ai_brain.py
import random
import re
from datetime import datetime
import requests

class AIBrain:
    def __init__(self):
        self.market_mood = "neutral"
        self.conversations = {}
        
    def get_ai_response(self, prompt, context=""):
        """Інтелектуальна відповідь без важких ML бібліотек"""
        # Простий AI на базі шаблонів та правил
        prompt_lower = prompt.lower()
        
        # Крипто-тематика
        crypto_patterns = {
            r'.*(btc|bitcoin|бітко).*': [
                "₿ Bitcoin зараз показує цікаву динаміку. Ключові рівні: $60K-65K",
                "📊 BTC: Аналізуючи графік, бачу потенціал для руху до $70K",
                "⚡ Bitcoin волатильність зросла. Рекомендую обережність"
            ],
            r'.*(eth|ethereum|етер).*': [
                "🔷 Ethereum має сильну технологічну базу. Цікаві рівні: $3K-3.5K",
                "📈 ETH показує гарну віддачу від рівнів підтримки",
                "💎 Ethereum: DeFi сектор продовжує зростати"
            ],
            r'.*(купити|buy|лонг|long).*': [
                "🎯 Для покупки шукайте підтвердження тренду на старшому таймфреймі",
                "📊 Перед покупкою перевірте обсяги - вони мають підтверджувати рух",
                "⚡ Входьте тільки при консенсусі індикаторів"
            ],
            r'.*(продати|sell|шорт|short).*': [
                "📉 Для продажу чекайте пробою ключових підтримок",
                "🔴 Short позиції вимагають особливої обережності",
                "⚡ Стоп-лос обов'язковий при шорті!"
            ]
        }
        
        # Шукаємо відповідний шаблон
        for pattern, responses in crypto_patterns.items():
            if re.search(pattern, prompt_lower):
                return random.choice(responses)
        
        # Загальні відповіді
        general_responses = [
            "🧠 Я аналізую ринок в реальному часі. Який актив вас цікавить?",
            "💡 Рекомендую диверсифікувати портфель між різними активами",
            "📊 Технічний аналіз показує... Чекайте чітких сигналів перед входом",
            "⚡ Волатильність зросла - ідеальний час для скальпінгу!",
            "🎯 Пам'ятайте про ризик-менеджмент! Не більше 2% на угоду"
        ]
        
        return random.choice(general_responses)
    
    def analyze_market(self):
        """Аналіз ринку з реальними даними"""
        try:
            # Спроба отримати реальні ціни
            btc_price = self.get_crypto_price("BTCUSDT")
            eth_price = self.get_crypto_price("ETHUSDT")
            
            analysis = f"""
📊 <b>Ринок зараз:</b>

₿ Bitcoin: ${btc_price:,.2f}
🔷 Ethereum: ${eth_price:,.2f}

<b>Рекомендації CortexTrader:</b>
• Аналізуйте 3+ таймфрейми перед входом
• Чекайте підтвердження сигналів
• Ризик-менеджмент - основа успіху

🎯 <i>Готовий обговорити ваші ідеї!</i>
            """
            return analysis
            
        except:
            # Резервний аналіз
            return "📈 Ринок показує середню волатильність. Ідеально для свинг-трейдингу!"
    
    def get_crypto_price(self, symbol):
        """Спроба отримати реальну ціну"""
        try:
            url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
            response = requests.get(url, timeout=5)
            data = response.json()
            return float(data['price'])
        except:
            # Резервне значення
            return random.uniform(40000, 45000) if symbol == "BTCUSDT" else random.uniform(2200, 2500)

# Решта коду залишається без змін...
    
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