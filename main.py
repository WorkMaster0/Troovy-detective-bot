import os
import logging
from telegram.ext import Application, CommandHandler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def start(update, context):
    await update.message.reply_text("🚀 Quantum Bot працює!")

async def status(update, context):
    await update.message.reply_text("📊 Статус: Активний")

def main():
    token = os.getenv('BOT_TOKEN')
    if not token:
        logger.error("BOT_TOKEN не знайдено!")
        return
    
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    
    logger.info("Бот запускається...")
    app.run_polling()

if __name__ == '__main__':
    main()