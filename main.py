#!/usr/bin/env python3
"""
Futures spread watcher (ccxt.pro) — watch multiple exchanges (futures), pick top-N by 24h change,
subscribe to tickers via websocket and alert to Telegram when cross-exchange spread appears.

Environment variables:
  TELEGRAM_TOKEN, CHAT_ID
  PORT (optional, default 10000)
  GATE_API_KEY, GATE_API_SECRET, MEXC_API_KEY, MEXC_API_SECRET, LBANK_API_KEY, LBANK_API_SECRET
Config in code below.
"""
import os
import asyncio
import time
import json
import logging
import math
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

# Exchanges to attempt (ids used by ccxt / ccxt.pro)
EXCHANGE_IDS = ["gate", "mexc", "lbank", "hotcoin", "hotbit"]  # will skip unsupported
TOP_N_PER_EXCHANGE = 120      # top N symbols by 24h change on each exchange (absolute change)
INTERSECTION_MIN_EXCHANGES = 2  # symbol must exist on at least this many exchanges to monitor
SPREAD_MIN_ABS = 0.0001        # minimal absolute spread (in quote currency) to consider
SPREAD_MIN_PCT = 2.0           # minimal relative spread percent (0.1%)
SPREAD_MAX_PCT = 100.0         # exclude absurd spreads >100%
ALERT_COOLDOWN = 60           # seconds per symbol pair to avoid duplicates
PRICE_CHANGE_LOOKBACK = "24h"  # descriptive only
DEBUG_MODE = False             # set True to increase verbosity / test alerts
TOP_BY_CHANGE = True           # use price-change filter instead of volume

# ---------------- global state ----------------
latest_quote = {}  # { exchange_id: { symbol: {'bid':float,'ask':float,'timestamp':ts, 'info':...} } }
watched_symbols: Set[str] = set()
last_alert_ts: Dict[Tuple[str, str, str], float] = {}  # key: (symbol, cheap_ex, expensive_ex) -> ts

# ---------------- helper: telegram ----------------
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.debug("Telegram credentials not set; skipping send.")
        return
    try:
        payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
        resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=10)
        if resp.status_code != 200:
            logger.warning("Telegram send failed: %s %s", resp.status_code, resp.text)
    except Exception as e:
        logger.exception("send_telegram error: %s", e)

def format_alert(symbol: str, buy_ex: str, buy_ask: float, sell_ex: str, sell_bid: float, pct: float, absdiff: float, ts: float):
    t = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    text = (
        "🔔 *Futures Spread Alert*\n"
        f"*Symbol:* `{symbol}`\n"
        f"*Buy (ask):* `{buy_ask:.8f}` @ *{buy_ex}*\n"
        f"*Sell (bid):* `{sell_bid:.8f}` @ *{sell_ex}*\n"
        f"*Spread:* *{pct:.2f}%* (`{absdiff:.8f}`)\n"
        f"*Time:* {t}"
    )
    return text

# ---------------- utilities for ccxt discovery ----------------
def safe_attr(obj, key, default=None):
    try:
        return obj.get(key, default)
    except Exception:
        return default

async def discover_futures_markets(exchange_ids: List[str]) -> Dict[str, List[str]]:
    """
    For each exchange id, use synchronous ccxt (if available) to load markets and return
    list of futures (contract) symbols settled in USDT (common naming).
    """
    if not ccxt:
        logger.error("ccxt (sync) not available — cannot discover markets")
        return {}

    markets_by_exchange = {}
    for ex_id in exchange_ids:
        try:
            if not hasattr(ccxt, ex_id):
                logger.warning("ccxt has no exchange class for id '%s' — skipping", ex_id)
                continue
            ex_kwargs = {}
            # pick API keys from env if provided
            api_key = os.getenv(f"{ex_id.upper()}_API_KEY")
            secret = os.getenv(f"{ex_id.upper()}_API_SECRET")
            if api_key and secret:
                ex_kwargs['apiKey'] = api_key
                ex_kwargs['secret'] = secret
            ex = getattr(ccxt, ex_id)(ex_kwargs)
            # load markets (sync)
            ex.load_markets()
            symbols = []
            for s, m in ex.markets.items():
                # contract futures detection: market.get('contract') true and USDT involved
                is_contract = bool(m.get('contract') or m.get('future') or m.get('type') == 'future')
                settle = m.get('settle') or m.get('settlement') or m.get('quote')
                # many exchanges represent futures as 'BTC/USDT:USDT' or market['symbol'] includes 'USDT'
                if is_contract and ('USDT' in s or (isinstance(settle, str) and 'USDT' in settle.upper())):
                    symbols.append(s)
            markets_by_exchange[ex_id] = sorted(list(set(symbols)))
            logger.info("%s -> %d futures symbols discovered", ex_id, len(symbols))
        except Exception as e:
            logger.exception("discover markets failed for %s: %s", ex_id, e)
    return markets_by_exchange

async def compute_top_by_change(exchange_ids: List[str], markets_by_exchange: Dict[str, List[str]], top_n: int) -> Dict[str, List[str]]:
    """
    For each exchange, compute 24h change % via synchronous ccxt.fetch_ticker or via REST
    and return top_n symbols by absolute change.
    """
    if not ccxt:
        logger.error("ccxt not available — cannot compute 24h change")
        return markets_by_exchange

    top_by_exchange = {}
    for ex_id, symbols in markets_by_exchange.items():
        try:
            if not hasattr(ccxt, ex_id):
                logger.warning("ccxt has no exchange class for id '%s' — skipping", ex_id)
                continue
            ex_kwargs = {}
            api_key = os.getenv(f"{ex_id.upper()}_API_KEY")
            secret = os.getenv(f"{ex_id.upper()}_API_SECRET")
            if api_key and secret:
                ex_kwargs['apiKey'] = api_key
                ex_kwargs['secret'] = secret
            ex = getattr(ccxt, ex_id)(ex_kwargs)
            to_score = []
            # fetch tickers in batches if supported
            try:
                tickers = ex.fetch_tickers(symbols)
            except Exception:
                # fallback: fetch one by one
                tickers = {}
                for s in symbols:
                    try:
                        tickers[s] = ex.fetch_ticker(s)
                    except Exception:
                        continue
            for s, t in tickers.items():
                pct = None
                try:
                    pct = safe_attr(t, 'percentage')
                    if pct is None:
                        # some exchanges provide 'info' with other fields
                        info = safe_attr(t, 'info', {})
                        # try common fields
                        pct = safe_attr(info, 'priceChangePercent') or safe_attr(info, 'priceChangePercent24h') or safe_attr(info, 'percentChange')
                        if pct is not None:
                            pct = float(pct)
                except Exception:
                    pct = None
                if pct is None:
                    pct = 0.0
                to_score.append((s, abs(float(pct))))
            to_score.sort(key=lambda x: x[1], reverse=True)
            top_symbols = [s for s, _ in to_score[:top_n]]
            top_by_exchange[ex_id] = top_symbols
            logger.info("%s -> selected top %d by 24h change", ex_id, len(top_symbols))
        except Exception as e:
            logger.exception("compute top change failed for %s: %s", ex_id, e)
            top_by_exchange[ex_id] = symbols[:top_n]
    return top_by_exchange

# ---------------- ccxt.pro WS watchers ----------------
async def create_pro_client(exchange_id: str):
    """
    Create ccxt.pro client instance (async). Returns instance or None if not available.
    """
    if not ccxtpro:
        logger.error("ccxt.pro not available in environment; cannot use WS clients")
        return None
    if not hasattr(ccxtpro, exchange_id):
        logger.warning("ccxt.pro has no exchange class for id '%s' - skipping", exchange_id)
        return None
    kwargs = {
        'enableRateLimit': True,
        # optional keys from env
    }
    api_key = os.getenv(f"{exchange_id.upper()}_API_KEY")
    api_secret = os.getenv(f"{exchange_id.upper()}_API_SECRET")
    if api_key and api_secret:
        kwargs['apiKey'] = api_key
        kwargs['secret'] = api_secret
    try:
        ex = getattr(ccxtpro, exchange_id)(kwargs)
        # set verbose in debug
        if DEBUG_MODE:
            ex.verbose = True
        # some exchanges require specify defaultType = 'future' / 'swap'
        try:
            ex.options = ex.options or {}
            # many providers: set default type to 'future' or 'swap'
            if 'defaultType' in ex.options:
                ex.options['defaultType'] = 'future'
            else:
                ex.options.update({'defaultType': 'future', 'defaultSubType': 'linear'})
        except Exception:
            pass
        await asyncio.sleep(0)  # ensure coroutine
        logger.info("Initialized exchange client: %s", exchange_id)
        return ex
    except Exception as e:
        logger.exception("Failed to init exchange %s: %s", exchange_id, e)
        return None

async def watch_exchange_tickers(exchange, ex_id: str, symbols: List[str]):
    """
    Subscribe to order books via watch_order_book for given exchange and symbols.
    Keeps updating latest_quote[ex_id][symbol].
    """
    global latest_quote
    latest_quote.setdefault(ex_id, {})
    logger.info("Watcher started for %s (symbols=%d)", ex_id, len(symbols))
    try:
        while True:
            for s in symbols:
                try:
                    orderbook = await exchange.watch_order_book(s)
                    if not orderbook or not orderbook.get('bids') or not orderbook.get('asks'):
                        continue
                    bid = float(orderbook['bids'][0][0])
                    ask = float(orderbook['asks'][0][0])
                    ts = float(orderbook.get('timestamp') or time.time())
                    latest_quote[ex_id][s] = {'bid': bid, 'ask': ask, 'timestamp': ts}
                    if DEBUG_MODE:
                        logger.debug("%s %s bid=%s ask=%s", ex_id, s, bid, ask)
                    await check_spread_for_symbol(s, ex_id)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug("%s watch_order_book error for %s: %s", ex_id, s, e)
                    await asyncio.sleep(0.05)
            await asyncio.sleep(0.01)
    finally:
        logger.info("Watcher finished for %s", ex_id)

async def check_spread_for_symbol(symbol: str, updated_ex: str):
    """
    Compare updated_ex's ask vs other exchanges' bid for same symbol to find arbitrage spread.
    Send alert if found and passes filters.
    """
    # collect available quotes for this symbol across exchanges
    rows = []
    for ex, d in latest_quote.items():
        q = d.get(symbol)
        if q and q.get('bid') and q.get('ask'):
            rows.append((ex, q['bid'], q['ask'], q['timestamp']))
    if len(rows) < 2:
        return
    # sort by ask ascending to find cheapest ask and by bid descending to find most expensive bid
    rows_by_ask = sorted(rows, key=lambda x: x[2])   # (ex, bid, ask, ts)
    rows_by_bid = sorted(rows, key=lambda x: x[1], reverse=True)
    # evaluate best buy (cheapest ask) vs best sell (highest bid)
    buy_ex, buy_bid_dummy, buy_ask, buy_ts = rows_by_ask[0]
    sell_ex, sell_bid, sell_ask_dummy, sell_ts = rows_by_bid[0]
    # ensure not same exchange
    if buy_ex == sell_ex:
        # possibly check second best
        if len(rows_by_bid) > 1 and rows_by_bid[1][0] != buy_ex:
            sell_ex, sell_bid, _, sell_ts = rows_by_bid[1]
        else:
            return
    # compute spread percent relative to buy_ask
    if buy_ask == 0:
        return
    absdiff = sell_bid - buy_ask
    pct = (absdiff / buy_ask) * 100.0
    # require positive arbitrage
    if absdiff <= 0:
        return
    # filters
    if pct < SPREAD_MIN_PCT:
        return
    if pct > SPREAD_MAX_PCT:
        # skip absurd spreads
        if DEBUG_MODE:
            logger.debug("Skipping huge spread %s%% for %s (%s vs %s)", pct, symbol, buy_ex, sell_ex)
        return
    # low absolute threshold
    if absdiff < SPREAD_MIN_ABS:
        return
    # cooldown deduplication
    key = (symbol, buy_ex, sell_ex)
    now = time.time()
    last = last_alert_ts.get(key, 0)
    if now - last < ALERT_COOLDOWN:
        return
    last_alert_ts[key] = now
    # format & send
    msg = format_alert(symbol, buy_ex, buy_ask, sell_ex, sell_bid, pct, absdiff, now)
    logger.info("ALERT %s: buy %s @%s sell %s @%s pct=%.2f", symbol, buy_ex, buy_ask, sell_ex, sell_bid, pct)
    send_telegram(msg)

# ---------------- orchestration ----------------
async def run_ws_monitor():
    if ccxtpro is None or ccxt is None:
        logger.error("ccxt/ccxt.pro not installed. Exiting.")
        return

    # 1) discover futures markets via sync ccxt
    markets_by_exchange = await discover_futures_markets(EXCHANGE_IDS)
    if not markets_by_exchange:
        logger.error("No markets discovered — exiting")
        return

    # 2) compute top by 24h change (if enabled)
    if TOP_BY_CHANGE:
        top_by_exchange = await compute_top_by_change(list(markets_by_exchange.keys()), markets_by_exchange, TOP_N_PER_EXCHANGE)
    else:
        top_by_exchange = {k: v[:TOP_N_PER_EXCHANGE] for k, v in markets_by_exchange.items()}

    # 3) build candidate pool (symbols that appear on at least INTERSECTION_MIN_EXCHANGES)
    symbol_occurrences = {}
    for ex, syms in top_by_exchange.items():
        for s in syms:
            symbol_occurrences.setdefault(s, set()).add(ex)
    # pick symbols present on enough exchanges
    candidates = [s for s, exs in symbol_occurrences.items() if len(exs) >= INTERSECTION_MIN_EXCHANGES]
    logger.info("Found %d candidate symbols present on >=%d exchanges", len(candidates), INTERSECTION_MIN_EXCHANGES)
    if not candidates:
        logger.error("No candidate symbols to watch — exiting")
        return

    # 4) For each exchange, prepare the list of symbols (intersection with candidates)
    watch_list = {}
    for ex_id, syms in top_by_exchange.items():
        watch_symbols = [s for s in syms if s in candidates]
        if watch_symbols:
            watch_list[ex_id] = watch_symbols
            logger.info("%s -> subscribing to %d common symbols", ex_id, len(watch_symbols))
        else:
            logger.info("%s -> no common symbols to subscribe", ex_id)

    # 5) create ccxt.pro clients for exchanges and start watchers
    clients = {}
    tasks = []
    try:
        for ex_id, symbols in watch_list.items():
            client = await create_pro_client(ex_id)
            if client is None:
                continue
            clients[ex_id] = client
            t = asyncio.create_task(watch_exchange_tickers(client, ex_id, symbols))
            tasks.append(t)
        if not tasks:
            logger.error("No websocket watcher tasks started — exiting")
            return
        # wait until cancelled (run forever)
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("WS monitor cancelled, closing clients")
    except Exception as e:
        logger.exception("run_ws_monitor error: %s", e)
    finally:
        # close all clients cleanly
        for ex_id, client in clients.items():
            try:
                await client.close()
            except Exception as e:
                logger.exception("Error closing client %s: %s", ex_id, e)

# ---------------- HTTP keepalive (simple) ----------------
from http.server import BaseHTTPRequestHandler, HTTPServer
def start_keepalive_server(port: int = PORT):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"futures-spread-bot ok")
        def log_message(self, fmt, *args):
            # minimal logging
            logger.debug(fmt % args)
    server = HTTPServer(("", port), Handler)
    logger.info("Keepalive HTTP server running on port %d", port)
    server.serve_forever()

# ---------------- main entry ----------------
def main():
    # run keepalive server in thread for platforms like Render
    import threading
    t = threading.Thread(target=start_keepalive_server, daemon=True)
    t.start()

    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(run_ws_monitor())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.exception("Main loop error: %s", e)
    finally:
        # ensure loop closed
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()
        logger.info("Stopped")

if __name__ == "__main__":
    logger.info("Starting futures-spread-bot (ccxt.pro event-based). Symbols: top-by-change mode=%s", TOP_BY_CHANGE)
    main()