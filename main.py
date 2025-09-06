# main.py
import os
import ccxt
import requests
import time
from datetime import datetime, timedelta
from flask import Flask, request
import telebot
import threading
import sys

# -------------------------
# Налаштування через env (безпечніше)
# -------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")   # поставити у Render env
CHAT_ID = os.getenv("CHAT_ID", "")                 # твій chat id (число)
GATE_API_KEY = os.getenv("GATE_API_KEY", "")
GATE_API_SECRET = os.getenv("GATE_API_SECRET", "")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "")       # напр. https://troovy-detective-bot-1-4on4.onrender.com
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = WEBHOOK_HOST + WEBHOOK_PATH if WEBHOOK_HOST else ""

TRADE_AMOUNT_USD = float(os.getenv("TRADE_AMOUNT_USD", "5"))
SPREAD_THRESHOLD = float(os.getenv("SPREAD_THRESHOLD", "2.0"))  # в процентах
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "10"))         # сек

# кількість пар з кожної мережі, можна збільшити
LIMIT_PER_CHAIN = int(os.getenv("LIMIT_PER_CHAIN", "8"))

# -------------------------
# Перевірка обов'язкових налаштувань
# -------------------------
if not TELEGRAM_TOKEN:
    print("ERROR: TELEGRAM_TOKEN не заданий. Встанови його в змінних оточення.")
    sys.exit(1)
if not CHAT_ID:
    print("ERROR: CHAT_ID не заданий. Встанови його в змінних оточення.")
    sys.exit(1)
if not WEBHOOK_HOST:
    print("ERROR: WEBHOOK_HOST не заданий. Встанови його в змінних оточення.")
    sys.exit(1)
if not GATE_API_KEY or not GATE_API_SECRET:
    print("WARNING: GATE API ключі не задані — автоматична торгівля не працюватиме до їх додавання.")

# -------------------------
# Ініціалізація
# -------------------------
bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)

gate = ccxt.gateio({
    "apiKey": GATE_API_KEY,
    "secret": GATE_API_SECRET,
    "options": {"defaultType": "swap"}  # futures (USDT-margined)
})

active_positions = {}  # символ -> side
last_noop_notify = datetime.min  # щоб не спамити "нема можливостей"
NOOP_NOTIFY_INTERVAL = timedelta(minutes=60)  # раз/годину максимум "no opportunities" повідомлення

# -------------------------
# Утиліти лог + Telegram (без падіння при помилці)
# -------------------------
def safe_send(text):
    try:
        bot.send_message(CHAT_ID, text)
    except Exception as e:
        print(f"{datetime.now()} | ❌ Не вдалось відправити в Telegram: {e}")

def log_and_notify(text, tg=True):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {text}"
    print(line)
    if tg:
        safe_send(line)

# -------------------------
# Перевірка токена Telegram + встановлення webhook
# -------------------------
def verify_and_setup_webhook():
    try:
        me = bot.get_me()
        log_and_notify(f"✅ Telegram token валідний — @{me.username} (id {me.id})")
    except Exception as e:
        print(f"{datetime.now()} | ❌ Помилка валідності Telegram token: {e}")
        print("Перевір TELEGRAM_TOKEN (format: 123456:ABC...). Зупиняю запуск webhook.")
        return False

    # встановити webhook через API (додатково логуємо повний результат)
    try:
        resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
                              data={"url": WEBHOOK_URL}, timeout=10)
        try:
            j = resp.json()
        except Exception:
            log_and_notify(f"❌ setWebhook: не JSON відповідь (HTTP {resp.status_code}): {resp.text}")
            return False

        if not j.get("ok", False):
            log_and_notify(f"❌ setWebhook failed: {j}")
            # якщо помилка 401 — явно вказати причину
            if j.get("error_code") == 401:
                log_and_notify("❗ Помилка 401 Unauthorized — перевір Telegram token (BotFather).")
            return False

        log_and_notify("✅ Webhook встановлено успішно.")
        return True

    except Exception as e:
        log_and_notify(f"❌ Помилка при setWebhook: {e}")
        return False

# -------------------------
# Dexscreener: отримуємо токени з ETH / BSC / SOL
# -------------------------
def get_top_tokens(limit_per_chain=LIMIT_PER_CHAIN):
    chains = ["ethereum", "bsc", "solana"]
    headers = {"User-Agent": "Mozilla/5.0"}
    tokens = []
    for chain in chains:
        try:
            url = f"https://api.dexscreener.com/latest/dex/pairs/{chain}"
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code != 200:
                log_and_notify(f"⚠️ Dexscreener ({chain}) HTTP {resp.status_code} — пропускаю", tg=False)
                continue
            try:
                data = resp.json()
            except Exception:
                log_and_notify(f"❌ Некоректна відповідь від Dexscreener ({chain}): {resp.text[:300]}", tg=False)
                continue

            pairs = data.get("pairs", [])[:limit_per_chain]
            for p in pairs:
                base = p.get("baseToken", {})
                price_usd = p.get("priceUsd")
                if base.get("symbol") and price_usd:
                    symbol = base["symbol"].upper() + "/USDT"
                    dex_price = float(price_usd)
                    tokens.append((symbol, dex_price, chain))
            log_and_notify(f"🔍 {chain.upper()}: додано {len(pairs)} пар", tg=False)
        except Exception as e:
            log_and_notify(f"❌ Помилка Dexscreener ({chain}): {e}", tg=False)
    # уникнути дублікатів — беремо перший випадок
    seen = set()
    unique = []
    for sym, price, chain in tokens:
        if sym not in seen:
            seen.add(sym)
            unique.append((sym, price, chain))
    return unique

# -------------------------
# Gate helpers
# -------------------------
def is_pair_available_on_gate(symbol):
    pair = symbol.replace("/", "/USDT:USDT")
    try:
        markets = gate.load_markets()
        return pair in markets
    except Exception as e:
        log_and_notify(f"❌ Помилка завантаження ринків Gate: {e}")
        return False

def open_gate_position(symbol, side):
    pair = symbol.replace("/", "/USDT:USDT")
    if not is_pair_available_on_gate(symbol):
        log_and_notify(f"⚠️ Пара {pair} відсутня на Gate Futures — пропускаю")
        return None, None
    if symbol in active_positions:
        log_and_notify(f"⚠️ Позиція по {symbol} вже відкрита — пропускаю")
        return None, None
    try:
        # перевірка балансу
        bal = gate.fetch_balance()
        usdt_available = bal.get('total', {}).get('USDT', 0)
        if usdt_available < TRADE_AMOUNT_USD:
            log_and_notify(f"❌ Недостатньо USDT ({usdt_available}) — не можу торгувати")
            return None, None

        ticker = gate.fetch_ticker(pair)
        gate_price = ticker['last']
        amount = TRADE_AMOUNT_USD / gate_price
        # округли коректно для ринку (можна додати функцію нормалізації за precision)
        order = gate.create_order(symbol=pair, type="market", side=side.lower(), amount=amount)
        active_positions[symbol] = side
        log_and_notify(f"✅ Відкрито {side} {amount:.6f} {symbol} за Gate ціну {gate_price:.6f}")
        return amount, gate_price
    except Exception as e:
        log_and_notify(f"❌ Помилка відкриття ордеру на Gate: {e}")
        return None, None

def close_gate_position_with_limit(symbol, side, amount, dex_price):
    pair = symbol.replace("/", "/USDT:USDT")
    if not is_pair_available_on_gate(symbol):
        log_and_notify(f"⚠️ Cannot close — pair {pair} not on Gate")
        return
    try:
        close_side = "SELL" if side == "BUY" else "BUY"
        # створюємо reduce-only limit order на DEX ціну
        order = gate.create_order(symbol=pair, type="limit", side=close_side.lower(),
                                  amount=amount, price=dex_price,
                                  params={"reduceOnly": True})
        log_and_notify(f"🎯 Лімітне закриття {close_side} {amount:.6f} {symbol} за {dex_price:.6f}")
        # видаляємо зі списку активних позицій
        active_positions.pop(symbol, None)
    except Exception as e:
        log_and_notify(f"❌ Помилка при виставленні ліміту на Gate: {e}")

# -------------------------
# Арбітраж логіка по одній парі
# -------------------------
def arbitrage_one(symbol, dex_price, chain):
    try:
        pair = symbol.replace("/", "/USDT:USDT")
        if not is_pair_available_on_gate(symbol):
            return False

        gate_ticker = gate.fetch_ticker(pair)
        gate_price = gate_ticker['last']
        spread = (dex_price - gate_price) / gate_price * 100
        # лог в консоль короткий
        print(f"{datetime.now()} | [{chain.upper()}] {symbol} | DEX {dex_price:.6f} | Gate {gate_price:.6f} | Spread {spread:.2f}%")

        # перевірка порогу
        if spread >= SPREAD_THRESHOLD:
            amt, _ = open_gate_position(symbol, "BUY")
            if amt:
                close_gate_position_with_limit(symbol, "BUY", amt, dex_price)
                return True
        elif spread <= -SPREAD_THRESHOLD:
            amt, _ = open_gate_position(symbol, "SELL")
            if amt:
                close_gate_position_with_limit(symbol, "SELL", amt, dex_price)
                return True
        return False
    except Exception as e:
        log_and_notify(f"❌ Помилка arbitrage_one ({symbol}): {e}")
        return False

# -------------------------
# Основний цикл
# -------------------------
def start_arbitrage():
    log_and_notify("🚀 Бот запущено — починаю моніторинг.")
    global last_noop_notify
    loop_count = 0
    while True:
        loop_count += 1
        opportunities = []
        tokens = get_top_tokens(limit_per_chain=LIMIT_PER_CHAIN)
        # коротке сумарне повідомлення про отримані токени (щоб бачити, що пошук іде)
        summary = {}
        for _, _, ch in tokens:
            summary[ch] = summary.get(ch, 0) + 1
        summary_text = ", ".join([f"{k.upper()}: {v}" for k, v in summary.items()]) if summary else "0"
        log_and_notify(f"🔁 Цикл {loop_count}: отримано токенів {len(tokens)} ({summary_text})", tg=False)

        for sym, dex_price, chain in tokens:
            ok = arbitrage_one(sym, dex_price, chain)
            if ok:
                opportunities.append(sym)
            # коротка пауза, щоб не перевантажити API біржі
            time.sleep(0.4)

        # після обходу — повідомлення в Telegram лише якщо були можливості або періодична "no-op"
        if opportunities:
            log_and_notify(f"💥 Знайдено можливості: {', '.join(opportunities)}")
        else:
            # надсилаємо коротке "no-op" не частіше ніж раз на годину
            if datetime.now() - last_noop_notify > NOOP_NOTIFY_INTERVAL:
                safe_send(f"{datetime.now().strftime('%H:%M:%S')} | Немає можливостей у цьому циклі ({len(tokens)} токенів перевірено).")
                last_noop_notify = datetime.now()

        time.sleep(CHECK_INTERVAL)

# -------------------------
# Webhook route + health root
# -------------------------
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    json_str = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK", 200

@app.route("/", methods=["GET"])
def health():
    return "OK", 200

# -------------------------
# Запуск
# -------------------------
if __name__ == "__main__":
    ok = verify_and_setup_webhook()
    if not ok:
        print("Webhook не встановлено — перевір TELEGRAM_TOKEN. Bot працюватиме, але без webhook (власне, без команд).")
    # старт арбітражного потоку
    threading.Thread(target=start_arbitrage, daemon=True).start()
    # Flask
    app.run(host="0.0.0.0", port=5000)