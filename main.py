import os
import logging
from telegram.ext import Application, CommandHandler
from quantum_shadow import setup_shadow_handlers

# Налаштування логування
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

async def start(update, context):
    """Команда старту"""
    await update.message.reply_text(
        "🌌 Quantum Shadow Protocol QSP-9000\n\n"
        "Доступні команди:\n"
        "/start - Ця довідка\n" 
        "/shadow - Запуск тіньової операції\n"
        "/shadow_status - Статус мережі\n\n"
        "⚡ Протокол готовий до роботи"
    )

def main():
    """Головна функція"""
    token = os.getenv('BOT_TOKEN')
    if not token:
        logger.error("❌ BOT_TOKEN не знайдено!")
        return
    
    # Створення додатку
    application = Application.builder().token(token).build()
    
    # Додавання обробників
    application.add_handler(CommandHandler("start", start))
    setup_shadow_handlers(application)
    
    # Запуск бота
    logger.info("🚀 Бот запускається...")
    application.run_polling()

if __name__ == '__main__':
    main()