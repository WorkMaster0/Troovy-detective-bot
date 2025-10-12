#!/usr/bin/env python3
"""
Cross-exchange spread parser + Telegram alerts.

Features:
- Uses ccxt to fetch orderbooks from configured exchanges (Gate.io, MEXC, LBank, HotCoin(if supported))
- Filters leveraged / synthetic tickers (3L/3S/UP/DOWN/BEAR/BULL/PERP etc.)
- Normalizes pairs, checks base/quote match
- Computes spread = (best_bid - best_ask) / best_ask  (arbitrage potential)
- Filters out absurd spreads and false positives
- Sends Telegram alerts with orderbook snapshot (and retries on timeout)
- Config via environment variables
"""

import os
import time
import math
import json
import logging
import io
import re
from datetime import datetime, timezone
from typing import Dict, Tuple, List

import requests
from PIL import Image, ImageDraw, ImageFont

import ccxt
import pandas as pd

# ---------------- Logging ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("spread_bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger("spread-bot")

# ---------------- Config ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

# Exchanges to try via ccxt exchange id -> friendly name
EXCHANGE_IDS = {
    "gateio": "Gate.io",
    "mexc": "MEXC",
    "lbank": "LBank",
    # "hotcoin" may or may not be supported in your ccxt version.
    # Try "hotcoin" or "hotbit" etc. If not supported, it will be skipped.
    "hotcoin": "HotCoin"
}

# Pairs to monitor: pass via env as comma separated like "BTC/USDT,ETH/USDT"
PAIRS_ENV = os.getenv("SPREAD_PAIRS", "")
if PAIRS_ENV.strip():
    WATCH_PAIRS = [p.strip().upper() for p in PAIRS_ENV.split(",")]
else:
    # default: common large-cap pairs
    WATCH_PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT", "DOGE/USDT"]

# Spread threshold (relative): e.g. 0.01 = 1%
SPREAD_THRESHOLD = float(os.getenv("SPREAD_THRESHOLD", "0.01"))

# Absolute price sanity threshold (ignore microdust tokens if desired)
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.00005"))

# Max allowed multiplier between prices -- if exceeded, treat as suspicious (e.g. 100x)
MAX_PRICE_RATIO = float(os.getenv("MAX_PRICE_RATIO", "100.0"))

# How often check (seconds)
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SEC", "15"))

# Telegram request timeout and retry
TELEGRAM_TIMEOUT = 12
TELEGRAM_RETRIES = 2

# Debug mode
DEBUG = os.getenv("DEBUG", "false").lower() in ("1", "true", "yes")

# Regex: filter leveraged / synthetic tokens (common substrings)
LEVERAGED_RE = re.compile(r"(3S|3L|UP|DOWN|BEAR|BULL|PERP|ETF|SYNTH|LEVER|3X|-3X|-UP|-DOWN)$", re.IGNORECASE)

# Whitelist of allowed quote currencies (we only compare same quote)
ALLOWED_QUOTES = {"USDT", "USD", "BTC", "USDC", "BUSD"}

# Optional per-exchange symbol mapping overrides (if an exchange uses different notation)
# Example: {"gateio": {"BTC/USDT": "BTC_USDT"}} - ccxt usually handles mapping so not necessary
EXCHANGE_SYMBOL_OVERRIDES = {}

# Optional limit for orderbook depth to fetch
OB_LIMIT = 5

# ---------------- Utilities ----------------
def now_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def safe_float(x):
    try:
        return float(x)
    except Exception:
        return None

# ---------------- Telegram helper ----------------
MARKDOWNV2_ESCAPE = r"_*[]()~`>#+-=|{}.!"

def escape_md_v2(text: str) -> str:
    return re.sub(f"([{re.escape(MARKDOWNV2_ESCAPE)}])", r"\\\1", str(text))

def send_telegram(text: str, image_bytes: bytes = None, tries: int = TELEGRAM_RETRIES):
    """Send message + optional photo to Telegram with retry on timeout."""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.debug("Telegram credentials not set, skipping send.")
        return False
    try:
        headers = {"User-Agent": "spread-bot/1.0"}
        if image_bytes:
            # Try to process via PIL first (avoid ANTIALIAS warnings)
            try:
                img = Image.open(io.BytesIO(image_bytes))
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                buf.seek(0)
                files = {"photo": ("spread.png", buf, "image/png")}
            except Exception:
                files = {"photo": ("spread.png", io.BytesIO(image_bytes), "image/png")}
            data = {"chat_id": CHAT_ID, "caption": escape_md_v2(text), "parse_mode": "MarkdownV2"}
            resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                                 data=data, files=files, timeout=TELEGRAM_TIMEOUT, headers=headers)
        else:
            payload = {"chat_id": CHAT_ID, "text": escape_md_v2(text), "parse_mode": "MarkdownV2"}
            resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                 json=payload, timeout=TELEGRAM_TIMEOUT, headers=headers)
        resp.raise_for_status()
        return True
    except requests.exceptions.ReadTimeout as e:
        logger.warning("Telegram send timeout: %s", e)
        if tries > 0:
            time.sleep(1)
            return send_telegram(text, image_bytes, tries - 1)
        return False
    except Exception as e:
        logger.exception("send_telegram error: %s", e)
        return False

# ---------------- Image snapshot helper ----------------
def render_orderbook_image(title: str, orderbooks: Dict[str, Dict], width=800, height_per_exchange=120) -> bytes:
    """
    Renders a compact image showing best bids/asks per exchange and small lists.
    orderbooks: {exchange: {"bid": bid_price, "ask": ask_price, "bids": [(p,q)...], "asks":[...]}}
    """
    exchanges = list(orderbooks.keys())
    height = max(200, len(exchanges) * height_per_exchange)
    img = Image.new("RGB", (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    try:
        # choose a default font (system-dep). If fails, fallback to default PIL font.
        font = ImageFont.load_default()
    except Exception:
        font = None

    y = 10
    draw.text((10, y), title, fill=(0, 0, 0), font=font)
    y += 18
    for ex in exchanges:
        ob = orderbooks[ex]
        bid = ob.get("bid")
        ask = ob.get("ask")
        draw.text((10, y), f"{ex}  bid={bid if bid is not None else 'N/A'}  ask={ask if ask is not None else 'N/A'}", fill=(0, 0, 0), font=font)
        y += 14
        # show first 3 bids and asks
        bids = ob.get("bids", [])[:3]
        asks = ob.get("asks", [])[:3]
        draw.text((20, y), "BIDS:", fill=(0, 100, 0), font=font)
        bx = 80
        for p, q in bids:
            draw.text((bx, y), f"{p:.6f}@{q:.3f}", fill=(0, 100, 0), font=font)
            bx += 160
        y += 14
        draw.text((20, y), "ASKS:", fill=(139, 0, 0), font=font)
        ax = 80
        for p, q in asks:
            draw.text((ax, y), f"{p:.6f}@{q:.3f}", fill=(139, 0, 0), font=font)
            ax += 160
        y += 18
        draw.line((10, y, width - 10, y), fill=(220, 220, 220))
        y += 6

    # footer
    draw.text((10, height - 20), f"Generated: {now_utc_str()}", fill=(80, 80, 80), font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()

# ---------------- CCXT Exchange setup ----------------
def create_ccxt_exchanges(futures_mode=True) -> Dict[str, ccxt.Exchange]:
    exchanges = {}
    for ex_id, friendly in EXCHANGE_IDS.items():
        try:
            ex_class = getattr(ccxt, ex_id)
            api_key = os.getenv(f"{ex_id.upper()}_API_KEY", "") or os.getenv(f"{ex_id.upper()}_KEY", "")
            api_secret = os.getenv(f"{ex_id.upper()}_API_SECRET", "") or os.getenv(f"{ex_id.upper()}_SECRET", "")
            params = {"enableRateLimit": True}
            if api_key and api_secret:
                params["apiKey"] = api_key
                params["secret"] = api_secret

            exchange = ex_class(params)

            # === 🔥 ось головне: перемикач на futures/swap ===
            if futures_mode:
                if ex_id in ["gateio", "mexc", "lbank"]:
                    exchange.options["defaultType"] = "swap"
                elif ex_id in ["binance", "bybit", "okx"]:
                    exchange.options["defaultMarket"] = "future"

            exchange.load_markets()
            exchanges[ex_id] = exchange
            logger.info("Created ccxt exchange: %s (mode=%s)", ex_id, "futures" if futures_mode else "spot")

        except Exception as e:
            logger.warning("Could not init exchange %s (%s): %s", ex_id, friendly, e)
    return exchanges

# ---------------- Helpers for orderbook retrieval ----------------
def safe_fetch_orderbook(exchange: ccxt.Exchange, symbol: str, limit: int = OB_LIMIT):
    """
    Fetch orderbook via ccxt and normalize fields.
    Returns dict with keys: bid, ask, bids(list), asks(list)
    """
    try:
        ob = exchange.fetch_order_book(symbol, limit)
        bids = ob.get("bids", []) or []
        asks = ob.get("asks", []) or []
        best_bid = bids[0][0] if bids else None
        best_ask = asks[0][0] if asks else None
        # format lists to (price, amount)
        bids = [(float(p), float(q)) for p, q in bids]
        asks = [(float(p), float(q)) for p, q in asks]
        return {"bid": safe_float(best_bid), "ask": safe_float(best_ask), "bids": bids, "asks": asks}
    except ccxt.BadSymbol:
        logger.debug("%s does not have symbol %s", exchange.id, symbol)
        return None
    except Exception as e:
        logger.debug("Orderbook fetch failed for %s %s: %s", exchange.id, symbol, e)
        return None

# ---------------- Symbol normalization and validation ----------------
def is_leveraged(symbol: str) -> bool:
    """Return True if symbol likely refers to leveraged / synthetic token."""
    return bool(LEVERAGED_RE.search(symbol.replace("/", "").upper()))

def normalize_pair(pair: str) -> Tuple[str, str]:
    """Return (base, quote) for a pair string like 'BTC/USDT'."""
    parts = pair.replace("-", "/").replace("_", "/").split("/")
    if len(parts) >= 2:
        base = parts[0].upper()
        quote = parts[1].upper()
        return base, quote
    return pair.upper(), ""

def try_ccxt_symbol(exchange: ccxt.Exchange, pair: str) -> str:
    """
    Try to find correct ccxt symbol on exchange for a desired pair.
    ccxt.exchange.markets mapping used to find matching symbol.
    """
    target_base, target_quote = normalize_pair(pair)
    # if exchange has markets loaded, try direct match
    try:
        markets = exchange.load_markets()
    except Exception:
        markets = getattr(exchange, "markets", None) or {}
    # direct variations
    candidates = [
        f"{target_base}/{target_quote}",
        f"{target_base}/{target_quote.replace('USDC', 'USDT')}",
        f"{target_base}_{target_quote}",
        f"{target_base}{target_quote}",
    ]
    for c in candidates:
        if c in markets:
            return c
    # fallback: search through markets
    for m in markets.keys():
        try:
            b, q = normalize_pair(m)
        except Exception:
            continue
        if b == target_base and q == target_quote:
            return m
    # not found
    return None

# ---------------- Spread calculation ----------------
def compute_spread_snapshot(exchanges: Dict[str, ccxt.Exchange], pair: str) -> Dict:
    """
    For given pair (standard 'BASE/QUOTE'), fetch best bid/ask across exchanges
    and compute spread opportunities.
    Returns dict with snapshot info.
    """
    base, quote = normalize_pair(pair)
    if quote not in ALLOWED_QUOTES:
        # allow but log
        logger.debug("Pair %s uses non-standard quote %s", pair, quote)
    orderbooks = {}
    for ex_id, ex in exchanges.items():
        try:
            # first try to map pair to exchange's market symbol
            sym = try_ccxt_symbol(ex, pair)
            if not sym:
                logger.debug("Symbol %s not on %s", pair, ex_id)
                continue
            ob = safe_fetch_orderbook(ex, sym, limit=OB_LIMIT)
            if not ob:
                continue
            # sanity: price not None and > MIN_PRICE
            if (ob["bid"] is None or ob["ask"] is None):
                continue
            if ob["bid"] < MIN_PRICE and ob["ask"] < MIN_PRICE:
                # probably tiny alt or mismatch -> skip
                logger.debug("Skipping %s on %s due to price < MIN_PRICE", sym, ex_id)
                continue
            orderbooks[ex_id] = ob
        except Exception as e:
            logger.debug("Error fetching orderbook for %s on %s: %s", pair, ex_id, e)
    if not orderbooks:
        return {}

    # find best bid (max) and best ask (min)
    best_bid_ex, best_bid = None, -math.inf
    best_ask_ex, best_ask = None, math.inf
    for ex_id, ob in orderbooks.items():
        if ob["bid"] and ob["bid"] > best_bid:
            best_bid = ob["bid"]
            best_bid_ex = ex_id
        if ob["ask"] and ob["ask"] < best_ask:
            best_ask = ob["ask"]
            best_ask_ex = ex_id

    if best_bid_ex is None or best_ask_ex is None:
        return {}

    # Basic sanity checks
    # 1) symbols could be different assets (e.g. VET3S vs VET) -- detect via huge ratio
    if best_bid <= 0 or best_ask <= 0:
        return {}
    price_ratio = max(best_bid, best_ask) / min(best_bid, best_ask) if min(best_bid, best_ask) > 0 else float('inf')
    if price_ratio > MAX_PRICE_RATIO:
        # suspect mismatch (e.g. different assets like leveraged tokens); ignore
        logger.info("Suspicious price ratio for %s: ratio=%.2f (bid=%s@%s ask=%s@%s) -> skipping",
                    pair, price_ratio, best_bid, best_bid_ex, best_ask, best_ask_ex)
        return {}

    # compute spread metric (arbitrage: sell at bid, buy at ask)
    raw_diff = best_bid - best_ask
    spread_rel = raw_diff / best_ask if best_ask != 0 else 0.0

    snapshot = {
        "pair": pair,
        "orderbooks": orderbooks,
        "best_bid_ex": best_bid_ex,
        "best_bid": best_bid,
        "best_ask_ex": best_ask_ex,
        "best_ask": best_ask,
        "raw_diff": raw_diff,
        "spread_rel": spread_rel,
        "price_ratio": price_ratio,
        "timestamp": now_utc_str()
    }
    return snapshot

# ---------------- Main scanning logic ----------------
def run_scan_loop():
    exchanges = create_ccxt_exchanges()
    if not exchanges:
        logger.error("No exchanges available. Exiting.")
        return

    logger.info("Monitoring pairs: %s", ", ".join(WATCH_PAIRS))
    while True:
        try:
            for pair in WATCH_PAIRS:
                # Skip leveraged-looking pairs early
                if is_leveraged(pair):
                    if DEBUG:
                        logger.debug("Skipping leveraged/synthetic pair: %s", pair)
                    continue
                snap = compute_spread_snapshot(exchanges, pair)
                if not snap:
                    if DEBUG:
                        logger.debug("No valid snapshot for %s", pair)
                    continue
                # Check thresholds
                if snap["spread_rel"] >= SPREAD_THRESHOLD and snap["raw_diff"] > 0:
                    # Compose message and image
                    spread_pct = snap["spread_rel"] * 100
                    msg = (
                        f"🔔 *Spread Alert via Parser*\n"
                        f"Pair: `{pair}`\n"
                        f"Buy (ask): `{snap['best_ask']:.8f}` @ *{EXCHANGE_IDS.get(snap['best_ask_ex'], snap['best_ask_ex'])}*\n"
                        f"Sell (bid): `{snap['best_bid']:.8f}` @ *{EXCHANGE_IDS.get(snap['best_bid_ex'], snap['best_bid_ex'])}*\n"
                        f"Spread: *{spread_pct:.2f}%* ({snap['raw_diff']:.8f})\n"
                        f"Time: {snap['timestamp']}\n"
                    )
                    # small sanity re-check: ensure both prices reasonably close to median across exchanges
                    prices = [v["bid"] for v in snap["orderbooks"].values()] + [v["ask"] for v in snap["orderbooks"].values()]
                    prices = [p for p in prices if p and p > 0]
                    median_price = float(pd.Series(prices).median()) if prices else None
                    if median_price:
                        # if median differs from best_ask or best_bid by > MAX_PRICE_RATIO/4, warn
                        if (abs(snap["best_ask"] - median_price) / median_price > 0.9) or (abs(snap["best_bid"] - median_price) / median_price > 0.9):
                            logger.info("Median price check failed for %s: median=%.6f bid=%.6f ask=%.6f -> skipping", pair, median_price, snap["best_bid"], snap["best_ask"])
                            continue

                    # build image
                    try:
                        image = render_orderbook_image(f"{pair} spread {spread_pct:.2f}% ({snap['best_bid_ex']}->{snap['best_ask_ex']})", 
                                                       {EXCHANGE_IDS.get(k,k): v for k,v in snap["orderbooks"].items()})
                    except Exception as e:
                        logger.exception("render_orderbook_image failed: %s", e)
                        image = None

                    # send telegram
                    send_ok = send_telegram(msg, image)
                    if send_ok:
                        logger.info("Alert sent for %s: spread=%.4f (%.2f%%) buy@%s sell@%s", pair, snap["raw_diff"], spread_pct, snap["best_ask_ex"], snap["best_bid_ex"])
                    else:
                        logger.warning("Failed to send alert for %s", pair)
                else:
                    if DEBUG:
                        logger.debug("Pair %s no significant spread (%.6f)", pair, snap.get("spread_rel", 0))
            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            logger.info("Interrupted by user, exiting.")
            return
        except Exception as e:
            logger.exception("Main loop error: %s", e)
            time.sleep(5)

# ---------------- Entry ----------------
if __name__ == "__main__":
    logger.info("Starting Spread Parser bot")
    logger.info("Exchanges configured: %s", ", ".join(EXCHANGE_IDS.keys()))
    logger.info("Pairs configured: %s", ", ".join(WATCH_PAIRS))
    logger.info("SPREAD_THRESHOLD=%.4f MIN_PRICE=%.8f MAX_PRICE_RATIO=%.2f CHECK_INTERVAL=%ds", SPREAD_THRESHOLD, MIN_PRICE, MAX_PRICE_RATIO, CHECK_INTERVAL)
    run_scan_loop()