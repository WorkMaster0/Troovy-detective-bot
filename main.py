import os
import io
import logging
import requests
import pandas as pd
import mplfinance as mpf
from flask import Flask

# === Logging ===
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# === Flask App ===
app = Flask(__name__)

@app.route("/")
def index():
    return "Bot is running!"

# === Telegram ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

def send_telegram(message, photo=None):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("Telegram not configured (TELEGRAM_TOKEN / CHAT_ID missing)")
        return

    try:
        if photo:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
            files = {"photo": ("chart.png", photo, "image/png")}
            data = {"chat_id": CHAT_ID, "caption": message}
            r = requests.post(url, data=data, files=files)
        else:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            data = {"chat_id": CHAT_ID, "text": message}
            r = requests.post(url, data=data)

        if r.status_code != 200:
            logger.error("Telegram send failed: %s", r.text)
        else:
            logger.info("Telegram message sent ✅")
    except Exception as e:
        logger.error("Telegram send error: %s", e)

# === Test Chart ===
def make_test_chart():
    df = pd.DataFrame({
        "Open": [100, 102, 104, 103, 105],
        "High": [102, 104, 106, 105, 107],
        "Low":  [99, 101, 103, 102, 104],
        "Close":[101, 103, 105, 104, 106],
    }, index=pd.date_range("2025-09-01", periods=5, freq="T"))

    buf = io.BytesIO()
    mpf.plot(df, type="candle", style="charles", savefig=buf)
    buf.seek(0)
    return buf

# === Startup ===
if __name__ == "__main__":
    logger.info("Bot started ✅")
    # Надсилаємо тестове повідомлення з графіком
    chart = make_test_chart()
    send_telegram("🚀 Тестове повідомлення: бот запущений і працює!", photo=chart)

    # Flask сервер
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))