import os
import asyncio
import ccxt.pro as ccxt
import requests
from fastapi import FastAPI, Request

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SPREAD_THRESHOLD = 10
EXCHANGES = ["binance", "bybit", "okx"]

app = FastAPI()

running = False
chat_id_global = None


# -------- TELEGRAM --------
def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text})


# -------- SCANNER --------
async def arbitrage_ws():
    global running

    exchanges = []
    for name in EXCHANGES:
        exchange = getattr(ccxt, name)()
        exchanges.append(exchange)

    markets = await exchanges[0].load_markets()
    symbols = [s for s in markets if s.endswith("/USDT") and markets[s]["spot"]]

    while running:
        for symbol in symbols:
            prices = []

            for ex in exchanges:
                try:
                    ob = await ex.watch_order_book(symbol)
                    bid = ob["bids"][0][0] if ob["bids"] else None
                    ask = ob["asks"][0][0] if ob["asks"] else None

                    if bid and ask:
                        prices.append({
                            "exchange": ex.id,
                            "bid": bid,
                            "ask": ask
                        })
                except:
                    continue

            if len(prices) < 2:
                continue

            lowest = min(prices, key=lambda x: x["ask"])
            highest = max(prices, key=lambda x: x["bid"])

            spread = (highest["bid"] - lowest["ask"]) / lowest["ask"] * 100

            if spread >= SPREAD_THRESHOLD:
                send_message(
                    chat_id_global,
                    f"🔥 {symbol}\n"
                    f"Buy: {lowest['exchange']} @ {lowest['ask']}\n"
                    f"Sell: {highest['exchange']} @ {highest['bid']}\n"
                    f"Spread: {spread:.2f}%"
                )

        await asyncio.sleep(0.1)


# -------- WEBHOOK --------
@app.post("/webhook")
async def webhook(request: Request):
    global running, chat_id_global

    data = await request.json()
    chat_id = data["message"]["chat"]["id"]
    text = data["message"].get("text", "")

    if text == "/start":
        send_message(chat_id, "Arbitrage bot ready.")

    if text == "/scan":
        if not running:
            running = True
            chat_id_global = chat_id
            asyncio.create_task(arbitrage_ws())
            send_message(chat_id, "Started ultra-fast WebSocket scanning.")
        else:
            send_message(chat_id, "Already scanning.")

    if text == "/stop":
        running = False
        send_message(chat_id, "Stopped scanning.")

    return {"ok": True}


@app.get("/")
def home():
    return {"status": "running"}