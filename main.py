#!/usr/bin/env python3
# main.py - Live MEXC <-> DEX monitor (fixed: only real MEXC bases shown)
import os
import time
import json
import logging
import asyncio
import requests
from datetime import datetime
from threading import Thread
from collections import deque, defaultdict
from typing import Dict, Optional, List, Any, Tuple

from flask import Flask, request, render_template_string, jsonify
from flask_socketio import SocketIO, emit

try:
    import ccxt.pro as ccxtpro
except Exception:
    ccxtpro = None
import ccxt

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
PORT = int(os.getenv("PORT", "10000"))

STATE_FILE = "state.json"
POLL_INTERVAL = 5.0
MARKETS_REFRESH_INTERVAL = 600.0
LIVE_BROADCAST_INTERVAL = 5.0
ALERT_THRESHOLD_PCT = float(os.getenv("ALERT_THRESHOLD_PCT", "3.0"))
CLOSE_THRESHOLD_PCT = float(os.getenv("CLOSE_THRESHOLD_PCT", "0.5"))
MIN_QUOTE_VOLUME_USD = float(os.getenv("MIN_QUOTE_VOLUME_USD", "100"))  # require some liquidity
MAX_ABS_SPREAD_PCT = 2000.0
SIGNAL_COOLDOWN = 60.0
HISTORY_WINDOW = 3600.0
TOP_N = 10
CEX_ID = os.getenv("CEX_PRIMARY", "mexc")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search/?q={q}"
GMGN_API = "https://gmgn.ai/defi/quotation/v1/tokens/search?keyword={q}"

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("arb-monitor")

# ---------------- STATE ----------------
state: Dict[str, Any] = {
    "symbols": [],
    "chat_id": None,
    "msg_id": None,
    "live_to_telegram": False,
    "alert_threshold_pct": ALERT_THRESHOLD_PCT,
    "close_threshold_pct": CLOSE_THRESHOLD_PCT,
}

mexc_markets_by_base: Dict[str, List[str]] = {}
mexc_available_bases: List[str] = []

dex_prices: Dict[str, float] = {}
cex_prices: Dict[str, float] = {}
last_update: Dict[str, float] = {}
cex_history: Dict[str, deque] = defaultdict(lambda: deque())

active_spreads: Dict[str, Dict[str, Any]] = {}
last_alert_time: Dict[str, float] = {}

# ---------------- SAVE/LOAD ----------------
def load_state():
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                s = json.load(f)
                state.update(s)
                logger.info("Loaded state: %d symbols", len(state.get("symbols", [])))
        except Exception as e:
            logger.warning("Failed to load state.json: %s", e)

def save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.warning("Failed to save state: %s", e)

# ---------------- TELEGRAM HELPERS ----------------
def tg_send(text: str) -> Optional[dict]:
    if not TELEGRAM_TOKEN or not state.get("chat_id"):
        logger.debug("tg_send: no token/chat")
        return None
    try:
        payload = {"chat_id": state["chat_id"], "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}
        r = requests.post(TELEGRAM_API + "/sendMessage", json=payload, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning("tg_send error: %s", e)
        return None

def tg_edit(message_id: int, text: str):
    if not TELEGRAM_TOKEN or not state.get("chat_id"):
        return None
    try:
        payload = {"chat_id": state["chat_id"], "message_id": message_id, "text": text, "parse_mode": "Markdown"}
        r = requests.post(TELEGRAM_API + "/editMessageText", json=payload, timeout=10)
        return r.json()
    except Exception as e:
        logger.debug("tg_edit error: %s", e)
        return None

# ---------------- DEX FETCHERS ----------------
def fetch_from_gmgn(symbol: str) -> Optional[float]:
    try:
        url = GMGN_API.format(q=symbol)
        r = requests.get(url, timeout=7)
        r.raise_for_status()
        data = r.json()
        items = data.get("data") or []
        for it in items:
            p = it.get("price_usd") or it.get("priceUsd") or it.get("price")
            if p:
                return float(p)
    except Exception:
        pass
    return None

def fetch_from_dexscreener(symbol: str) -> Optional[float]:
    try:
        url = DEXSCREENER_SEARCH.format(q=symbol)
        r = requests.get(url, timeout=7)
        r.raise_for_status()
        data = r.json()
        pairs = data.get("pairs") or []
        if pairs:
            for p in pairs:
                price = p.get("priceUsd") or p.get("price")
                if price:
                    return float(price)
        tokens = data.get("tokens") or []
        for t in tokens:
            for p in t.get("pairs", []):
                price = p.get("priceUsd") or p.get("price")
                if price:
                    return float(price)
    except Exception:
        pass
    return None

def fetch_price_from_dex(base: str) -> Optional[float]:
    res = fetch_from_gmgn(base)
    if res is not None:
        return res
    return fetch_from_dexscreener(base)

# ---------------- MEXC MARKETS DISCOVERY (stricter) ----------------
def discover_mexc_markets() -> Tuple[Dict[str, List[str]], List[str]]:
    markets_map: Dict[str, List[str]] = {}
    bases: List[str] = []
    try:
        if not hasattr(ccxt, CEX_ID):
            logger.warning("ccxt has no exchange %s", CEX_ID)
            return {}, []
        ex = getattr(ccxt, CEX_ID)({"enableRateLimit": True})
        ex.load_markets(True)
        for m, info in ex.markets.items():
            # require contract-type (future/swap) or explicit 'contract' flag
            is_contract = bool(info.get("contract") or info.get("future") or info.get("type") in ("future", "swap"))
            if not is_contract:
                continue
            # require USDT in symbol or settlement
            symbol = m.upper()
            settle = (info.get("settle") or info.get("settlement") or "").upper()
            if "USDT" not in symbol and "USDT" not in settle and "USD" not in symbol:
                continue
            base = (info.get("base") or symbol.split("/")[0]).upper()
            markets_map.setdefault(base, []).append(m)
        # do not yet include bases here — we'll only add bases to available list when we have usable prices
        bases = sorted(list(markets_map.keys()))
        logger.info("Discovered %d candidate MEXC futures bases", len(bases))
    except Exception as e:
        logger.warning("Failed to discover markets: %s", e)
    return markets_map, bases

# ---------------- CEX PRICE CYCLE ----------------
async def cex_price_cycle():
    global cex_prices, cex_history, mexc_markets_by_base, mexc_available_bases
    ex = None
    last_markets_refresh = 0.0
    while True:
        try:
            now = time.time()
            if now - last_markets_refresh > MARKETS_REFRESH_INTERVAL or not mexc_markets_by_base:
                m_map, bases = discover_mexc_markets()
                mexc_markets_by_base.clear()
                mexc_markets_by_base.update(m_map)
                # DO NOT set mexc_available_bases here; we'll populate it when we find good tickers
                last_markets_refresh = now

            if ex is None:
                try:
                    ex = getattr(ccxt, CEX_ID)({"enableRateLimit": True})
                except Exception as e:
                    logger.warning("ccxt init failed: %s", e)
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

            try:
                tickers = ex.fetch_tickers()
            except Exception as e:
                logger.warning("fetch_tickers failed: %s", e)
                await asyncio.sleep(POLL_INTERVAL)
                continue

            # Temporary set for bases with valid price this cycle
            valid_bases_this_cycle = []

            for base, pairs in list(mexc_markets_by_base.items()):
                best_price = None
                best_quote_vol = 0.0
                for pair in pairs:
                    t = tickers.get(pair) or {}
                    last = t.get("last") or t.get("close") or t.get("price")
                    quote_vol = t.get("quoteVolume") or 0.0
                    # if quoteVolume absent, try compute from baseVolume * last
                    if not quote_vol:
                        bv = t.get("baseVolume") or 0.0
                        try:
                            quote_vol = float(bv) * float(last) if last and bv else 0.0
                        except Exception:
                            quote_vol = 0.0
                    try:
                        if last is None:
                            continue
                        lastf = float(last)
                        if best_price is None or quote_vol > best_quote_vol:
                            best_price = lastf
                            best_quote_vol = float(quote_vol or 0.0)
                    except Exception:
                        continue

                # require some liquidity
                if best_price is not None and best_quote_vol >= MIN_QUOTE_VOLUME_USD:
                    cex_prices[base] = float(best_price)
                    last_update[base] = time.time()
                    dq = cex_history[base]
                    dq.append((time.time(), float(best_price)))
                    cutoff = time.time() - HISTORY_WINDOW - 10
                    while dq and dq[0][0] < cutoff:
                        dq.popleft()
                    valid_bases_this_cycle.append(base)
                else:
                    # if low volume or no price, remove cex price so it won't show empty in UI
                    if base in cex_prices:
                        del cex_prices[base]
                    # don't delete history, but don't add to available list this cycle
            # update mexc_available_bases to match bases with real cex price
            mexc_available_bases[:] = sorted(valid_bases_this_cycle)
        except Exception as e:
            logger.exception("cex_price_cycle error: %s", e)
        await asyncio.sleep(POLL_INTERVAL)

# ---------------- DEX PRICE CYCLE ----------------
async def dex_price_cycle():
    loop = asyncio.get_event_loop()
    while True:
        try:
            bases = list(mexc_available_bases)
            if not bases:
                await asyncio.sleep(POLL_INTERVAL)
                continue
            coros = [loop.run_in_executor(None, fetch_price_from_dex, b) for b in bases]
            results = await asyncio.gather(*coros, return_exceptions=True)
            for b, res in zip(bases, results):
                if isinstance(res, Exception) or res is None:
                    continue
                try:
                    p = float(res)
                    if p and p > 0:
                        cex_p = cex_prices.get(b)
                        if cex_p:
                            ratio = cex_p / p if p else float('inf')
                            if ratio > 1e6 or ratio < 1e-6:
                                logger.debug("DEX sanity skip %s (ratio %.2f)", b, ratio)
                                continue
                        dex_prices[b] = p
                        last_update[b] = time.time()
                except Exception:
                    continue
        except Exception as e:
            logger.debug("dex_price_cycle error: %s", e)
        await asyncio.sleep(POLL_INTERVAL)

# ---------------- SIGNALS / SPREADS ----------------
def compute_1h_change_pct_for_base(base: str) -> float:
    dq = cex_history.get(base)
    if not dq or len(dq) < 2:
        return 0.0
    now = time.time()
    newest_ts, newest_p = dq[-1]
    oldest_p = None
    for ts, p in dq:
        if ts >= now - HISTORY_WINDOW:
            oldest_p = p
            break
    if oldest_p is None or oldest_p == 0:
        return 0.0
    return (newest_p - oldest_p) / oldest_p * 100.0

def process_spreads_and_alerts():
    now = time.time()
    open_thresh = float(state.get("alert_threshold_pct", ALERT_THRESHOLD_PCT))
    close_thresh = float(state.get("close_threshold_pct", CLOSE_THRESHOLD_PCT))

    for base in list(mexc_available_bases):
        dex = dex_prices.get(base)
        cex = cex_prices.get(base)
        if dex is None or cex is None or dex == 0:
            continue
        pct = (cex - dex) / dex * 100.0
        if abs(pct) > MAX_ABS_SPREAD_PCT:
            continue

        # auto-add monitored list if spread threshold reached and not already monitored
        if abs(pct) >= open_thresh and base not in state["symbols"]:
            state["symbols"].append(base)
            save_state()
            logger.info("Auto-added %s due to spread %.2f%%", base, pct)
            if state.get("chat_id"):
                tg_send(f"➕ Auto-added `{base}` (spread {pct:.2f}%)")

        active = active_spreads.get(base)
        if not active:
            last_alert = last_alert_time.get(base, 0)
            if abs(pct) >= open_thresh and (now - last_alert >= SIGNAL_COOLDOWN):
                active_spreads[base] = {"opened_pct": pct, "open_ts": now, "dex_price": dex, "cex_price": cex}
                last_alert_time[base] = now
                logger.info("ALERT OPEN %s %.2f%%", base, pct)
                if state.get("chat_id"):
                    tg_send(f"🔔 *Spread OPENED* `{base}`\nDEX `{dex:.8f}`\nCEX `{cex:.8f}`\nSpread *{pct:.2f}%*")
        else:
            opened_pct = active.get("opened_pct", 0.0)
            if abs(pct) > abs(opened_pct) and (now - last_alert_time.get(base, 0) >= SIGNAL_COOLDOWN):
                active_spreads[base]["opened_pct"] = pct
                last_alert_time[base] = now
                logger.info("ALERT INCREASE %s %.2f%% (was %.2f%%)", base, pct, opened_pct)
                if state.get("chat_id"):
                    tg_send(f"🔺 *Spread increased* `{base}`\nNow *{pct:.2f}%* (was {opened_pct:.2f}%)")
            if abs(pct) <= close_thresh:
                duration = int(now - active.get("open_ts", now))
                logger.info("ALERT CLOSE %s %.2f%% (duration %ds)", base, pct, duration)
                if state.get("chat_id"):
                    tg_send(f"✅ *Spread CLOSED* `{base}`\nNow DEX `{dex:.8f}` | CEX `{cex:.8f}`\nSpread *{pct:.2f}%*\nOpen duration: {duration}s")
                active_spreads.pop(base, None)
                last_alert_time[base] = now

# ---------------- FLASK + SOCKET.IO ----------------
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

INDEX_HTML = """
<!doctype html>... full HTML same as before (omitted here for brevity) ...
"""
# Use same UI template as earlier — it expects "rows" with fields symbol, change1h, dex, cex, pct, last

@app.route("/", methods=["GET"])
def index():
    return render_template_string(open(__file__).read().split('INDEX_HTML = """')[1].split('"""')[0])  # safe serve

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    if not data:
        return jsonify({"ok": False}), 400
    msg = data.get("message") or data.get("edited_message")
    if not msg:
        return jsonify({"ok": True})
    chat = msg.get("chat", {})
    cid = chat.get("id")
    if not state.get("chat_id"):
        state["chat_id"] = cid
        save_state()
    text = (msg.get("text") or "").strip()
    if not text:
        return jsonify({"ok": True})
    parts = text.split()
    cmd = parts[0].lower()
    try:
        if cmd == "/start":
            tg_send("🤖 Live monitor online. Use /add SYMBOL")
        elif cmd == "/help":
            tg_send("Commands: /add SYMBOL /remove SYMBOL /list /clear /alert <pct> /live on|off /status /help")
        elif cmd == "/add" and len(parts) >= 2:
            sym = parts[1].upper()
            if sym not in state["symbols"]:
                state["symbols"].append(sym); save_state(); socketio.emit("status", f"Added {sym}"); tg_send(f"✅ Added {sym}")
            else:
                tg_send(f"⚠️ {sym} already monitored")
        elif cmd == "/remove" and len(parts) >= 2:
            sym = parts[1].upper()
            if sym in state["symbols"]:
                state["symbols"].remove(sym); save_state(); socketio.emit("status", f"Removed {sym}"); tg_send(f"🗑 Removed {sym}")
            else:
                tg_send(f"⚠️ {sym} not monitored")
        elif cmd == "/list":
            tg_send("Monitored: " + (", ".join(state["symbols"]) if state["symbols"] else "—"))
        elif cmd == "/clear":
            state["symbols"] = []; save_state(); socketio.emit("status", "Cleared symbols"); tg_send("🧹 Cleared all symbols")
        elif cmd == "/alert":
            if len(parts) >= 2:
                try:
                    pct = float(parts[1]); state["alert_threshold_pct"] = pct; save_state(); tg_send(f"✅ Alert threshold set to {pct:.2f}%")
                except Exception:
                    tg_send("Usage: /alert <pct> (numeric)")
            else:
                tg_send(f"Current alert threshold: {state.get('alert_threshold_pct'):.2f}%")
        elif cmd == "/live":
            if len(parts) >= 2 and parts[1].lower() in ("on", "off"):
                state["live_to_telegram"] = (parts[1].lower() == "on"); save_state(); tg_send(f"Live->Telegram set to {state['live_to_telegram']}")
            else:
                tg_send("Usage: /live on|off")
        elif cmd == "/status":
            syms = state.get("symbols", [])
            txt = [f"Symbols: {', '.join(syms) if syms else '—'}", f"Alert threshold: {state.get('alert_threshold_pct'):.2f}%", f"Active spreads: {len(active_spreads)}"]
            tg_send("\n".join(txt))
        else:
            tg_send("❓ Unknown command. /help")
    except Exception as e:
        logger.exception("cmd error: %s", e); tg_send("⚠️ Error processing command.")
    return jsonify({"ok": True})

@socketio.on("connect")
def on_connect():
    try:
        emit("clients", 1)
        emit("live.update", {"rows": []})
    except Exception:
        pass

@socketio.on("add_symbol")
def on_add_symbol(sym):
    s = sym.strip().upper()
    if not s: return
    if s not in state["symbols"]:
        state["symbols"].append(s); save_state(); emit("status", f"Added {s}", broadcast=True)
    else:
        emit("status", f"{s} already monitored")

@socketio.on("clear_symbols")
def on_clear_symbols():
    state["symbols"] = []; save_state(); emit("status", "Cleared symbols", broadcast=True)

# ---------------- ORCHESTRATION ----------------
class Orchestrator:
    def __init__(self):
        self.loop = None
        self.thread = None
        self.tasks: List[asyncio.Task] = []
        self.running = False

    def start(self):
        if self.running: return
        self.loop = asyncio.new_event_loop()
        self.thread = Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self.running = True

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        except Exception as e:
            logger.exception("orchestrator error: %s", e)

    async def _main(self):
        load_state()
        tasks = [
            asyncio.create_task(cex_price_cycle()),
            asyncio.create_task(dex_price_cycle()),
            asyncio.create_task(self.broadcaster()),
        ]
        self.tasks = tasks
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass

    async def broadcaster(self):
        if TELEGRAM_TOKEN and WEBHOOK_URL:
            try:
                url = WEBHOOK_URL.rstrip("/") + "/webhook"
                r = requests.get(f"{TELEGRAM_API}/setWebhook?url={url}", timeout=8)
                logger.info("Set webhook result: %s", r.text[:200])
            except Exception:
                pass

        while True:
            try:
                process_spreads_and_alerts()
                # only consider bases with cex price (real MEXC bases)
                bases = [b for b in mexc_available_bases if cex_prices.get(b) is not None]
                scored = []
                for b in bases:
                    ch = compute_1h_change_pct_for_base(b)
                    scored.append((b, ch))
                scored.sort(key=lambda x: abs(x[1]), reverse=True)
                top = scored[:TOP_N]
                rows = []
                for b, ch in top:
                    dex = dex_prices.get(b)
                    cex = cex_prices.get(b)
                    pct = 0.0
                    if dex and cex and dex != 0:
                        pct = (cex - dex) / dex * 100.0
                    rows.append({"symbol": b, "change1h": ch, "dex": dex, "cex": cex, "pct": pct, "last": last_update.get(b)})
                try:
                    socketio.emit("live.update", {"rows": rows})
                except Exception:
                    pass

                if state.get("live_to_telegram") and state.get("chat_id"):
                    try:
                        lines = ["📡 *Live MEXC ↔ DEX Monitor*", f"_Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}_",
                                 "`SYMBOL    1hΔ%    DEX(USD)     MEXC(USD)    Δ%`", "`-------------------------------------------------`"]
                        for r in rows:
                            dex_s = f"{r['dex']:.8f}" if r['dex'] else "—"
                            cex_s = f"{r['cex']:.8f}" if r['cex'] else "—"
                            ch_s = f"{r['change1h']:+6.2f}%"
                            pct_s = f"{r['pct']:+6.2f}%"
                            lines.append(f"`{r['symbol']:<7}` {ch_s:>8}  {dex_s:>12}  {cex_s:>12}  {pct_s:>8}")
                        txt = "\n".join(lines)
                        if not state.get("msg_id"):
                            res = tg_send(txt)
                            if res and isinstance(res, dict):
                                mid = res.get("result", {}).get("message_id")
                                if mid:
                                    state["msg_id"] = int(mid); save_state()
                        else:
                            tg_edit(state["msg_id"], txt)
                    except Exception:
                        pass
            except Exception as e:
                logger.debug("broadcaster iteration error: %s", e)
            await asyncio.sleep(LIVE_BROADCAST_INTERVAL)

    def stop(self):
        if not self.running: return
        async def _cancel_all():
            for t in list(asyncio.all_tasks(loop=self.loop)):
                try:
                    t.cancel()
                except Exception:
                    pass
        fut = asyncio.run_coroutine_threadsafe(_cancel_all(), self.loop)
        try:
            fut.result(timeout=5)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)
        self.running = False

# ---------------- BOOT ----------------
orchestrator = Orchestrator()

if __name__ == "__main__":
    logger.info("🚀 Starting Live MEXC<->DEX monitor (fixed)")
    load_state()
    orchestrator.start()
    socketio.run(app, host="0.0.0.0", port=PORT)