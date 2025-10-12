#!/usr/bin/env python3
"""
Futures spread scanner (ccxt.pro websocket)
- Exchanges: gateio, mexc, hotcoin, lbank (futures/swap mode)
- Event-based (watchTicker)
- Send Telegram alerts for spreads within thresholds (ignores >100%)
"""

import os
import asyncio
import time
import json
import logging
import math
from datetime import datetime, timezone
from typing import Dict, Any, List, Tuple
import requests
from PIL import Image
import io

# requires ccxt.pro
try:
    import ccxt.pro as ccxtpro
except Exception as e:
    raise RuntimeError("ccxt.pro is required. Install it with `pip install ccxtpro`") from e

# ---------------- logging ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("futures-spread-bot")

# ---------------- config from env ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

# Exchanges to use (ccxt ids)
EXCHANGE_IDS = ["gateio", "mexc", "hotcoin", "lbank"]

# Default symbols (3m futures-perp style). You can override via env SYMBOLS (comma-separated)
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "ADA/USDT",
    "XRP/USDT", "LTC/USDT", "DOT/USDT", "LINK/USDT", "AVAX/USDT"
]
SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", ",".join(DEFAULT_SYMBOLS)).split(",") if s.strip()]

# thresholds
MIN_SPREAD_PCT = float(os.getenv("MIN_SPREAD_PCT", "0.5"))  # minimum spread to alert (percent)
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "100.0"))  # do not show > 100%
MIN_DELTA_PCT = float(os.getenv("MIN_DELTA_PCT", "0.5"))  # minimal change vs last alert to resend (percent)
POLL_TIMEOUT = int(os.getenv("POLL_TIMEOUT", "20"))  # seconds for watchTicker timeout fallback

# dedupe
DEDUP_WINDOW = int(os.getenv("DEDUP_WINDOW", "30"))  # seconds to "cooldown" same pair if no significant change

# safety
MAX_RECONNECT_DELAY = 60

# ---------------- global runtime state ----------------
# latest_prices[symbol][exchange_id] = {"bid": float, "ask": float, "timestamp": ts}
latest_prices: Dict[str, Dict[str, Dict[str, Any]]] = {}
# last_sent[symbol] = {"buy_ex":..,"sell_ex":..,"spread":float,"time":ts}
last_sent: Dict[str, Dict[str, Any]] = {}

# ---------------- Telegram helpers ----------------
MARKDOWNV2_ESCAPE = r"_*[]()~`>#+-=|{}.!"

def escape_md_v2(text: str) -> str:
    return __import__("re").sub(f"([{__import__('re').escape(MARKDOWNV2_ESCAPE)}])", r"\\\1", str(text))

def send_telegram_message(text: str, photo_bytes: bytes = None, tries: int = 2):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.debug("Telegram not configured; skipping send.")
        return False
    try:
        if photo_bytes:
            # use PIL to re-save to avoid Pillow ANTIALIAS issue in some envs
            try:
                img = Image.open(io.BytesIO(photo_bytes))
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                buf.seek(0)
                files = {'photo': ('signal.png', buf, 'image/png')}
            except Exception:
                files = {'photo': ('signal.png', io.BytesIO(photo_bytes), 'image/png')}
            data = {'chat_id': CHAT_ID, 'caption': escape_md_v2(text), 'parse_mode': 'MarkdownV2'}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto", data=data, files=files, timeout=15)
        else:
            payload = {"chat_id": CHAT_ID, "text": escape_md_v2(text), "parse_mode": "MarkdownV2"}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=10)
        return True
    except requests.exceptions.ReadTimeout as e:
        logger.warning("Telegram timeout: %s", e)
        if tries > 0:
            time.sleep(1)
            return send_telegram_message(text, photo_bytes, tries-1)
        return False
    except Exception as e:
        logger.exception("send_telegram error: %s", e)
        return False

# ---------------- ccxt.pro exchanges factory ----------------
async def create_exchange_client(exchange_id: str):
    """
    Create ccxt.pro exchange instance in futures/swap mode.
    Uses env variables for API keys if present (EXCHANGEID_API_KEY, EXCHANGEID_API_SECRET).
    """
    api_key = os.getenv(f"{exchange_id.upper()}_API_KEY", "") or os.getenv(f"{exchange_id.upper()}_KEY", "")
    api_secret = os.getenv(f"{exchange_id.upper()}_API_SECRET", "") or os.getenv(f"{exchange_id.upper()}_SECRET", "")
    params = {"enableRateLimit": True}
    if api_key and api_secret:
        params['apiKey'] = api_key
        params['secret'] = api_secret
    try:
        ex_class = getattr(ccxtpro, exchange_id)
    except Exception:
        # fallback: dynamic creation
        ex_class = getattr(ccxtpro, exchange_id, None)
    if ex_class is None:
        raise RuntimeError(f"ccxt.pro has no exchange class for id '{exchange_id}'")
    exchange = ex_class(params)
    # try to set futures/swap mode
    try:
        # many exchanges accept defaultType = 'swap'
        exchange.options = getattr(exchange, 'options', {}) or {}
        exchange.options["defaultType"] = "swap"
    except Exception:
        logger.debug("Could not set defaultType for %s (may be fine)", exchange_id)
    try:
        await exchange.load_markets()
    except Exception as e:
        logger.warning("load_markets failed for %s: %s", exchange_id, e)
        # still return instance; some exchanges may lazy-load on watchTicker
    return exchange

# ---------------- symbol helper ----------------
def find_exchange_symbol(exchange, unified_symbol: str) -> str:
    """
    Try to map unified_symbol (e.g. 'BTC/USDT') to exchange-specific symbol variant.
    ccxt.pro usually supports unified symbols, but some markets might be named 'BTC/USDT:USDT' etc.
    Strategy: prefer exact match, else try same base/quote, else fallback to unified.
    """
    try:
        markets = getattr(exchange, "markets", None)
        if not markets:
            # no markets loaded yet
            return unified_symbol
        if unified_symbol in markets:
            return unified_symbol
        base, quote = unified_symbol.split("/")
        # try find a market with same base and quote in exchange.symbols
        for s in exchange.symbols:
            parts = s.split("/")
            if len(parts) >= 2 and parts[0].upper() == base.upper() and parts[1].upper().startswith(quote.upper()):
                return s
        # fallback
        return unified_symbol
    except Exception:
        return unified_symbol

# ---------------- ticker watching per exchange/symbol ----------------
async def watch_ticker_loop(exchange, exchange_id: str, symbol: str, shared_symbol: str):
    """
    Continuously watch ticker for a given exchange & symbol, update latest_prices on updates.
    This coroutine reconnects on failures.
    """
    mapped_symbol = find_exchange_symbol(exchange, symbol)
    reconnect_delay = 1
    while True:
        try:
            logger.debug("Watching %s:%s (mapped %s)", exchange_id, symbol, mapped_symbol)
            # watch_ticker yields dict with bid/ask
            ticker = await exchange.watch_ticker(mapped_symbol, params={})
            # ticker expected keys: bid, ask, timestamp, datetime, last...
            bid = float(ticker.get("bid") or 0.0)
            ask = float(ticker.get("ask") or 0.0)
            ts = ticker.get("timestamp") or int(time.time() * 1000)
            # push into global latest_prices
            latest_prices.setdefault(symbol, {})
            latest_prices[symbol][exchange_id] = {"bid": bid, "ask": ask, "timestamp": ts}
            # When a new tick arrives — process spread decision
            await process_spread_event(symbol)
            # reset reconnect_delay
            reconnect_delay = 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("watch_ticker error for %s on %s: %s", symbol, exchange_id, e)
            # exponential backoff
            await asyncio.sleep(min(reconnect_delay, MAX_RECONNECT_DELAY))
            reconnect_delay = min(MAX_RECONNECT_DELAY, reconnect_delay * 2)
            # attempt to reload markets
            try:
                await exchange.load_markets()
            except Exception:
                pass

# ---------------- spread calculation & alert ----------------
def compute_best_prices_for_symbol(symbol: str) -> Tuple[Tuple[str, float], Tuple[str, float]]:
    """
    Returns ((buy_ex, buy_price), (sell_ex, sell_price))
    buy = lowest ask (we buy there), sell = highest bid (we sell there)
    If insufficient data -> returns (None,None)
    """
    if symbol not in latest_prices:
        return (None, None)
    asks = []
    bids = []
    for ex_id, data in latest_prices[symbol].items():
        a = data.get("ask", 0.0)
        b = data.get("bid", 0.0)
        # sanity: require both > 0
        if a and a > 0:
            asks.append((ex_id, float(a)))
        if b and b > 0:
            bids.append((ex_id, float(b)))
    if not asks or not bids:
        return (None, None)
    # best buy = min ask
    best_buy = min(asks, key=lambda x: x[1])
    # best sell = max bid
    best_sell = max(bids, key=lambda x: x[1])
    return best_buy, best_sell

def pct(a, b):
    """percent change from a to b: (b-a)/a*100. caller ensures a>0"""
    return (b - a) / a * 100.0

def should_alert(symbol: str, buy_ex: str, buy_price: float, sell_ex: str, sell_price: float) -> Tuple[bool, float]:
    """
    Decide whether to alert based on thresholds and dedup rules.
    Returns (should_send, spread_pct)
    """
    if buy_price <= 0:
        return False, 0.0
    spread_pct = pct(buy_price, sell_price)  # how much higher sell is vs buy
    # ignore reversed or negative spreads
    if spread_pct <= 0:
        return False, spread_pct
    # ignore huge anomalies
    if spread_pct > MAX_SPREAD_PCT:
        return False, spread_pct
    # threshold
    if spread_pct < MIN_SPREAD_PCT:
        return False, spread_pct
    # dedupe: if last_sent exists, compare
    last = last_sent.get(symbol)
    now_ts = time.time()
    if last:
        same_pair = (last.get("buy_ex") == buy_ex and last.get("sell_ex") == sell_ex)
        prev_spread = float(last.get("spread", 0.0))
        # if same pair and change small -> ignore
        if same_pair and abs(spread_pct - prev_spread) < MIN_DELTA_PCT:
            # if still within cooldown window -> skip
            if now_ts - float(last.get("time", 0)) < DEDUP_WINDOW:
                return False, spread_pct
    return True, spread_pct

def craft_message(symbol: str, buy_ex: str, buy_price: float, sell_ex: str, sell_price: float, spread_pct: float) -> str:
    time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    msg = (
        f"🔔 *Spread Alert via WS*\n"
        f"Symbol: `{symbol}`\n"
        f"Buy (ask): `{buy_price:.8f}` @ *{buy_ex}*\n"
        f"Sell (bid): `{sell_price:.8f}` @ *{sell_ex}*\n"
        f"Spread: *{spread_pct:.2f}%* ({sell_price - buy_price:.8f})\n"
        f"Time: {time_str}\n"
    )
    return msg

# ---------------- optional simple text chart (no matplotlib) ----------------
def craft_small_textbook(symbol: str, buy_ex: str, buy_price: float, sell_ex: str, sell_price: float, spread_pct: float) -> bytes:
    """
    Create a tiny PNG with the price and arrows (PIL) to send to Telegram.
    Keep it simple to avoid heavy plotting libs.
    """
    from PIL import ImageDraw, ImageFont
    W, H = 640, 200
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)
    # fonts: default
    title = f"{symbol}  Spread: {spread_pct:.2f}%"
    draw.text((10, 10), title, fill="black")
    draw.text((10, 50), f"Buy @ {buy_ex}: {buy_price:.8f}", fill="green")
    draw.text((10, 90), f"Sell @ {sell_ex}: {sell_price:.8f}", fill="red")
    # arrows
    draw.line((450, 60, 580, 60), fill="green", width=4)
    draw.polygon([(580,60),(570,55),(570,65)], fill="green")
    draw.line((450, 100, 580, 100), fill="red", width=4)
    draw.polygon([(450,100),(460,95),(460,105)], fill="red")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()

# ---------------- process event when price updated ----------------
last_process_time = {}  # per symbol throttle not to spin

async def process_spread_event(symbol: str):
    """
    Called from ticker watchers whenever a new tick for symbol arrives.
    Compute best_buy & best_sell, decide, and alert if needed.
    """
    # small debounce: process at most once per 0.5s per symbol
    now = time.time()
    if symbol in last_process_time and (now - last_process_time[symbol]) < 0.4:
        return
    last_process_time[symbol] = now

    best_buy, best_sell = compute_best_prices_for_symbol(symbol)
    if not best_buy or not best_sell:
        return
    buy_ex, buy_price = best_buy
    sell_ex, sell_price = best_sell
    # if same exchange (no arbitrage) skip
    if buy_ex == sell_ex:
        return
    # sanity: prices must be positive and reasonable
    if buy_price <= 0 or sell_price <= 0:
        return

    should, spread_pct = should_alert(symbol, buy_ex, buy_price, sell_ex, sell_price)
    if not should:
        return

    # craft message and small image
    msg = craft_message(symbol, buy_ex, buy_price, sell_ex, sell_price, spread_pct)
    img = craft_small_textbook(symbol, buy_ex, buy_price, sell_ex, sell_price, spread_pct)

    # send and record
    ok = send_telegram_message(msg, photo_bytes := img)
    if ok:
        last_sent[symbol] = {"buy_ex": buy_ex, "sell_ex": sell_ex, "spread": spread_pct, "time": time.time()}
        logger.info("Alert sent for %s: buy %s@%f sell %s@%f spread=%.2f%%", symbol, buy_ex, buy_price, sell_ex, sell_price, spread_pct)
    else:
        logger.warning("Failed to send alert for %s", symbol)

# ---------------- orchestrator ----------------
async def run_ws_monitor():
    """
    Start exchange clients, spawn watch_ticker tasks for each (exchange,symbol).
    """
    # create exchange clients
    exchanges = {}
    for ex_id in EXCHANGE_IDS:
        try:
            exchanges[ex_id] = await create_exchange_client(ex_id)
            logger.info("Initialized exchange client: %s", ex_id)
        except Exception as e:
            logger.exception("Failed to init exchange %s: %s", ex_id, e)

    # validate which symbols are available on which exchanges (map)
    symbol_map: Dict[str, List[Tuple[str, str]]] = {s: [] for s in SYMBOLS}
    for s in SYMBOLS:
        for ex_id, ex in exchanges.items():
            try:
                mapped = find_exchange_symbol(ex, s)
                # test if market exists
                if hasattr(ex, "markets") and mapped in getattr(ex, "markets", {}):
                    symbol_map[s].append((ex_id, mapped))
                else:
                    # some exchanges lazily expose markets; we still try to subscribe later
                    symbol_map[s].append((ex_id, mapped))
            except Exception:
                logger.debug("Market check failed for %s on %s", s, ex_id)

    # start watch tasks
    tasks = []
    for s in SYMBOLS:
        for ex_id, ex in exchanges.items():
            # spawn task
            t = asyncio.create_task(watch_ticker_loop(ex, ex_id, s, s))
            tasks.append(t)
            # small sleep to avoid bursting connections
            await asyncio.sleep(0.05)

    # run until cancelled
    await asyncio.gather(*tasks)

# ---------------- entrypoint ----------------
def main():
    logger.info("Starting futures-spread-bot (ccxt.pro event-based). Symbols: %s", SYMBOLS)
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(run_ws_monitor())
    except KeyboardInterrupt:
        logger.info("Interrupted by user, shutting down.")
    except Exception as e:
        logger.exception("Fatal error in main: %s", e)
    finally:
        # try to close exchanges
        async def close_all():
            for ex_id in list(EXCHANGE_IDS):
                try:
                    ex = getattr(ccxtpro, ex_id, None)
                except Exception:
                    ex = None
            # ccxt.pro automatically closes clients on process end; nothing guaranteed here
        try:
            loop.run_until_complete(asyncio.sleep(0.1))
        except Exception:
            pass

if __name__ == "__main__":
    main()