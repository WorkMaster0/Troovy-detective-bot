#!/usr/bin/env python3
# main.py - Live DEX <-> CEX monitor with history + Top 10 spreads
import os
import time
import json
import logging
import asyncio
import requests
from datetime import datetime
from threading import Thread
from typing import Dict, Any, List, Optional
from flask import Flask, request, render_template_string, jsonify
from flask_socketio import SocketIO, emit

# optional ccxt.pro for CEX websocket; fallback to ccxt REST
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
POLL_INTERVAL_DEX = float(os.getenv("POLL_INTERVAL_DEX", "3.0"))
LIVE_BROADCAST_INTERVAL = float(os.getenv("LIVE_BROADCAST_INTERVAL", "2.0"))
ALERT_THRESHOLD_PCT = float(os.getenv("ALERT_THRESHOLD_PCT", "2.0"))
CLOSE_THRESHOLD_PCT = float(os.getenv("CLOSE_THRESHOLD_PCT", "0.5"))
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "80"))
MAX_HISTORY = 50
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

CEX_PRIMARY = os.getenv("CEX_PRIMARY", "mexc")
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search/?q={q}"
GMGN_API = "https://gmgn.ai/defi/quotation/v1/tokens/search?keyword={q}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("live-monitor")

# ---------------- STATE ----------------
state: Dict[str, Any] = {
    "symbols": [],
    "chat_id": None,
    "msg_id": None,
    "monitoring": True,
    "alert_threshold_pct": ALERT_THRESHOLD_PCT,
    "close_threshold_pct": CLOSE_THRESHOLD_PCT,
    "live_to_telegram": False,
}

dex_prices: Dict[str, float] = {}
cex_prices: Dict[str, float] = {}
last_update: Dict[str, float] = {}
price_history: Dict[str, List[float]] = {}
active_spreads: Dict[str, Dict[str, Any]] = {}
last_alert_time: Dict[str, float] = {}

# ---------------- SAVE / LOAD ----------------
def load_state():
    global state
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                s = json.load(f)
                state.update(s)
                logger.info("Loaded state: %d symbols", len(state.get("symbols", [])))
    except Exception as e:
        logger.exception("load_state error: %s", e)

def save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.exception("save_state error: %s", e)

# ---------------- TELEGRAM ----------------
def tg_send(text: str) -> Optional[dict]:
    if not TELEGRAM_TOKEN or not state.get("chat_id"):
        return None
    try:
        payload = {"chat_id": state["chat_id"], "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}
        r = requests.post(TELEGRAM_API + "/sendMessage", json=payload, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.exception("tg_send error: %s", e)
        return None

def tg_edit(message_id: int, text: str):
    if not TELEGRAM_TOKEN or not state.get("chat_id"):
        return None
    try:
        payload = {"chat_id": state["chat_id"], "message_id": message_id, "text": text, "parse_mode": "Markdown"}
        r = requests.post(TELEGRAM_API + "/editMessageText", json=payload, timeout=10)
        return r.json()
    except Exception as e:
        logger.exception("tg_edit error: %s", e)
        return None

# ---------------- UTIL ----------------
def fetch_from_gmgn(symbol: str) -> Optional[float]:
    try:
        url = GMGN_API.format(q=symbol.upper())
        r = requests.get(url, timeout=7)
        r.raise_for_status()
        data = r.json()
        for item in data.get("data") or []:
            price = item.get("price_usd") or item.get("priceUsd") or item.get("price")
            if price:
                return float(price)
    except:
        return None
    return None

def fetch_from_dexscreener(symbol: str) -> Optional[float]:
    try:
        url = DEXSCREENER_SEARCH.format(q=symbol.upper())
        r = requests.get(url, timeout=7)
        r.raise_for_status()
        data = r.json()
        pairs = data.get("pairs") or []
        for p in pairs:
            price = p.get("priceUsd") or p.get("price")
            if price: return float(price)
        tokens = data.get("tokens") or []
        for t in tokens:
            for p in t.get("pairs", []):
                price = p.get("priceUsd") or p.get("price")
                if price: return float(price)
    except:
        return None
    return None

def fetch_price_from_dex(symbol: str) -> Optional[float]:
    return fetch_from_gmgn(symbol) or fetch_from_dexscreener(symbol)

# ---------------- CEX watcher ----------------
class CEXWatcher:
    def __init__(self, exchange_id="mexc"):
        self.exchange_id = exchange_id
        self.client = None
        self.task = None
        self.running = False

    async def start(self):
        if self.running: return
        try:
            self.client = getattr(ccxt, self.exchange_id)({"enableRateLimit": True})
        except Exception:
            self.client = None
        self.running = True
        self.task = asyncio.create_task(self._run())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try: await self.task
            except: pass
        self.task = None
        self.client = None

    async def _run(self):
        while self.running:
            syms = list(state.get("symbols", []))[:MAX_SYMBOLS]
            if not syms or not self.client:
                await asyncio.sleep(1.0)
                continue
            try:
                all_tickers = self.client.fetch_tickers()
            except:
                await asyncio.sleep(2.0)
                continue
            for s in syms:
                s_upper = s.upper()
                for pair, ticker in all_tickers.items():
                    if s_upper in pair:
                        last = ticker.get("last") or ticker.get("close") or ticker.get("price")
                        if last:
                            cex_prices[s] = float(last)
                            break
            await asyncio.sleep(2.0)

# ---------------- DEX poller ----------------
class DexPoller:
    def __init__(self):
        self.task = None
        self.running = False

    async def start(self):
        if self.running: return
        self.running = True
        self.task = asyncio.create_task(self._run())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try: await self.task
            except: pass
        self.task = None

    async def _run(self):
        loop = asyncio.get_event_loop()
        while self.running:
            syms = list(state.get("symbols", []))[:MAX_SYMBOLS]
            if not syms:
                await asyncio.sleep(1.0)
                continue
            coros = [loop.run_in_executor(None, fetch_price_from_dex, s) for s in syms]
            results = await asyncio.gather(*coros, return_exceptions=True)
            for s, res in zip(syms, results):
                if isinstance(res, Exception) or res is None: continue
                dex_prices[s] = float(res)
                last_update[s] = time.time()
                # maintain price history
                price_history.setdefault(s, [])
                price_history[s].append(res)
                if len(price_history[s]) > MAX_HISTORY:
                    price_history[s] = price_history[s][-MAX_HISTORY:]
            await asyncio.sleep(POLL_INTERVAL_DEX)

# ---------------- SPREAD & ALERT ----------------
def process_spread(sym: str):
    dex = dex_prices.get(sym)
    cex = cex_prices.get(sym)
    if dex is None or cex is None or dex == 0: return
    pct = (cex - dex) / dex * 100.0
    now = time.time()
    open_thresh = float(state.get("alert_threshold_pct", ALERT_THRESHOLD_PCT))
    close_thresh = float(state.get("close_threshold_pct", CLOSE_THRESHOLD_PCT))
    if sym not in active_spreads and pct >= open_thresh:
        active_spreads[sym] = {"opened_pct": pct, "open_ts": now, "dex_price": dex, "cex_price": cex}
        last_alert_time[sym] = now
        tg_send(f"🔔 *Spread OPENED*\n`{sym}` DEX `{dex:.8f}` | CEX `{cex:.8f}` Δ*{pct:.2f}%*")
    elif sym in active_spreads and pct <= close_thresh:
        opened = active_spreads[sym]
        duration = now - opened.get("open_ts", now)
        tg_send(f"✅ *Spread CLOSED*\n`{sym}` Δ*{pct:.2f}%*, was {opened.get('opened_pct'):.2f}%, duration {int(duration)}s")
        active_spreads.pop(sym, None)
        last_alert_time[sym] = now

# ---------------- BUILD TOP 10 ----------------
def build_live_table_text() -> str:
    syms = list(state.get("symbols", []))
    if not syms: return "🟡 *No symbols monitored.* Use `/add SYMBOL` to add."
    # compute absolute % change from first in history
    top_list = []
    for s in syms:
        hist = price_history.get(s, [])
        if len(hist) >= 2:
            change = (hist[-1] - hist[0]) / hist[0] * 100
            top_list.append((abs(change), s))
        else:
            top_list.append((0, s))
    top_list.sort(reverse=True)
    top_syms = [s for _, s in top_list[:10]]

    lines = []
    lines.append("📡 *Live DEX ↔ CEX Monitor (Top 10 by change)*")
    lines.append(f"_Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}_\n")
    lines.append("`SYMBOL    DEX(USD)      CEX(USD)     Δ%   Last`")
    lines.append("`-----------------------------------------------------------`")
    for s in top_syms:
        dex = dex_prices.get(s)
        cex = cex_prices.get(s)
        dex_str = f"{dex:.8f}" if dex else "—"
        cex_str = f"{cex:.8f}" if cex else "—"
        pct_str = "—"
        if dex and cex and dex != 0:
            pct = (cex - dex)/dex*100
            pct_str = f"{pct:+6.2f}%"
        lu = last_update.get(s)
        lu_str = datetime.utcfromtimestamp(lu).strftime("%H:%M:%S") if lu else "—"
        lines.append(f"`{s:<7}` {dex_str:>12}  {cex_str:>12}  {pct_str:>7}  {lu_str}")
    lines.append("\n`/add SYMBOL  /remove SYMBOL  /list  /alert <pct>  /live on|off`")
    return "\n".join(lines)

# ---------------- FLASK ----------------
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

@app.route("/", methods=["GET"])
def index():
    return render_template_string("<h3>Live DEX ↔ CEX Monitor (open SocketIO)</h3>")

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    msg = data.get("message") or data.get("edited_message")
    if not msg: return jsonify({"ok": True})
    chat = msg.get("chat", {})
    cid = chat.get("id")
    if not state.get("chat_id"): state["chat_id"] = cid; save_state()
    text = (msg.get("text") or "").strip()
    if not text: return jsonify({"ok": True})
    parts = text.split(); cmd = parts[0].lower()
    try:
        if cmd == "/add" and len(parts)>=2:
            s = parts[1].upper()
            if s not in state["symbols"]:
                state["symbols"].append(s)
                save_state()
                socketio.emit("status", f"Added {s}")
                tg_send(f"✅ Added {s}")
            else: tg_send(f"⚠️ {s} already monitored")
        elif cmd == "/remove" and len(parts)>=2:
            s = parts[1].upper()
            if s in state["symbols"]:
                state["symbols"].remove(s)
                save_state()
                socketio.emit("status", f"Removed {s}")
                tg_send(f"🗑 Removed {s}")
            else: tg_send(f"⚠️ {s} not monitored")
        elif cmd == "/list":
            tg_send("Monitored: " + (", ".join(state["symbols"]) or "—"))
        elif cmd == "/clear":
            state["symbols"] = []
            save_state()
            socketio.emit("status", "Cleared symbols")
            tg_send("🧹 Cleared all symbols")
        elif cmd == "/alert" and len(parts)>=2:
            try:
                pct = float(parts[1])
                state["alert_threshold_pct"] = pct
                save_state()
                tg_send(f"✅ Alert threshold set to {pct:.2f}%")
            except:
                tg_send("Usage: /alert <pct>")
        elif cmd == "/live" and len(parts)>=2:
            state["live_to_telegram"] = parts[1].lower()=="on"
            save_state()
            tg_send(f"Live-to-Telegram: {state['live_to_telegram']}")
    except Exception as e:
        logger.exception(e)
        tg_send("⚠️ Error processing command.")
    return jsonify({"ok": True})

@socketio.on("connect")
def on_connect():
    emit("live.update", {"symbols": state.get("symbols", []), "dex_prices": dex_prices, "cex_prices": cex_prices, "last_update": last_update})

# ---------------- ORCHESTRATOR ----------------
class Orchestrator:
    def __init__(self):
        self.loop = None
        self.thread = None
        self.cex = CEXWatcher(CEX_PRIMARY)
        self.dex = DexPoller()
        self.running = False

    def start(self):
        if self.running: return
        self.loop = asyncio.new_event_loop()
        self.thread = Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self.running = True

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    async def _main(self):
        load_state()
        await self.cex.start()
        await self.dex.start()
        while True:
            for s in list(state.get("symbols", []))[:MAX_SYMBOLS]:
                process_spread(s)
            socketio.emit("live.update", {"symbols": state.get("symbols", []), "dex_prices": dex_prices, "cex_prices": cex_prices, "last_update": last_update})
            if state.get("live_to_telegram") and state.get("chat_id"):
                txt = build_live_table_text()
                if not state.get("msg_id"):
                    res = tg_send(txt)
                    if res:
                        mid = res.get("result", {}).get("message_id")
                        if mid:
                            state["msg_id"] = int(mid)
                            save_state()
                else:
                    tg_edit(state["msg_id"], txt)
            await asyncio.sleep(LIVE_BROADCAST_INTERVAL)

orchestrator = Orchestrator()

if __name__ == "__main__":
    logger.info("🚀 Starting Live DEX<->CEX monitor with Top 10")
    load_state()
    if TELEGRAM_TOKEN and WEBHOOK_URL:
        try:
            url = WEBHOOK_URL.rstrip("/") + "/webhook"
            r = requests.get(f"{TELEGRAM_API}/setWebhook?url={url}", timeout=10)
            logger.info("Set webhook result: %s", r.text[:200])
        except Exception as e:
            logger.warning("Webhook setup failed: %s", e)
    orchestrator.start()
    socketio.run(app, host="0.0.0.0", port=PORT)