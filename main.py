#!/usr/bin/env python3
"""
Spread parser + WS support to generate Telegram signals when cross-exchange spread appears.

Requires:
  pip install ccxt pandas numpy matplotlib pillow requests
"""

import os
import time
import json
import logging
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import ccxt
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import io
from PIL import Image
import requests

# ---------------- CONFIG ----------------
PORT = int(os.getenv("PORT", "5000"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
STATE_FILE = "spread_state.json"

EXCHANGES_TO_TRY = {
    "gateio": ["gateio", "gate"],
    "mexc": ["mexc", "mexc3", "mexc3p"],
    "hotcoin": ["hotcoin", "hotbit"],
    "lbank": ["lbank"]
}

SYMBOL_QUOTE = "USDT"
SPREAD_THRESHOLD = 0.01
MIN_EXCHANGES = 2
CHECK_INTERVAL_SEC = 30
THREADS = int(os.getenv("PARALLEL_WORKERS", "6"))
MAX_DEPTH = 5
PLOT_W, PLOT_H = 8, 4

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("spread_bot_ws.log"), logging.StreamHandler()]
)
logger = logging.getLogger("spread-bot-ws")

# ---------------- STATE ----------------
def load_state(path):
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.exception("load_state error: %s", e)
    return {"alerts": {}, "last_scan": None}

def save_state(path, data):
    try:
        with open(path + ".tmp", "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(path + ".tmp", path)
    except Exception as e:
        logger.exception("save_state error: %s", e)

state = load_state(STATE_FILE)

# ---------------- TELEGRAM ----------------
MARKDOWNV2_ESCAPE = r"_*[]()~`>#+-=|{}.!"

def escape_md_v2(text: str) -> str:
    return text and __import__("re").sub(f"([{__import__('re').escape(MARKDOWNV2_ESCAPE)}])", r"\\\1", str(text))

def send_telegram(text: str, photo_bytes: bytes = None, tries: int = 1):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.info("Telegram not configured.")
        return
    try:
        if photo_bytes:
            try:
                img = Image.open(io.BytesIO(photo_bytes))
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                buf.seek(0)
                files = {'photo': ('spread.png', buf, 'image/png')}
            except Exception as e:
                logger.warning("PIL fallback: %s", e)
                files = {'photo': ('spread.png', photo_bytes, 'image/png')}
            data = {'chat_id': CHAT_ID, 'caption': escape_md_v2(text), 'parse_mode': 'MarkdownV2'}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto", data=data, files=files, timeout=15)
        else:
            payload = {"chat_id": CHAT_ID, "text": escape_md_v2(text), "parse_mode": "MarkdownV2"}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=10)
    except requests.exceptions.ReadTimeout as e:
        logger.warning("Telegram timeout: %s", e)
        if tries > 0:
            time.sleep(2)
            send_telegram(text, photo_bytes, tries-1)
    except Exception as e:
        logger.exception("send_telegram error: %s", e)

# ---------------- EXCHANGE INIT ----------------
def init_exchanges():
    exmap = {}
    for friendly, ids in EXCHANGES_TO_TRY.items():
        for eid in ids:
            try:
                params = {}
                key = os.getenv(friendly.upper() + "_API_KEY") or os.getenv(eid.upper() + "_API_KEY")
                secret = os.getenv(friendly.upper() + "_API_SECRET") or os.getenv(eid.upper() + "_API_SECRET")
                if key and secret:
                    params = {"apiKey": key, "secret": secret}
                ex_cls = getattr(ccxt, eid, None)
                if ex_cls is None:
                    continue
                ex = ex_cls(params)
                ex.enableRateLimit = True
                # try load markets
                try:
                    ex.load_markets()
                except Exception as e:
                    logger.warning("load_markets failed for %s: %s", eid, e)
                exmap[friendly] = ex
                logger.info("Initialized exchange %s as ccxt id %s", friendly, eid)
                break
            except Exception as e:
                logger.debug("Failed init %s as %s: %s", eid, friendly, e)
    return exmap

EXCHANGES = init_exchanges()
if not EXCHANGES:
    logger.error("No exchanges initialized → exiting.")
    raise SystemExit(1)

# ---------------- WS ORDERBOOK CACHE ----------------
# Structure: exchange_name -> symbol -> {"bids":[], "asks":[], "timestamp":...}
ws_orderbooks = {ex: {} for ex in EXCHANGES.keys()}
ws_lock = threading.Lock()

# ccxt supports exchange.watchOrderBook for some exchanges (if asynchronous)
# but typical ccxt in sync mode doesn't. We implement fallback per exchange if possible.

def handle_ws_orderbook(exchange_name, msg):
    """
    msg expected format: {'symbol': ..., 'bids': [...], 'asks': [...], 'timestamp': ...}
    Called when WS gives new orderbook snapshot.
    """
    try:
        with ws_lock:
            symbol = msg.get("symbol")
            if symbol is None:
                return
            ws_orderbooks[exchange_name][symbol] = {
                "bids": msg.get("bids", []),
                "asks": msg.get("asks", []),
                "timestamp": msg.get("timestamp")
            }
    except Exception as e:
        logger.exception("handle_ws_orderbook error: %s", e)

def start_ws_for_exchange(name, ex):
    """
    Try to start WS for orderbook for that exchange via ccxt if supported.
    We use ccxt’s watchOrderBook (if available) in an async context.
    Fallback: no WS for this exchange.
    """
    if not getattr(ex, "watchOrderBook", None):
        logger.info("Exchange %s does not support watchOrderBook in ccxt → WS disabled", name)
        return

    def ws_thread():
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        async def watch_loop():
            # for each symbol in markets
            for sym in ex.symbols:
                # only USDT-quote ones
                if sym.endswith("/" + SYMBOL_QUOTE):
                    try:
                        # subscribe infinite
                        async for ob in ex.watch_order_book(sym, MAX_DEPTH):
                            # ob = {'symbol': sym, 'bids':[], 'asks':[], 'timestamp':...}
                            handle_ws_orderbook(name, ob)
                    except Exception as e:
                        logger.warning("WS watch failed %s @ %s: %s", sym, name, e)
                        await asyncio.sleep(1)
        try:
            loop.run_until_complete(watch_loop())
        except Exception as e:
            logger.exception("WS thread for %s ended: %s", name, e)
        finally:
            loop.close()

    t = threading.Thread(target=ws_thread, daemon=True)
    t.start()
    logger.info("Started WS thread for exchange %s", name)

# start WS for all exchanges that support it
for nm, ex in EXCHANGES.items():
    start_ws_for_exchange(nm, ex)

# ---------------- ORDERBOOK FETCH / SPREAD LOGIC ----------------
def get_orderbook_snapshot(exchange_name, symbol):
    """Return orderbook dict from ws cache or fallback to REST."""
    with ws_lock:
        exob = ws_orderbooks.get(exchange_name, {}).get(symbol)
        if exob:
            return {
                "bids": exob.get("bids", []),
                "asks": exob.get("asks", []),
                "timestamp": exob.get("timestamp")
            }
    # fallback via REST
    try:
        ex = EXCHANGES[exchange_name]
        ob = ex.fetch_order_book(symbol, limit=MAX_DEPTH)
        return {"bids": ob.get("bids", []), "asks": ob.get("asks", []), "timestamp": ob.get("timestamp")}
    except Exception as e:
        logger.debug("REST fallback failed for %s %s: %s", exchange_name, symbol, e)
        return None

def avg_price_of_side(side, n=MAX_DEPTH):
    """Compute weighted avg price for side = list of [price, qty]."""
    if not side:
        return None
    s = side[:n]
    num = sum(p[0] * p[1] for p in s)
    den = sum(p[1] for p in s)
    if den == 0:
        return None
    return num / den

def compute_cross_spread(symbol):
    # gather orderbooks
    obs = {}
    for nm in EXCHANGES.keys():
        ob = get_orderbook_snapshot(nm, symbol)
        if ob:
            bids = ob.get("bids", [])
            asks = ob.get("asks", [])
            avg_bid = avg_price_of_side(bids)
            avg_ask = avg_price_of_side(asks)
            if avg_bid is not None and avg_ask is not None:
                obs[nm] = {"avg_bid": avg_bid, "avg_ask": avg_ask}
    if len(obs) < MIN_EXCHANGES:
        return None
    best_ask = (None, None)
    best_bid = (None, None)
    for nm, v in obs.items():
        a = v.get("avg_ask")
        b = v.get("avg_bid")
        if a is not None:
            if best_ask[0] is None or a < best_ask[0]:
                best_ask = (a, nm)
        if b is not None:
            if best_bid[0] is None or b > best_bid[0]:
                best_bid = (b, nm)
    if best_ask[0] is None or best_bid[0] is None:
        return None
    spread_abs = best_bid[0] - best_ask[0]
    spread_frac = spread_abs / best_ask[0] if best_ask[0] != 0 else None
    return {
        "symbol": symbol,
        "best_ask_ex": best_ask[1], "best_ask": best_ask[0],
        "best_bid_ex": best_bid[1], "best_bid": best_bid[0],
        "spread_abs": spread_abs,
        "spread_frac": spread_frac,
        "obs": obs
    }

def make_plot(symbol, cross_info):
    obs = cross_info.get("obs", {})
    exchanges = list(obs.keys())
    bids = [obs[ex]["avg_bid"] for ex in exchanges]
    asks = [obs[ex]["avg_ask"] for ex in exchanges]
    mids = [(b + a) / 2.0 for b, a in zip(bids, asks)]
    x = list(range(len(exchanges)))

    fig, ax = plt.subplots(figsize=(PLOT_W, PLOT_H))
    ax.bar(x, mids, label="mid", alpha=0.6)
    ax.scatter(x, bids, marker="v", color="green", label="bid")
    ax.scatter(x, asks, marker="^", color="red", label="ask")

    # highlight best
    try:
        bi = exchanges.index(cross_info["best_bid_ex"])
        ai = exchanges.index(cross_info["best_ask_ex"])
        ax.scatter([bi], [cross_info["best_bid"]], s=120, edgecolors="black", facecolors="none", linewidths=1.2)
        ax.scatter([ai], [cross_info["best_ask"]], s=120, edgecolors="black", facecolors="none", linewidths=1.2)
    except Exception:
        pass

    ax.set_xticks(x)
    ax.set_xticklabels(exchanges, rotation=45, ha="right")
    ax.set_title(f"{symbol} cross-exchange spread")
    ax.grid(axis="y", alpha=0.3)
    ax.legend(fontsize=8)
    txt = (f"Bid {cross_info['best_bid']:.6f} @ {cross_info['best_bid_ex']}\n"
           f"Ask {cross_info['best_ask']:.6f} @ {cross_info['best_ask_ex']}\n"
           f"Spread {cross_info['spread_frac']*100:.2f}%")
    ax.text(0.98, 0.02, txt, transform=ax.transAxes, ha="right", va="bottom",
            bbox=dict(facecolor="white", alpha=0.7, boxstyle="round"))
    buf = io.BytesIO()
    plt.tight_layout()
    fig.savefig(buf, format="png", dpi=150)
    buf.seek(0)
    plt.close(fig)
    return buf.getvalue()

def send_if_spread(symbol):
    info = compute_cross_spread(symbol)
    if not info:
        return
    if info["spread_frac"] is None:
        return
    if info["spread_frac"] >= SPREAD_THRESHOLD and info["best_ask_ex"] != info["best_bid_ex"]:
        key = f"{symbol}:{info['best_ask_ex']}->{info['best_bid_ex']}"
        now = time.time()
        last = state.get("alerts", {}).get(key)
        cooldown = 60  # seconds
        if last and (now - last) < cooldown:
            logger.debug("Cooldown skip for %s", key)
            return
        msg = (f"🔔 *Spread Alert via WS*\n"
               f"Symbol: `{symbol}`\n"
               f"Buy (ask): `{info['best_ask']:.6f}` @ *{info['best_ask_ex']}*\n"
               f"Sell (bid): `{info['best_bid']:.6f}` @ *{info['best_bid_ex']}*\n"
               f"Spread: *{info['spread_frac']*100:.2f}%* ({info['spread_abs']:.6f})\n"
               f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
        for ex, v in info["obs"].items():
            msg += f"\n{ex}: bid={v['avg_bid']} ask={v['avg_ask']}"
        try:
            plot = make_plot(symbol, info)
        except Exception as e:
            logger.exception("Plot error: %s", e)
            plot = None
        send_telegram(msg, photo_bytes=plot)
        state.setdefault("alerts", {})[key] = now
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(STATE_FILE, state)
        logger.info("Sent spread alert %s spread=%.4f", symbol, info["spread_frac"])

def ws_monitor_loop():
    """
    Loop over symbols continuously from WS cache.
    """
    logger.info("WS monitor loop started")
    # build union of symbols from all exchanges
    syms = set()
    for ex in EXCHANGES.values():
        if hasattr(ex, "symbols"):
            for s in ex.symbols:
                if s.endswith("/" + SYMBOL_QUOTE):
                    syms.add(s)
    syms = list(syms)
    while True:
        for s in syms:
            try:
                send_if_spread(s)
            except Exception as e:
                logger.exception("WS monitor error for %s: %s", s, e)
        time.sleep(1)  # check high frequency

def rest_scan_loop():
    logger.info("REST scan loop started")
    # same symbol list
    syms = set()
    for ex in EXCHANGES.values():
        if hasattr(ex, "symbols"):
            for s in ex.symbols:
                if s.endswith("/" + SYMBOL_QUOTE):
                    syms.add(s)
    syms = list(syms)
    while True:
        for s in syms:
            try:
                send_if_spread(s)
            except Exception as e:
                logger.exception("REST scan error %s: %s", s, e)
        time.sleep(CHECK_INTERVAL_SEC)

# ---------------- FLASK-lite HTTP (for Render port binding) ----------------
def start_http_server():
    import http.server, socketserver
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, fmt, *args):
            logger.debug("HTTP: " + fmt % args)
        def do_GET(self):
            if self.path in ["/", "/status"]:
                resp = {
                    "time": datetime.now(timezone.utc).isoformat(),
                    "exchanges": list(EXCHANGES.keys()),
                    "alerts": list(state.get("alerts", {}).keys())[-10:]
                }
                data = json.dumps(resp, indent=2).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                return http.server.SimpleHTTPRequestHandler.do_GET(self)
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        logger.info("HTTP server listening on %d", PORT)
        httpd.serve_forever()

# ---------------- MAIN ----------------
if __name__ == "__main__":
    # start HTTP server thread
    t = threading.Thread(target=start_http_server, daemon=True)
    t.start()
    # start WS monitor thread
    tws = threading.Thread(target=ws_monitor_loop, daemon=True)
    tws.start()
    # also start REST fallback loop
    t2 = threading.Thread(target=rest_scan_loop, daemon=True)
    t2.start()

    logger.info("Spread WS bot started; exchanges: %s", list(EXCHANGES.keys()))
    # join threads
    t.join()
    tws.join()
    t2.join()