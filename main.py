#!/usr/bin/env python3
"""
Futures spread watcher (ccxt.pro) — watch multiple exchanges (futures), pick top-N by 24h change,
subscribe to tickers via websocket and alert to Telegram when cross-exchange spread appears or closes.
"""
import os
import asyncio
import time
import logging
from datetime import datetime, timezone
from typing import Dict, Set, List, Tuple
import requests

try:
    import ccxt.pro as ccxtpro
    import ccxt
except Exception as e:
    ccxtpro = None
    ccxt = None

# ---------------- logging ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("futures-spread-bot")

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
PORT = int(os.getenv("PORT", "10000"))

EXCHANGE_IDS = ["bybit", "mexc", "lbank"]
TOP_N_PER_EXCHANGE = 120
INTERSECTION_MIN_EXCHANGES = 2
SPREAD_MIN_ABS = 0.0001
SPREAD_MIN_PCT = 2.0
SPREAD_MAX_PCT = 100.0
ALERT_COOLDOWN = 60
DEBUG_MODE = False
TOP_BY_CHANGE = True

# ---------------- global state ----------------
latest_quote = {}  # { exchange_id: { symbol: {'bid':float,'ask':float,'timestamp':ts} } }
last_alert_ts: Dict[Tuple[str, str, str], float] = {}
active_spreads: Dict[Tuple[str, str, str], float] = {}  # track open spreads

# ---------------- telegram ----------------
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)

def format_alert(symbol, buy_ex, buy_ask, sell_ex, sell_bid, pct, absdiff, ts):
    t = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        "🔔 *Futures Spread Alert*\n"
        f"*Symbol:* `{symbol}`\n"
        f"*Buy (ask):* `{buy_ask:.8f}` @ *{buy_ex}*\n"
        f"*Sell (bid):* `{sell_bid:.8f}` @ *{sell_ex}*\n"
        f"*Spread:* *{pct:.2f}%* (`{absdiff:.8f}`)\n"
        f"*Time:* {t}"
    )

def format_close(symbol, buy_ex, sell_ex, prev, now_pct):
    t = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        "🔕 *Spread Closed*\n"
        f"*Symbol:* `{symbol}`\n"
        f"{buy_ex} ↔ {sell_ex}\n"
        f"Previous: *{prev:.2f}%* → Now: *{now_pct:.2f}%*\n"
        f"Time: {t}"
    )

# ---------------- helpers ----------------
def safe_attr(obj, key, default=None):
    try:
        return obj.get(key, default)
    except Exception:
        return default

async def discover_futures_markets(exchanges: List[str]):
    markets_by_exchange = {}
    if not ccxt:
        return markets_by_exchange

    for ex_id in exchanges:
        try:
            ex = getattr(ccxt, ex_id)()
            ex.load_markets()
            symbols = [
                s for s, m in ex.markets.items()
                if (m.get('contract') or m.get('future') or m.get('type') == 'future')
                and 'USDT' in s
            ]
            markets_by_exchange[ex_id] = symbols
            logger.info("%s: %d futures symbols", ex_id, len(symbols))
        except Exception as e:
            logger.warning("discover %s failed: %s", ex_id, e)
    return markets_by_exchange

async def compute_top_by_change(exchange_ids, markets_by_exchange, top_n):
    results = {}
    for ex_id, symbols in markets_by_exchange.items():
        try:
            ex = getattr(ccxt, ex_id)()
            tickers = ex.fetch_tickers(symbols)
            scored = []
            for s, t in tickers.items():
                pct = safe_attr(t, 'percentage', 0)
                if pct is None:
                    pct = 0
                scored.append((s, abs(float(pct))))
            scored.sort(key=lambda x: x[1], reverse=True)
            results[ex_id] = [s for s, _ in scored[:top_n]]
            logger.info("%s: top %d by 24h change", ex_id, len(results[ex_id]))
        except Exception as e:
            logger.warning("top_by_change %s failed: %s", ex_id, e)
            results[ex_id] = symbols[:top_n]
    return results

# ---------------- ws logic ----------------
async def create_pro_client(ex_id):
    if not ccxtpro or not hasattr(ccxtpro, ex_id):
        return None
    try:
        ex = getattr(ccxtpro, ex_id)({"enableRateLimit": True})
        ex.options["defaultType"] = "swap"
        return ex
    except Exception as e:
        logger.warning("create_pro_client %s failed: %s", ex_id, e)
        return None

async def watch_exchange_tickers(exchange, ex_id, symbols):
    latest_quote.setdefault(ex_id, {})
    logger.info("Watching %s (%d symbols)", ex_id, len(symbols))
    try:
        while True:
            for s in symbols:
                try:
                    ob = await exchange.watch_order_book(s)
                    bid, ask = ob["bids"][0][0], ob["asks"][0][0]
                    latest_quote[ex_id][s] = {"bid": bid, "ask": ask, "ts": time.time()}
                    await check_spread_for_symbol(s)
                except Exception as e:
                    await asyncio.sleep(0.05)
            await asyncio.sleep(0.01)
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Watcher stopped for %s", ex_id)

async def check_spread_for_symbol(symbol):
    rows = [(ex, q["bid"], q["ask"]) for ex, m in latest_quote.items() if (q := m.get(symbol))]
    if len(rows) < 2:
        return

    buy_ex, _, buy_ask = sorted(rows, key=lambda x: x[2])[0]
    sell_ex, sell_bid, _ = sorted(rows, key=lambda x: x[1], reverse=True)[0]
    if buy_ex == sell_ex:
        return

    absdiff = sell_bid - buy_ask
    if absdiff <= 0 or buy_ask == 0:
        return

    pct = (absdiff / buy_ask) * 100
    key = (symbol, buy_ex, sell_ex)
    now = time.time()

    # --- Spread opened ---
    if pct >= SPREAD_MIN_PCT and pct <= SPREAD_MAX_PCT:
        if key not in active_spreads and (now - last_alert_ts.get(key, 0)) > ALERT_COOLDOWN:
            last_alert_ts[key] = now
            active_spreads[key] = pct
            msg = format_alert(symbol, buy_ex, buy_ask, sell_ex, sell_bid, pct, absdiff, now)
            send_telegram(msg)
            logger.info("ALERT %.2f%% %s (%s->%s)", pct, symbol, buy_ex, sell_ex)
        else:
            active_spreads[key] = pct

    # --- Spread closed ---
    elif key in active_spreads and pct < (SPREAD_MIN_PCT / 2):
        prev = active_spreads.pop(key)
        msg = format_close(symbol, buy_ex, sell_ex, prev, pct)
        send_telegram(msg)
        logger.info("CLOSED %.2f%%->%.2f%% %s (%s↔%s)", prev, pct, symbol, buy_ex, sell_ex)

# ---------------- orchestrator ----------------
async def run_ws_monitor():
    markets_by_exchange = await discover_futures_markets(EXCHANGE_IDS)
    if not markets_by_exchange:
        logger.error("No markets found.")
        return

    top_by_ex = await compute_top_by_change(EXCHANGE_IDS, markets_by_exchange, TOP_N_PER_EXCHANGE)
    symbol_occ = {}
    for ex, syms in top_by_ex.items():
        for s in syms:
            symbol_occ.setdefault(s, set()).add(ex)
    candidates = [s for s, exs in symbol_occ.items() if len(exs) >= INTERSECTION_MIN_EXCHANGES]

    watch_list = {ex: [s for s in syms if s in candidates] for ex, syms in top_by_ex.items()}

    clients = {}
    tasks = []
    for ex_id, syms in watch_list.items():
        client = await create_pro_client(ex_id)
        if not client:
            continue
        clients[ex_id] = client
        tasks.append(asyncio.create_task(watch_exchange_tickers(client, ex_id, syms)))

    if not tasks:
        logger.error("No watchers started.")
        return

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        for client in clients.values():
            try:
                await client.close()
            except Exception:
                pass

# ---------------- keepalive ----------------
from http.server import BaseHTTPRequestHandler, HTTPServer
def start_keepalive_server(port=PORT):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"futures-spread-bot ok")
    HTTPServer(("", port), Handler).serve_forever()

# ---------------- main ----------------
def main():
    import threading
    threading.Thread(target=start_keepalive_server, daemon=True).start()
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(run_ws_monitor())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()

if __name__ == "__main__":
    logger.info("🚀 Starting Futures Spread Bot (with Spread Close Alerts)")
    main()