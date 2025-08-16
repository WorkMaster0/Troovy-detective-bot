from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = "ТВІЙ_ТЕЛЕГРАМ_ТОКЕН"  # Заміни на реальний токен

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привіт! Я Troovy Detective Bot. Напиши /scan_new_tokens для аналізу.")

async def scan_new_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Сканую нові токени... (функція в розробці)")

def main():
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("scan_new_tokens", scan_new_tokens))
    application.run_polling()

if __name__ == "__main__":
    main()