# main.py
import requests
import telebot
from flask import Flask, request
from datetime import datetime
import threading
import time
import logging
from handlers.py import *

# Налаштування логування
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# -------------------------
# Налаштування
# -------------------------
API_KEY_TELEGRAM = "8051222216:AAFORHEn1IjWllQyPp8W_1OY3gVxcBNVvZI"
CHAT_ID = "6053907025"
WEBHOOK_HOST = "https://troovy-detective-bot-1-4on4.onrender.com"
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = WEBHOOK_HOST + WEBHOOK_PATH

bot = telebot.TeleBot(API_KEY_TELEGRAM)
app = Flask(__name__)

last_signals = {}

# -------------------------
# Функції сигналів
# -------------------------
def send_signal_message(symbol, signal_data):
    """Надсилання сигналу в Telegram"""
    try:
        current_time = datetime.now()
        
        if symbol in last_signals:
            last_time = last_signals[symbol]
            if (current_time - last_time).total_seconds() < 3600:
                return False
        
        last_signals[symbol] = current_time
        
        if signal_data["type"] == "LONG":
            emoji = "🚀"
            title = "LONG breakout"
            desc = f"ціна пробила опір {signal_data['level']:.4f}"
        elif signal_data["type"] == "SHORT":
            emoji = "⚡" 
            title = "SHORT breakout"
            desc = f"ціна пробила підтримку {signal_data['level']:.4f}"
        else:
            emoji = "⚠️"
            title = "Pre-top detected"
            desc = f"можливий short біля {signal_data['level']:.4f}"
        
        msg = (
            f"{emoji} <b>{symbol}</b>\n"
            f"{title}: {desc}\n"
            f"📊 Ринкова: {signal_data.get('current_price', 0):.4f} | "
            f"Відрив: {signal_data['diff']:+.4f} ({signal_data['diff_pct']:+.2f}%)\n"
        )
        
        if signal_data["type"] == "PRE_TOP":
            msg += f"📈 Імпульс: {signal_data['impulse']*100:.1f}%\n"
        
        msg += f"⏰ {current_time.strftime('%H:%M:%S')}"
        
        bot.send_message(CHAT_ID, msg, parse_mode="HTML")
        logger.info(f"Надіслано сигнал для {symbol}: {signal_data['type']}")
        return True
        
    except Exception as e:
        logger.error(f"Помилка відправки сигналу {symbol}: {e}")
        return False

def analyze_symbol(symbol):
    """Аналіз монети на основі S/R та pre-top"""
    try:
        df_1h = get_klines(symbol, interval="1h", limit=200)
        df_4h = get_klines(symbol, interval="4h", limit=100)
        
        if not df_1h or not df_4h or len(df_1h["c"]) < 50:
            return None
        
        closes_1h = np.array(df_1h["c"], dtype=float)
        closes_4h = np.array(df_4h["c"], dtype=float)
        volumes_1h = np.array(df_1h["v"], dtype=float)
        
        sr_levels_1h = find_support_resistance(closes_1h, window=15, delta=0.005)
        sr_levels_4h = find_support_resistance(closes_4h, window=10, delta=0.005)
        
        all_sr_levels = sorted(set(sr_levels_1h + sr_levels_4h))
        last_price = closes_1h[-1]
        signals = []
        
        for lvl in all_sr_levels:
            diff = last_price - lvl
            diff_pct = (diff / lvl) * 100
            
            if last_price > lvl * 1.01 and abs(diff_pct) < 50:
                signals.append({
                    "type": "LONG",
                    "level": lvl,
                    "diff": diff,
                    "diff_pct": diff_pct,
                    "timeframe": "1h/4h"
                })
                break
            elif last_price < lvl * 0.99 and abs(diff_pct) < 50:
                signals.append({
                    "type": "SHORT", 
                    "level": lvl,
                    "diff": diff,
                    "diff_pct": diff_pct,
                    "timeframe": "1h/4h"
                })
                break
        
        impulse_4h = (closes_4h[-1] - closes_4h[-4]) / closes_4h[-4] if len(closes_4h) >= 4 else 0
        impulse_1h = (closes_1h[-1] - closes_1h[-6]) / closes_1h[-6] if len(closes_1h) >= 6 else 0
        
        vol_spike = volumes_1h[-1] > 2.0 * np.mean(volumes_1h[-20:]) if len(volumes_1h) >= 20 else False
        
        nearest_resistance = min([lvl for lvl in all_sr_levels if lvl > last_price], default=None)
        
        if nearest_resistance and (impulse_4h > 0.08 or impulse_1h > 0.05) and vol_spike:
            diff_to_res = nearest_resistance - last_price
            diff_pct_to_res = (diff_to_res / last_price) * 100
            
            if diff_pct_to_res < 10:
                signals.append({
                    "type": "PRE_TOP",
                    "level": nearest_resistance,
                    "diff": diff_to_res,
                    "diff_pct": diff_pct_to_res,
                    "impulse": max(impulse_4h, impulse_1h),
                    "timeframe": "1h/4h"
                })
        
        return signals if signals else None
        
    except Exception as e:
        logger.error(f"Помилка аналізу {symbol}: {e}")
        return None

# -------------------------
# Команди Telegram
# -------------------------
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    help_text = (
        "🤖 Smart Auto Breakout Bot\n\n"
        "🔹 Автоматичні сигнали:\n"
        "🚀 Breakout - пробій рівнів\n"
        "⚠️ Pre-top - перед вершиною\n\n"
        "🔹 Команди:\n"
        "/scan_now - ручне сканування\n"
        "/pump_candidates - топ памп кандидати\n"
        "/volume_heatmap SYMBOL - TVH аналіз\n"
        "/status - статус бота\n\n"
        "Сигнали надсилаються автоматично кожні 5 хвилин"
    )
    bot.send_message(message.chat.id, help_text)

@bot.message_handler(commands=['pump_candidates'])
def pump_candidates_handler(message):
    """Топ кандидати для пампу"""
    try:
        bot.send_message(message.chat.id, "🔍 Шукаю найкращі памп кандидати...")
        
        candidates = get_top_pump_candidates(limit=8)
        
        if not candidates:
            bot.send_message(message.chat.id, "ℹ️ Не знайдено хороших кандидатів для пампу")
            return
        
        response = "🔥 <b>Топ кандидати для пампу:</b>\n\n"
        
        for i, candidate in enumerate(candidates, 1):
            symbol = candidate["symbol"]
            confidence = candidate["confidence"] * 100
            price_change = candidate["price_change_1h"]
            volume_ratio = candidate["volume_ratio"]
            
            response += (
                f"{i}. <b>{symbol}</b>\n"
                f"   📊 Впевненість: {confidence:.1f}%\n"
                f"   📈 Зміна (1h): {price_change:+.2f}%\n"
                f"   🔊 Об'єм: x{volume_ratio:.1f}\n"
            )
            
            if candidate["breakout_level"]:
                response += f"   🚀 Пробив рівень: {candidate['breakout_level']:.4f}\n"
            
            response += "\n"
        
        bot.send_message(message.chat.id, response, parse_mode="HTML")
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Помилка: {e}")

@bot.message_handler(commands=['volume_heatmap'])
def volume_heatmap_handler(message):
    """TVH аналіз для монети"""
    try:
        args = message.text.split()
        if len(args) < 2:
            bot.send_message(message.chat.id, "ℹ️ Використання: /volume_heatmap SYMBOL")
            return
        
        symbol = args[1].upper()
        if not symbol.endswith('USDT'):
            symbol += 'USDT'
        
        bot.send_message(message.chat.id, f"📊 Аналізую TVH для {symbol}...")
        
        analysis = analyze_volume_heatmap(symbol)
        
        if not analysis:
            bot.send_message(message.chat.id, f"❌ Не вдалося проаналізувати {symbol}")
            return
        
        response = (
            f"📈 <b>Volume Heatmap для {symbol}</b>\n\n"
            f"💰 Поточна ціна: {analysis['current_price']:.4f}\n"
            f"📊 Зміна (24h): {analysis['price_change_24h']:+.2f}%\n"
            f"🔊 Об'єм: x{analysis['volume_ratio']:.1f} від середнього\n"
            f"   • Поточний: {analysis['current_volume']:.0f}\n"
            f"   • Середній: {analysis['avg_volume_20']:.0f}\n\n"
            f"🔥 <b>Найвищі Volume Nodes:</b>\n"
        )
        
        for price, volume in analysis['high_volume_nodes']:
            response += f"   • {price:.4f}: {volume:.0f}\n"
        
        bot.send_message(message.chat.id, response, parse_mode="HTML")
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Помилка: {e}")

@bot.message_handler(commands=['scan_now'])
def scan_now_handler(message):
    """Ручне сканування"""
    try:
        bot.send_message(message.chat.id, "🔍 Запускаю сканування...")
        
        url = "https://api.binance.com/api/v3/ticker/24hr"
        data = requests.get(url, timeout=10).json()
        
        usdt_pairs = [
            d for d in data 
            if d["symbol"].endswith("USDT") and float(d["quoteVolume"]) > 5_000_000
        ]
        
        sorted_symbols = sorted(
            usdt_pairs,
            key=lambda x: abs(float(x["priceChangePercent"])),
            reverse=True
        )
        
        top_symbols = [s["symbol"] for s in sorted_symbols[:15]]
        signals_found = 0
        results = []
        
        for symbol in top_symbols:
            signal_data = analyze_symbol(symbol)
            if signal_data:
                best_signal = max(signal_data, key=lambda x: abs(x["diff_pct"]))
                df = get_klines(symbol, interval="1h", limit=2)
                if df and len(df["c"]) > 0:
                    best_signal["current_price"] = df["c"][-1]
                
                if best_signal["type"] == "LONG":
                    emoji = "🚀"
                    desc = f"пробив опір {best_signal['level']:.4f}"
                elif best_signal["type"] == "SHORT":
                    emoji = "⚡"
                    desc = f"пробив підтримку {best_signal['level']:.4f}"
                else:
                    emoji = "⚠️"
                    desc = f"pre-top біля {best_signal['level']:.4f}"
                
                results.append(
                    f"{emoji} {symbol}: {desc} "
                    f"({best_signal['diff_pct']:+.1f}%)"
                )
                signals_found += 1
        
        if results:
            response = "🔍 Результати сканування:\n\n" + "\n".join(results)
            bot.send_message(message.chat.id, response)
        else:
            bot.send_message(message.chat.id, "ℹ️ Жодних сигналів не знайдено.")
            
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Помилка сканування: {e}")

@bot.message_handler(commands=['status'])
def status_handler(message):
    """Статус бота"""
    status_text = (
        "🤖 <b>Статус бота:</b>\n\n"
        f"✅ Активний\n"
        f"⏰ Останній сигнал: {len(last_signals)} монет\n"
        f"🔄 Перевірка кожні 5 хвилин\n"
        f"📊 Моніторинг: топ-30 активних монет\n\n"
        f"⚡ <b>Унікальні функції:</b>\n"
        f"• Breakout сигнали\n"
        f"• Pre-top детекція\n"
        f"• Volume Heatmap (TVH)\n"
        f"• Памп кандидати\n"
        f"• Multi-timeframe аналіз"
    )
    bot.send_message(message.chat.id, status_text, parse_mode="HTML")

# -------------------------
# Основна логіка
# -------------------------
def check_market():
    """Постійна перевірка ринку"""
    logger.info("Запуск Smart Auto перевірки ринку...")
    
    while True:
        try:
            url = "https://api.binance.com/api/v3/ticker/24hr"
            data = requests.get(url, timeout=10).json()
            
            usdt_pairs = [
                d for d in data 
                if d["symbol"].endswith("USDT") and float(d["quoteVolume"]) > 5_000_000
            ]
            
            sorted_symbols = sorted(
                usdt_pairs,
                key=lambda x: abs(float(x["priceChangePercent"])),
                reverse=True
            )
            
            top_symbols = [s["symbol"] for s in sorted_symbols[:30]]
            logger.info(f"Аналізуємо {len(top_symbols)} монет")
            
            signals_found = 0
            
            for symbol in top_symbols:
                try:
                    signal_data = analyze_symbol(symbol)
                    
                    if signal_data:
                        best_signal = max(signal_data, key=lambda x: abs(x["diff_pct"]))
                        
                        df = get_klines(symbol, interval="1h", limit=2)
                        if df and len(df["c"]) > 0:
                            best_signal["current_price"] = df["c"][-1]
                        
                        if send_signal_message(symbol, best_signal):
                            signals_found += 1
                            
                    time.sleep(0.5)
                    
                except Exception as e:
                    logger.error(f"Помилка обробки {symbol}: {e}")
                    continue
            
            logger.info(f"Знайдено {signals_found} сигналів. Очікування 5 хвилин...")
            time.sleep(300)
            
        except Exception as e:
            logger.error(f"Критична помилка: {e}")
            time.sleep(60)

# -------------------------
# Flask та Webhook
# -------------------------
@app.route(WEBHOOK_PATH, methods=['POST'])
def webhook():
    try:
        json_str = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
    except Exception as e:
        logger.error(f"Помилка webhook: {e}")
    return 'OK', 200

@app.route('/')
def home():
    return 'Smart Auto Bot is running!', 200

@app.route('/status')
def web_status():
    return {
        'status': 'active',
        'last_signals': len(last_signals),
        'timestamp': datetime.now().isoformat()
    }, 200

def setup_webhook():
    try:
        url = f'https://api.telegram.org/bot{API_KEY_TELEGRAM}/setWebhook'
        response = requests.post(url, data={'url': WEBHOOK_URL}, timeout=10)
        logger.info(f'Webhook setup: {response.json()}')
    except Exception as e:
        logger.error(f'Помилка налаштування webhook: {e}')

# -------------------------
# Запуск
# -------------------------
if __name__ == '__main__':
    logger.info('Запуск Smart Auto Bot...')
    
    setup_webhook()
    
    market_thread = threading.Thread(target=check_market, daemon=True)
    market_thread.start()
    logger.info('Потік перевірки ринку запущено')
    
    try:
        app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
    except Exception as e:
        logger.error(f'Помилка запуску Flask: {e}')