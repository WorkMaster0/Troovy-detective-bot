#!/usr/bin/env python3
"""
Futures Spread Watcher — ccxt.pro + REST fallback + HTTP keepalive (для Render)
Моніторить USDT ф’ючерси між біржами та шле сповіщення в Telegram.
"""

import os
import asyncio
import time
import json
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict
import signal
import requests
import ccxt

try:
    import ccxt.pro as ccxtpro
except Exception:
    ccxtpro = None

# === CONFIG ===
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("futures-bot")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

EXCHANGES_TO_TRY = ["gate", "mexc", "lbank"]

MIN_SPREAD_PCT = 0.5
MAX_SPREAD_PCT = 100.0
REST_FALLBACK_INTERVAL = 15
DEDUP_INTERVAL = 120

# === Telegram ===
def escape_md_v2(text: str) -> str:
    import re
    escape_chars = r"_*[]()~`>#+-=|{}.!"
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", str(text))

def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        payload = {"chat_id": CHAT_ID, "text": escape_md_v2(msg), "parse_mode": "MarkdownV2"}
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        logger.warning(f"Telegram send error: {e}")

def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

# === Market Discovery ===
def discover_futures_markets(exchange_ids):
    results = {}
    for ex_id in exchange_ids:
        try:
            ex = getattr(ccxt, ex_id)()
            markets = ex.load_markets()
            futures = {
                m["symbol"]
                for m in markets.values()
                if ("USDT" in m["symbol"])
                and (m.get("contract") or "swap" in (m.get("type") or "") or "future" in (m.get("type") or ""))
            }
            logger.info("%s -> %d futures symbols", ex_id, len(futures))
            results[ex_id] = futures
        except Exception as e:
            logger.warning(f"{ex_id} discover error: {e}")
            results[ex_id] = set()
    return results

def find_common_symbols(exchange_symbols):
    count = {}
    for ex, syms in exchange_symbols.items():
        for s in syms:
            count[s] = count.get(s, 0) + 1
    common = [s for s, c in count.items() if c >= 2]
    logger.info("Found %d common symbols", len(common))
    return common

# === Spread check ===
last_sent = {}

def analyze_spread(symbol, shared):
    data = shared.get(symbol, {})
    if len(data) < 2:
        return
    best_ask, best_ask_ex = float("inf"), None
    best_bid, best_bid_ex = 0.0, None
    for ex, v in data.items():
        bid, ask = v.get("bid", 0), v.get("ask", 0)
        if ask and ask < best_ask:
            best_ask, best_ask_ex = ask, ex
        if bid and bid > best_bid:
            best_bid, best_bid_ex = bid, ex
    if not best_ask_ex or not best_bid_ex or best_ask_ex == best_bid_ex:
        return
    spread = (best_bid / best_ask - 1) * 100 if best_ask > 0 else 0
    if spread <= MIN_SPREAD_PCT or spread > MAX_SPREAD_PCT:
        return
    key = f"{symbol}|{best_ask_ex}|{best_bid_ex}"
    now = time.time()
    if now - last_sent.get(key, 0) < DEDUP_INTERVAL:
        return
    last_sent[key] = now
    msg = (
        f"🔔 *Spread Alert*\n"
        f"Symbol: `{symbol}`\n"
        f"Buy (ask): `{best_ask:.8f}` @ *{best_ask_ex}*\n"
        f"Sell (bid): `{best_bid:.8f}` @ *{best_bid_ex}*\n"
        f"Spread: *{spread:.2f}%* ({best_bid - best_ask:.8f})\n"
        f"Time: {now_utc()}"
    )
    logger.info(msg.replace("*", ""))
    send_telegram(msg)

# === Async WS Mode ===
async def create_client(ex_id):
    try:
        ex_class = getattr(ccxtpro, ex_id)
        client = ex_class({"enableRateLimit": True})
        await client.load_markets()
        return client
    except Exception as e:
        logger.warning(f"{ex_id} ws init fail: {e}")
        return None

async def ws_watcher(ex_id, client, symbols, shared):
    logger.info(f"Watcher started for {ex_id}")
    tasks = [asyncio.create_task(single_ticker_loop(ex_id, client, s, shared)) for s in symbols]
    await asyncio.gather(*tasks)

async def single_ticker_loop(ex_id, client, symbol, shared):
    while True:
        try:
            t = await client.watch_ticker(symbol)
            bid, ask = t.get("bid") or 0, t.get("ask") or 0
            if not bid or not ask:
                continue
            shared.setdefault(symbol, {})[ex_id] = {"bid": bid, "ask": ask}
            analyze_spread(symbol, shared)
        except Exception as e:
            await asyncio.sleep(3)

# === REST fallback ===
async def rest_fallback_loop(exchange_ids, symbols, shared):
    exchanges = [getattr(ccxt, ex)() for ex in exchange_ids]
    while True:
        for ex in exchanges:
            try:
                tickers = ex.fetch_tickers(symbols)
                for sym, t in tickers.items():
                    bid, ask = t.get("bid"), t.get("ask")
                    if not bid or not ask:
                        continue
                    shared.setdefault(sym, {})[ex.id] = {"bid": bid, "ask": ask}
                    analyze_spread(sym, shared)
            except Exception:
                pass
        await asyncio.sleep(REST_FALLBACK_INTERVAL)

# === HTTP keepalive (Render fix) ===
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def start_http_server():
    port = int(os.getenv("PORT", 10000))
    srv = HTTPServer(("", port), KeepAliveHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    logger.info(f"Keepalive HTTP server running on port {port}")

# === main ===
async def main():
    start_http_server()

    exchange_symbols = discover_futures_markets(EXCHANGES_TO_TRY)
    common = find_common_symbols(exchange_symbols)
    exch_symbols = {ex: [s for s in syms if s in common] for ex, syms in exchange_symbols.items()}
    shared = {}

    clients = {}
    try:
        if ccxtpro:
            for ex in EXCHANGES_TO_TRY:
                cli = await create_client(ex)
                if cli:
                    clients[ex] = cli
            tasks = [ws_watcher(ex, cli, exch_symbols[ex], shared) for ex, cli in clients.items()]
            await asyncio.gather(*tasks)
        else:
            await rest_fallback_loop(EXCHANGES_TO_TRY, common, shared)
    finally:
        # акуратне закриття WS
        for ex, cli in clients.items():
            try:
                await cli.close()
            except Exception:
                pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down gracefully...")