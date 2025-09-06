import ccxt
import requests
import time
from datetime import datetime

# -------------------------
# Налаштування
# -------------------------
# Gate API (фʼючерси USDT)
GATE_API_KEY = "cf99af3f8c0c1a711408f1a1970be8be"
GATE_API_SECRET = "4bd0a51eac2133386e60f4c5e1a78ea9c364e542399bc1865e679f509e93f72e"

gate = ccxt.gateio({
    "apiKey": GATE_API_KEY,
    "secret": GATE_API_SECRET,
    "options": {"defaultType": "swap"}  # ф'ючерси USDT
})

TRADE_AMOUNT_USD = 5       # малий обсяг для тесту
SPREAD_THRESHOLD = 0.5     # мінімальний спред %
CHECK_INTERVAL = 5         # секунд між перевірками

# -------------------------
# Отримання топ токенів з DEX Screener
# -------------------------
def get_top_tokens(limit=10):
    try:
        resp = requests.get("https://api.dexscreener.com/latest/dex/tokens")
        data = resp.json()
        # Беремо перші limit токенів
        tokens = []
        for t in data.get("pairs", [])[:limit]:
            symbol = t["baseToken"]["symbol"] + "/USDT"
            price = float(t["priceUsd"])
            tokens.append((symbol, price))
        return tokens
    except Exception as e:
        print("Помилка отримання топ токенів:", e)
        return []

# -------------------------
# Отримання ціни з DEX
# -------------------------
def get_dex_price(symbol):
    try:
        dex_sym = symbol.replace("/", "-")
        resp = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{dex_sym}")
        data = resp.json()
        price = float(data['pairs'][0]['priceUsd'])
        return price
    except Exception as e:
        print("Помилка DEX:", e)
        return None

# -------------------------
# Відкриття позиції на Gate
# -------------------------
def open_gate_position(symbol, side):
    try:
        pair = symbol.replace("/", "/USDT:USDT")
        ticker = gate.fetch_ticker(pair)
        gate_price = ticker['last']
        amount = TRADE_AMOUNT_USD / gate_price

        order = gate.create_order(
            symbol=pair,
            type="market",
            side=side.lower(),
            amount=amount
        )
        print(f"{datetime.now()} | ✅ Відкрито {side} {amount} {symbol} за Gate ціною {gate_price:.4f}")
        return amount, gate_price
    except Exception as e:
        print("Помилка відкриття позиції:", e)
        return None, None

# -------------------------
# Лімітний ордер на закриття за DEX
# -------------------------
def close_gate_position(symbol, side, amount, dex_price):
    try:
        pair = symbol.replace("/", "/USDT:USDT")
        close_side = "SELL" if side == "BUY" else "BUY"

        order = gate.create_order(
            symbol=pair,
            type="limit",
            side=close_side.lower(),
            amount=amount,
            price=dex_price,
            params={"reduceOnly": True}  # тільки закриття
        )
        print(f"{datetime.now()} | 🎯 Лімітний ордер на закриття {close_side} {amount} {symbol} за DEX ціною {dex_price}")
    except Exception as e:
        print("Помилка закриття позиції:", e)

# -------------------------
# Арбітраж по одному токені
# -------------------------
def arbitrage(symbol):
    dex_price = get_dex_price(symbol)
    if not dex_price:
        return

    pair = symbol.replace("/", "/USDT:USDT")
    gate_ticker = gate.fetch_ticker(pair)
    gate_price = gate_ticker['last']

    spread = (dex_price - gate_price) / gate_price * 100
    print(f"{datetime.now()} | {symbol} | DEX: {dex_price:.4f} | Gate: {gate_price:.4f} | Spread: {spread:.2f}%")

    if spread >= SPREAD_THRESHOLD:
        # DEX дорожчий → купуємо на Gate
        amount, _ = open_gate_position(symbol, "BUY")
        if amount:
            close_gate_position(symbol, "BUY", amount, dex_price)
    elif spread <= -SPREAD_THRESHOLD:
        # DEX дешевший → продаємо на Gate
        amount, _ = open_gate_position(symbol, "SELL")
        if amount:
            close_gate_position(symbol, "SELL", amount, dex_price)

# -------------------------
# Основний цикл
# -------------------------
if __name__ == "__main__":
    while True:
        tokens = get_top_tokens(limit=10)  # топ 10 токенів
        for symbol, _ in tokens:
            arbitrage(symbol)
        time.sleep(CHECK_INTERVAL)