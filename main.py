import pandas as pd
import numpy as np
import requests
import time
from datetime import datetime, timedelta
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.ensemble import IsolationForest
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import asyncio

class AdvancedPumpDetectBot:
    def __init__(self, token):
        self.token = token
        self.app = Application.builder().token(token).build()
        self.thresholds = {
            'volume_ratio': 5.0,
            'price_change': 15.0,
            'market_cap': 1000000,
            'liquidity': 50000
        }
        self.coin_blacklist = set()
        
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = [
            [InlineKeyboardButton("🔍 Сканувати зараз", callback_data="scan_now")],
            [InlineKeyboardButton("⚙️ Налаштування", callback_data="settings")],
            [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
            [InlineKeyboardButton("🚫 Чорний список", callback_data="blacklist")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "🤖 Advanced Pump Detect Bot\n\n"
            "Можливості:\n"
            "• Раннє виявлення пампів\n"
            "• Аналіз об'єму та ліквідності\n"
            "• Machine Learning детекція аномалій\n"
            "• Чорний список монет\n"
            "• Smart alerts система",
            reply_markup=reply_markup
        )

    async def get_market_data(self, symbol='BTC'):
        try:
            url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}USDT"
            response = requests.get(url)
            data = response.json()
            
            klines_url = f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval=5m&limit=100"
            klines_response = requests.get(klines_url)
            klines_data = klines_response.json()
            
            return {
                'symbol': symbol,
                'price': float(data['lastPrice']),
                'volume': float(data['volume']),
                'price_change': float(data['priceChangePercent']),
                'high': float(data['highPrice']),
                'low': float(data['lowPrice']),
                'klines': klines_data
            }
        except:
            return None

    async def detect_anomalies(self, price_data):
        prices = np.array([float(x[4]) for x in price_data]).reshape(-1, 1)
        
        model = IsolationForest(contamination=0.1, random_state=42)
        predictions = model.fit_predict(prices)
        
        anomalies = prices[predictions == -1]
        return len(anomalies) > 0

    async def calculate_metrics(self, market_data):
        current_volume = market_data['volume']
        avg_volume = current_volume / 24
        
        klines = market_data['klines']
        if len(klines) > 20:
            recent_prices = [float(x[4]) for x in klines[-20:]]
            old_prices = [float(x[4]) for x in klines[:20]]
            
            price_change = ((recent_prices[-1] - old_prices[0]) / old_prices[0]) * 100
            volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0
            
            has_anomaly = await self.detect_anomalies(klines)
            
            return {
                'volume_ratio': volume_ratio,
                'price_change': price_change,
                'has_anomaly': has_anomaly,
                'liquidity_score': current_volume * market_data['price']
            }
        return None

    async def scan_top_coins(self):
        try:
            response = requests.get("https://api.binance.com/api/v3/ticker/24hr")
            all_data = response.json()
            
            usdt_pairs = [x for x in all_data if x['symbol'].endswith('USDT')]
            sorted_by_volume = sorted(usdt_pairs, 
                                    key=lambda x: float(x['volume']), 
                                    reverse=True)[:50]
            
            results = []
            for coin in sorted_by_volume:
                symbol = coin['symbol'].replace('USDT', '')
                if symbol in self.coin_blacklist:
                    continue
                    
                market_data = await self.get_market_data(symbol)
                if market_data:
                    metrics = await self.calculate_metrics(market_data)
                    if metrics:
                        score = self.calculate_risk_score(metrics)
                        if score > 0.7:
                            results.append({
                                'symbol': symbol,
                                'score': score,
                                'metrics': metrics,
                                'market_data': market_data
                            })
            
            return sorted(results, key=lambda x: x['score'], reverse=True)
        except Exception as e:
            print(f"Scan error: {e}")
            return []

    def calculate_risk_score(self, metrics):
        volume_score = min(metrics['volume_ratio'] / 10, 1.0)
        price_score = min(metrics['price_change'] / 30, 1.0)
        anomaly_score = 1.0 if metrics['has_anomaly'] else 0.0
        
        weights = {
            'volume': 0.4,
            'price': 0.3,
            'anomaly': 0.3
        }
        
        return (volume_score * weights['volume'] + 
                price_score * weights['price'] + 
                anomaly_score * weights['anomaly'])

    async def send_alert(self, context: ContextTypes.DEFAULT_TYPE, coin_data):
        symbol = coin_data['symbol']
        metrics = coin_data['metrics']
        
        message = f"🚨 ВИЯВЛЕНО ПОТЕНЦІЙНИЙ ПАМП\n\n"
        message += f"Монета: {symbol}\n"
        message += f"Ризик: {coin_data['score']:.2%}\n"
        message += f"Зміна ціни: {metrics['price_change']:.2f}%\n"
        message += f"Об'єм: x{metrics['volume_ratio']:.1f}\n"
        message += f"Аномалії: {'Так' if metrics['has_anomaly'] else 'Ні'}\n\n"
        message += f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        
        await context.bot.send_message(
            chat_id=context.job.chat_id,
            text=message,
            parse_mode='HTML'
        )

    async def scan_job(self, context: ContextTypes.DEFAULT_TYPE):
        results = await self.scan_top_coins()
        for coin in results[:3]:
            await self.send_alert(context, coin)

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        if query.data == "scan_now":
            await query.edit_message_text("🔍 Сканую топ монети...")
            results = await self.scan_top_coins()
            
            if results:
                message = "📊 Результати сканування:\n\n"
                for i, coin in enumerate(results[:5], 1):
                    message += f"{i}. {coin['symbol']} - {coin['score']:.2%}\n"
            else:
                message = "ℹ️ Потенційних пампів не знайдено"
                
            await query.edit_message_text(message)
            
        elif query.data == "settings":
            await query.edit_message_text("⚙️ Налаштування порогів:\n"
                                        f"Volume Ratio: {self.thresholds['volume_ratio']}\n"
                                        f"Price Change: {self.thresholds['price_change']}%")

    def run(self):
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CallbackQueryHandler(self.button_handler))
        
        print("Бот запущений...")
        self.app.run_polling()

# Використання
if __name__ == "__main__":
    TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
    bot = AdvancedPumpDetectBot(TOKEN)
    bot.run()