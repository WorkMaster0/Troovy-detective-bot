# main.py
import requests
import telebot
from flask import Flask, request
from datetime import datetime
import threading
import time
import logging
from handlers import *

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

def check_golden_crosses():
    """Автоматична перевірка золотих хрестів"""
    try:
        crosses = find_golden_crosses()
        
        if not crosses:
            return
        
        # Групуємо по типах
        golden = [c for c in crosses if c["type"] == "GOLDEN"]
        death = [c for c in crosses if c["type"] == "DEATH"]
        
        # Надсилаємо сповіщення тільки про сильні сигнали
        strong_signals = [c for c in crosses if c["crossover_strength"] > 0.5]
        
        for signal in strong_signals:
            emoji = "🟢" if signal["type"] == "GOLDEN" else "🔴"
            msg = (
                f"{emoji} <b>{signal['symbol']}</b>\n"
                f"{'Золотий' if signal['type'] == 'GOLDEN' else 'Смертельний'} хрест\n"
                f"💰 Ціна: {signal['price']:.4f}\n"
                f"📈 EMA20: {signal['ema20']:.4f}\n"
                f"📉 EMA50: {signal['ema50']:.4f}\n"
                f"⚡ Сила: {signal['crossover_strength']:.2f}%\n"
                f"⏰ {datetime.now().strftime('%H:%M:%S')}"
            )
            
            # Перевіряємо, чи не надсилали вже сигнал для цієї монети
            if signal['symbol'] not in last_signals or \
               (datetime.now() - last_signals[signal['symbol']]).total_seconds() > 3600:
                
                bot.send_message(CHAT_ID, msg, parse_mode="HTML")
                last_signals[signal['symbol']] = datetime.now()
                logger.info(f"Надіслано сигнал хреста: {signal['symbol']} {signal['type']}")
                
    except Exception as e:
        logger.error(f"Помилка перевірки хрестів: {e}")

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

# ==================== НОВІ КОМАНДИ ====================
@bot.message_handler(commands=['whale_activity'])
def whale_activity_handler(message):
    """Детекція китівської активності"""
    try:
        bot.send_message(message.chat.id, "🐋 Шукаю китівську активність...")
        
        url = "https://api.binance.com/api/v3/ticker/24hr"
        data = requests.get(url, timeout=10).json()
        
        usdt_pairs = [
            d for d in data 
            if d["symbol"].endswith("USDT") and float(d["quoteVolume"]) > 8_000_000
        ]
        
        top_symbols = [s["symbol"] for s in sorted(usdt_pairs, 
                     key=lambda x: float(x["priceChangePercent"]), 
                     reverse=True)[:15]]
        
        whale_signals = []
        for symbol in top_symbols:
            activity = detect_whale_activity(symbol)
            if activity and activity["whale_detected"]:
                whale_signals.append(activity)
            time.sleep(0.3)
        
        if not whale_signals:
            bot.send_message(message.chat.id, "ℹ️ Китівська активність не виявлена")
            return
        
        response = "🐋 <b>Китівська активність виявлена:</b>\n\n"
        for i, signal in enumerate(whale_signals[:5], 1):
            response += (
                f"{i}. <b>{signal['symbol']}</b>\n"
                f"   💰 Ціна: {signal['price']:.4f}\n"
                f"   🔊 Об'єм: x{signal['volume_ratio']:.1f}\n"
                f"   📊 Z-score: {signal['z_score']:.2f}\n\n"
            )
        
        bot.send_message(message.chat.id, response, parse_mode="HTML")
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Помилка: {e}")

@bot.message_handler(commands=['liquidity_zones'])
def liquidity_zones_handler(message):
    """Аналіз ліквідних зон"""
    try:
        args = message.text.split()
        if len(args) < 2:
            bot.send_message(message.chat.id, "ℹ️ Використання: /liquidity_zones SYMBOL")
            return
        
        symbol = args[1].upper()
        if not symbol.endswith('USDT'):
            symbol += 'USDT'
        
        bot.send_message(message.chat.id, f"💧 Аналізую ліквідні зони для {symbol}...")
        
        analysis = calculate_liquidity_zones(symbol)
        
        if not analysis:
            bot.send_message(message.chat.id, f"❌ Не вдалося проаналізувати {symbol}")
            return
        
        response = (
            f"💧 <b>Ліквідні зони для {symbol}</b>\n"
            f"💰 Поточна ціна: {analysis['current_price']:.4f}\n\n"
            f"🔥 <b>Топ ліквідні зони:</b>\n"
        )
        
        for i, zone in enumerate(analysis['liquidity_zones'][:3], 1):
            response += (
                f"{i}. Зона: {zone['center']:.4f}\n"
                f"   📊 Об'єм: {zone['total_volume']:.0f}\n"
                f"   📈 Діапазон: {zone['min_price']:.4f} - {zone['max_price']:.4f}\n"
                f"   🎯 Щільність: {zone['density']} свічок\n\n"
            )
        
        bot.send_message(message.chat.id, response, parse_mode="HTML")
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Помилка: {e}")

@bot.message_handler(commands=['volatility_prediction'])
def volatility_prediction_handler(message):
    """Предикція волатильності"""
    try:
        bot.send_message(message.chat.id, "📊 Аналізую волатильність...")
        
        url = "https://api.binance.com/api/v3/ticker/24hr"
        data = requests.get(url, timeout=10).json()
        
        usdt_pairs = [
            d for d in data 
            if d["symbol"].endswith("USDT") and float(d["quoteVolume"]) > 5_000_000
        ]
        
        top_symbols = [s["symbol"] for s in sorted(usdt_pairs,
                     key=lambda x: abs(float(x["priceChangePercent"])),
                     reverse=True)[:10]]
        
        volatility_signals = []
        for symbol in top_symbols:
            prediction = predict_volatility_spikes(symbol)
            if prediction and prediction["volatility_spike_predicted"]:
                volatility_signals.append(prediction)
            time.sleep(0.3)
        
        if not volatility_signals:
            bot.send_message(message.chat.id, "ℹ️ Сплески волатильності не прогнозуються")
            return
        
        response = "⚡ <b>Прогноз сплесків волатильності:</b>\n\n"
        for i, signal in enumerate(volatility_signals[:5], 1):
            response += (
                f"{i}. <b>{signal['symbol']}</b>\n"
                f"   💰 Ціна: {signal['price']:.4f}\n"
                f"   📊 Волатильність: {signal['current_volatility']*100:.2f}%\n"
                f"   📈 Відношення: x{signal['volatility_ratio']:.2f}\n\n"
            )
        
        bot.send_message(message.chat.id, response, parse_mode="HTML")
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Помилка: {e}")

@bot.message_handler(commands=['market_manipulation'])
def market_manipulation_handler(message):
    """Детекція маніпуляцій ринком"""
    try:
        bot.send_message(message.chat.id, "🔍 Шукаю ознаки маніпуляцій...")
        
        url = "https://api.binance.com/api/v3/ticker/24hr"
        data = requests.get(url, timeout=10).json()
        
        usdt_pairs = [
            d for d in data 
            if d["symbol"].endswith("USDT") and float(d["quoteVolume"]) > 3_000_000 and
            abs(float(d["priceChangePercent"])) > 8.0
        ]
        
        top_symbols = [s["symbol"] for s in usdt_pairs[:8]]
        
        manipulation_signals = []
        for symbol in top_symbols:
            analysis = detect_market_manipulation(symbol)
            if analysis and analysis["manipulation_detected"]:
                manipulation_signals.append(analysis)
            time.sleep(0.3)
        
        if not manipulation_signals:
            bot.send_message(message.chat.id, "✅ Ознак маніпуляцій не виявлено")
            return
        
        response = "⚠️ <b>Виявлено можливі маніпуляції:</b>\n\n"
        for i, signal in enumerate(manipulation_signals, 1):
            response += (
                f"{i}. <b>{signal['symbol']}</b>\n"
                f"   🎯 Score: {signal['manipulation_score']}/4\n"
                f"   📊 Кореляція: {signal['correlation']:.2f}\n"
                f"   📈 Body ratio: {signal['avg_body_ratio']:.2f}\n\n"
            )
        
        response += "🔒 <i>Будьте обережні з цими активами</i>"
        bot.send_message(message.chat.id, response, parse_mode="HTML")
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Помилка: {e}")

@bot.message_handler(commands=['golden_crosses'])
def golden_crosses_handler(message):
    """Пошук золотих/смертельних хрестів"""
    try:
        bot.send_message(message.chat.id, "📈 Шукаю хрести...")
        
        crosses = find_golden_crosses()
        
        if not crosses:
            bot.send_message(message.chat.id, "ℹ️ Хрестів не знайдено")
            return
        
        response = "📊 <b>Знайдені хрести:</b>\n\n"
        
        golden_crosses = [c for c in crosses if c["type"] == "GOLDEN"]
        death_crosses = [c for c in crosses if c["type"] == "DEATH"]
        
        if golden_crosses:
            response += "🟢 <b>Золоті хрести:</b>\n"
            for cross in golden_crosses[:3]:
                response += (
                    f"• {cross['symbol']} - {cross['crossover_strength']:.2f}%\n"
                    f"  EMA20: {cross['ema20']:.4f}, EMA50: {cross['ema50']:.4f}\n"
                )
            response += "\n"
        
        if death_crosses:
            response += "🔴 <b>Смертельні хрести:</b>\n"
            for cross in death_crosses[:3]:
                response += (
                    f"• {cross['symbol']} - {cross['crossover_strength']:.2f}%\n"
                    f"  EMA20: {cross['ema20']:.4f}, EMA50: {cross['ema50']:.4f}\n"
                )
        
        bot.send_message(message.chat.id, response, parse_mode="HTML")
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Помилка: {e}")

@bot.message_handler(commands=['smart_money'])
def smart_money_handler(message):
    """Індикатори Smart Money"""
    try:
        args = message.text.split()
        if len(args) < 2:
            bot.send_message(message.chat.id, "ℹ️ Використання: /smart_money SYMBOL")
            return
        
        symbol = args[1].upper()
        if not symbol.endswith('USDT'):
            symbol += 'USDT'
        
        bot.send_message(message.chat.id, f"🧠 Аналізую Smart Money для {symbol}...")
        
        analysis = get_smart_money_indicators(symbol)
        
        if not analysis:
            bot.send_message(message.chat.id, f"❌ Не вдалося проаналізувати {symbol}")
            return
        
        # Емодзі для дивергенції
        divergence_emoji = {
            "BULLISH": "🟢",
            "BEARISH": "🔴", 
            "HIDDEN_BULLISH": "🟡",
            "HIDDEN_BEARISH": "🟠",
            "NEUTRAL": "⚪"
        }
        
        response = (
            f"🧠 <b>Smart Money аналіз для {symbol}</b>\n\n"
            f"💰 Ціна: {analysis['current_price']:.4f}\n"
            f"📊 Volume Delta: {analysis['volume_delta']:+.3f}\n"
            f"📈 Buy Pressure: {analysis['buy_pressure']*100:.1f}%\n"
            f"🎯 Дивергенція: {divergence_emoji.get(analysis['divergence'], '⚪')} {analysis['divergence']}\n"
            f"📉 Зміна ціни: {analysis['price_change']:+.2f}%\n"
            f"📊 Зміна об'єму: {analysis['volume_change']:+.2f}%\n\n"
        )
        
        # Інтерпретація
        if analysis['volume_delta'] > 0.1:
            response += "🟢 Сильний покупний тиск\n"
        elif analysis['volume_delta'] < -0.1:
            response += "🔴 Сильний продажний тиск\n"
        else:
            response += "⚪ Баланс сил\n"
        
        bot.send_message(message.chat.id, response, parse_mode="HTML")
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Помилка: {e}")

# -------------------------
# Основна логіка
# -------------------------
def check_market():
    """Постійна перевірка ринку"""
    logger.info("Запуск Smart Auto перевірки ринку...")
    
    # Лічильник ітерацій для різних типів перевірок
    iteration_count = 0
    
    while True:
        try:
            iteration_count += 1
            
            # Кожні 3 ітерації (15 хвилин) перевіряємо золоті хрести
            if iteration_count % 3 == 0:
                logger.info("Перевіряємо золоті хрести...")
                check_golden_crosses()
            
            # Кожні 6 ітерацій (30 хвилин) перевіряємо китівську активність
            if iteration_count % 6 == 0:
                logger.info("Перевіряємо китівську активність...")
                check_whale_activity_auto()
            
            # Кожні 12 ітерацій (60 хвилин) перевіряємо маніпуляції
            if iteration_count % 12 == 0:
                logger.info("Перевіряємо можливі маніпуляції...")
                check_market_manipulation_auto()
            
            # Основна перевірка ринку (кожні 5 хвилин)
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
            logger.info(f"Аналізуємо {len(top_symbols)} монет: {top_symbols[:5]}...")
            
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
                            
                    time.sleep(0.5)  # Затримка між монетами
                    
                except Exception as e:
                    logger.error(f"Помилка обробки {symbol}: {e}")
                    continue
            
            logger.info(f"Знайдено {signals_found} сигналів. Очікування 5 хвилин...")
            time.sleep(300)  # 5 хвилин між перевірками
            
        except Exception as e:
            logger.error(f"Критична помилка: {e}")
            time.sleep(60)  # 1 хвилина при помилці

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