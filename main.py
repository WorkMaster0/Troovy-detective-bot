import requests
from flask import request
import os
import logging
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

# Завантаження модулів
from reality_shift import reality_shift_command
from quantum_arbitrage import quantum_arbitrage_command
from dark_pool import dark_pool_command
from quantum_security import QuantumSecuritySystem
from config import WHITELIST_USERS

# Налаштування логування
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class QuantumTradingBot:
    def __init__(self, token):
        self.token = token
        self.application = Application.builder().token(token).build()
        self.security_system = QuantumSecuritySystem()
        
    def setup_handlers(self):
        """Налаштування обробників команд"""
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("reality_shift", reality_shift_command))
        self.application.add_handler(CommandHandler("quantum_arbitrage", quantum_arbitrage_command))
        self.application.add_handler(CommandHandler("dark_pool", dark_pool_command))
        self.application.add_handler(CommandHandler("status", self.status))

    def verify_kraken_ip(self, client_ip: str) -> bool:
    """Перевірка, що запит від Kraken (для webhook)"""
    kraken_ips = [
        '52.89.214.238',
        '34.212.75.30', 
        '54.218.53.128',
        '52.32.178.7',
        '52.36.174.99'
    ]
    return client_ip in kraken_ips
        
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда старту"""
        user = update.effective_user
        
        if user.id not in WHITELIST_USERS:
            await update.message.reply_text("🚫 Доступ заборонено")
            return
            
        await update.message.reply_text(
            "🌌 Quantum Trading Bot HRT-9000\n\n"
            "Доступні команди:\n"
            "/reality_shift - Квантовий зсув реальності\n"
            "/quantum_arbitrage - Квантовий арбітраж\n"
            "/dark_pool - Темний пул\n"
            "/status - Статус системи"
        )
    
    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Статус системи"""
        status_data = await self.security_system.get_system_status()
        await update.message.reply_text(
            f"📊 Статус системи:\n"
            f"🔗 Підключення до бірж: {status_data['exchange_connections']}\n"
            f"⚡ Швидкість відгуку: {status_data['response_time']}ms\n"
            f"💎 Активні стратегії: {status_data['active_strategies']}\n"
            f"🌐 Квантовий рівень: {status_data['quantum_level']}"
        )
    
    async def run(self):
        """Запуск бота"""
        self.setup_handlers()
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling()
        
        logger.info("🤖 Quantum Trading Bot запущено!")
        
        # Запуск фонових задач
        asyncio.create_task(self._background_tasks())
        
        # Очікування завершення
        await self.application.stop()

async def main():
    """Головна функція"""
    load_dotenv()
    
    bot_token = os.getenv('BOT_TOKEN')
    if not bot_token:
        logger.error("BOT_TOKEN не знайдено!")
        return
    
    bot = QuantumTradingBot(bot_token)
    await bot.run()

if __name__ == '__main__':
    asyncio.run(main())