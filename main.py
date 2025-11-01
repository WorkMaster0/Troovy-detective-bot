#!/usr/bin/env python3
# main.py - Live MEXC (CEX) <-> DEX monitor (top signals auto-added)
# - polls MEXC futures markets (ccxt), DEX prices (GMGN -> Dexscreener)
# - broadcasts live table via Flask + SocketIO and optionally edits Telegram message
# - alerts on spread >= alert threshold (default 3%), with cooldown/close logic
# - updates every 5 seconds (configurable)

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

# optional ccxt.pro support (we use sync ccxt for discovery & REST)
try:
    import ccxt.pro as ccxtpro  # optional
except Exception:
    ccxtpro = None
import ccxt

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # e.g. https://your-app.onrender.com
PORT = int(os.getenv("PORT", "10000"))

STATE_FILE = "state.json"
POLL_INTERVAL = 5.0                   # seconds between price cycles (user requested 5s)
MARKETS_REFRESH_INTERVAL = 600.0      # refresh MEXC markets every 10 minutes
LIVE_BROADCAST_INTERVAL = 5.0         # interval to emit live updates (5s)
ALERT_THRESHOLD_PCT = float(os.getenv("ALERT_THRESHOLD_PCT", "3.0"))  # open threshold
CLOSE_THRESHOLD_PCT = float(os.getenv("CLOSE_THRESHOLD_PCT", "0.5"))  # close threshold
MIN_QUOTE_VOLUME_USD = float(os.getenv("MIN_QUOTE_VOLUME_USD", str(1_000_000)))  # $1M
MAX_ABS_SPREAD_PCT = 2000.0           # skip insane spreads > 2000%
SIGNAL_COOLDOWN = 60.0                # seconds cooldown per symbol (unless spread increases)
HISTORY_WINDOW = 3600.0               # seconds to compute 1h % change
TOP_N = 10                            # show top 10 by 1h % change in UI
CEX_ID = os.getenv("CEX_PRIMARY", "mexc")  # default cex to query

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search/?q={q}"
GMGN_API = "https://gmgn.ai/defi/quotation/v1/tokens/search?keyword={q}"

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("arb-monitor")

# ---------------- STATE ----------------
state: Dict[str, Any] = {
    "symbols": [],            # man. monitored symbols (strings like "PEPE")
    "chat_id": None,
    "msg_id": None,
    "live_to_telegram": False,
    "alert_threshold_pct": ALERT_THRESHOLD_PCT,
    "close_threshold_pct": CLOSE_THRESHOLD_PCT,
}

# runtime caches
# only tokens that exist on MEXC futures (discovered)
mexc_markets_by_base: Dict[str, List[str]] = {}   # BASE -> [market_symbol(s)]
mexc_available_bases: List[str] = []              # list of base tokens available on MEXC futures

# latest prices
dex_prices: Dict[str, float] = {}     # base -> dex price USD
cex_prices: Dict[str, float] = {}     # base -> cex price USD
last_update: Dict[str, float] = {}    # base -> timestamp last price update

# history for 1h change (for cex)
cex_history: Dict[str, deque] = defaultdict(lambda: deque())  # base -> deque of (ts, price)

# alerts and cooldowns
active_spreads: Dict[str, Dict[str, Any]] = {}    # symbol -> open info
last_alert_time: Dict[str, float] = {}           # symbol -> last alert timestamp

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
        if r.status_code != 200:
            logger.warning("tg_edit failed: %s %s", r.status_code, r.text)
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
            # prefer first pair with valid priceUsd
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
    # try GMGN first, fallback to dexscreener
    res = fetch_from_gmgn(base)
    if res is not None:
        return res
    return fetch_from_dexscreener(base)

# ---------------- MEXC MARKETS DISCOVERY ----------------
def discover_mexc_markets() -> Tuple[Dict[str, List[str]], List[str]]:
    """
    Load markets via sync ccxt for CEX_ID, return mapping base->markets and list of bases (USDT futures).
    """
    markets_map: Dict[str, List[str]] = {}
    bases: List[str] = []
    try:
        if not hasattr(ccxt, CEX_ID):
            logger.warning("ccxt has no exchange class %s", CEX_ID)
            return {}, []
        ex = getattr(ccxt, CEX_ID)({"enableRateLimit": True})
        ex.load_markets(True)
        for m, info in ex.markets.items():
            # accept futures / swap contracts settled in USDT or symbol contains 'USDT'
            is_contract = bool(info.get("contract") or info.get("future") or info.get("type") == "future" or info.get("type") == "swap")
            settle = (info.get("settle") or info.get("settlement") or "")
            if not is_contract:
                continue
            if "USDT" not in m and "USDT" not in settle and "USD" not in m:
                continue
            base = info.get("base") or m.split("/")[0]
            base = base.upper()
            markets_map.setdefault(base, []).append(m)
        bases = sorted(list(markets_map.keys()))
        logger.info("Discovered %d MEXC futures bases", len(bases))
    except Exception as e:
        logger.warning("Failed to discover markets: %s", e)
    return markets_map, bases

# ---------------- CEX (MEXC) PRICES via REST ----------------
async def cex_price_cycle():
    """
    Periodically fetch tickers from MEXC (REST via ccxt) and update cex_prices and cex_history
    """
    global cex_prices, cex_history
    try:
        ex = getattr(ccxt, CEX_ID)({"enableRateLimit": True})
    except Exception as e:
        logger.warning("Failed to init ccxt %s: %s", CEX_ID, e)
        ex = None

    last_markets_refresh = 0.0
    while True:
        try:
            now = time.time()
            # refresh market list periodically
            if now - last_markets_refresh > MARKETS_REFRESH_INTERVAL or not mexc_available_bases:
                m_map, bases = discover_mexc_markets()
                mexc_markets_by_base.clear()
                mexc_markets_by_base.update(m_map)
                mexc_available_bases[:] = bases
                last_markets_refresh = now

            if not ex:
                try:
                    ex = getattr(ccxt, CEX_ID)({"enableRateLimit": True})
                except Exception:
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

            # fetch tickers once
            try:
                tickers = ex.fetch_tickers()
            except Exception as e:
                logger.warning("fetch_tickers failed: %s", e)
                await asyncio.sleep(POLL_INTERVAL)
                continue

            # iterate over known bases (to avoid scanning entire tickers)
            for base in list(mexc_available_bases):
                # find best ticker by matching base in pair keys
                best_price = None
                best_volume = 0.0
                for pair in mexc_markets_by_base.get(base, []):
                    t = tickers.get(pair) or {}
                    last = t.get("last") or t.get("close") or t.get("price")
                    qvol = t.get("quoteVolume") or (t.get("baseVolume") and t.get("last") and float(t.get("baseVolume"))*float(t.get("last"))) or 0.0
                    try:
                        if last is not None:
                            lastf = float(last)
                            qvolf = float(qvol or 0.0)
                            # prefer highest quote volume
                            if best_price is None or qvolf > best_volume:
                                best_price = lastf
                                best_volume = qvolf
                    except Exception:
                        continue
                # apply volume filter
                if best_price is not None and (best_volume >= MIN_QUOTE_VOLUME_USD or best_volume == 0.0):
                    cex_prices[base] = float(best_price)
                    last_update[base] = time.time()
                    # append to history
                    dq = cex_history[base]
                    dq.append((time.time(), float(best_price)))
                    # trim old entries
                    cutoff = time.time() - HISTORY_WINDOW - 10
                    while dq and dq[0][0] < cutoff:
                        dq.popleft()
                else:
                    # no usable cex price (low volume)
                    # keep previous if exists but do not overwrite with None
                    logger.debug("CEX: no usable price for %s (best_volume=%.2f)", base, best_volume)
            # remove very old history entries across symbols occasionally
            # sleep
        except Exception as e:
            logger.exception("cex_price_cycle error: %s", e)
        await asyncio.sleep(POLL_INTERVAL)

# ---------------- DEX PRICE CYCLE ----------------
async def dex_price_cycle():
    """
    Poll DEX price per base symbol using threadpool (requests)
    """
    loop = asyncio.get_event_loop()
    while True:
        try:
            bases = list(set(mexc_available_bases))  # only check tokens that exist on MEXC
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
                    # ignore zero/negative
                    if p and p > 0:
                        # sanity: ignore absurd mismatch where dex price * 1e6 < cex -> probably bad
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

# ---------------- SIGNAL / SPREAD PROCESSING ----------------
def compute_1h_change_pct_for_base(base: str) -> float:
    dq = cex_history.get(base)
    if not dq or len(dq) < 2:
        return 0.0
    now = time.time()
    # find newest and oldest within last hour
    newest_ts, newest_p = dq[-1]
    # find leftmost entry within HISTORY_WINDOW
    oldest_p = None
    for ts, p in dq:
        if ts >= now - HISTORY_WINDOW:
            oldest_p = p
            break
    if oldest_p is None or oldest_p == 0:
        return 0.0
    return (newest_p - oldest_p) / oldest_p * 100.0

def process_spreads_and_alerts():
    """
    For each base available, compute spread between CEX and DEX and maybe alert/add to monitored list
    Rules:
      - Only consider bases in mexc_available_bases
      - Require both cex and dex prices
      - Skip if dex price == 0
      - Skip if |pct| > MAX_ABS_SPREAD_PCT (likely bad)
      - Only alert if |pct| >= alert_threshold
      - Cooldown: SIGNAL_COOLDOWN unless spread increases beyond previously opened_pct
    """
    now = time.time()
    open_thresh = float(state.get("alert_threshold_pct", ALERT_THRESHOLD_PCT))
    close_thresh = float(state.get("close_threshold_pct", CLOSE_THRESHOLD_PCT))
    for base in list(mexc_available_bases):
        dex = dex_prices.get(base)
        cex = cex_prices.get(base)
        if dex is None or cex is None or dex == 0:
            continue
        pct = (cex - dex) / dex * 100.0  # positive -> cex higher
        if abs(pct) > MAX_ABS_SPREAD_PCT:
            # ignore insane
            continue

        # auto-add to monitored symbols on first signal
        if abs(pct) >= open_thresh and base not in state["symbols"]:
            state["symbols"].append(base)
            save_state()
            logger.info("Auto-added %s due to spread %.2f%%", base, pct)
            # notify on new auto-add
            if state.get("chat_id"):
                tg_send(f"➕ Auto-added `{base}` (spread {pct:.2f}%)")
        # check open/close alerts per active_spreads
        active = active_spreads.get(base)
        if not active:
            # not active - open if exceed threshold and pass cooldown (or never alerted before)
            last_alert = last_alert_time.get(base, 0)
            if abs(pct) >= open_thresh and (now - last_alert >= SIGNAL_COOLDOWN):
                # open
                active_spreads[base] = {"opened_pct": pct, "open_ts": now, "dex_price": dex, "cex_price": cex}
                last_alert_time[base] = now
                logger.info("ALERT OPEN %s %.2f%%", base, pct)
                if state.get("chat_id"):
                    tg_send(f"🔔 *Spread OPENED* `{base}`\nDEX: `{dex:.8f}`\nCEX: `{cex:.8f}`\nSpread: *{pct:.2f}%*")
        else:
            # already active -> if spread increased, optionally re-alert; if decreased below close -> close
            opened_pct = active.get("opened_pct", 0.0)
            if abs(pct) > abs(opened_pct) and (now - last_alert_time.get(base, 0) >= SIGNAL_COOLDOWN):
                # re-alert because spread increased
                active_spreads[base]["opened_pct"] = pct
                last_alert_time[base] = now
                logger.info("ALERT INCREASE %s %.2f%% (was %.2f%%)", base, pct, opened_pct)
                if state.get("chat_id"):
                    tg_send(f"🔺 *Spread increased* `{base}`\nNow: *{pct:.2f}%* (was {opened_pct:.2f}%)")
            # close condition: abs(pct) <= close_thresh
            if abs(pct) <= close_thresh:
                duration = int(now - active.get("open_ts", now))
                logger.info("ALERT CLOSE %s %.2f%% (duration %ds)", base, pct, duration)
                if state.get("chat_id"):
                    tg_send(f"✅ *Spread CLOSED* `{base}`\nNow: DEX `{dex:.8f}` | CEX `{cex:.8f}`\nSpread: *{pct:.2f}%*\nOpen duration: {duration}s")
                active_spreads.pop(base, None)
                last_alert_time[base] = now

# ---------------- FLASK + SOCKET.IO ----------------
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

INDEX_HTML = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Live MEXC ↔ DEX Monitor</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
  </head>
  <body class="bg-light">
    <div class="container py-4">
      <h3>Live MEXC ↔ DEX Monitor (Top 10 by 1h change)</h3>
      <div class="mb-2">
        <form id="addForm" class="row g-2">
          <div class="col-auto"><input id="symbol" class="form-control" placeholder="SYMBOL (e.g. PEPE)" autocomplete="off"></div>
          <div class="col-auto"><button class="btn btn-primary">Add</button></div>
          <div class="col-auto"><button id="clearBtn" class="btn btn-danger" type="button">Clear All</button></div>
        </form>
      </div>
      <div id="statusBadge" class="mb-2"></div>
      <div class="table-responsive">
        <table class="table table-sm table-bordered" id="liveTable">
          <thead class="table-light"><tr><th>Symbol</th><th>1h Δ%</th><th>DEX (USD)</th><th>MEXC (USD)</th><th>Δ%</th><th>Last</th></tr></thead>
          <tbody id="tbody"></tbody>
        </table>
      </div>
      <div class="small text-muted">Connected clients: <span id="clients">0</span></div>
    </div>
    <script>
      const socket = io();
      const tbody = document.getElementById("tbody");
      const clientsEl = document.getElementById("clients");
      const statusBadge = document.getElementById("statusBadge");

      socket.on("connect", () => { console.log("connected"); });
      socket.on("live.update", (data) => {
        const rows = data.rows || [];
        tbody.innerHTML = "";
        rows.forEach(r => {
          const tr = document.createElement("tr");
          let pct_sign = r.pct >= 0 ? "🟢" : "🔴";
          let pct = (r.pct).toFixed(2) + "%";
          let change1h = (r.change1h).toFixed(2) + "%";
          let dexStr = r.dex == null ? "—" : Number(r.dex).toFixed(8);
          let cexStr = r.cex == null ? "—" : Number(r.cex).toFixed(8);
          let last = r.last ? new Date(r.last*1000).toISOString().substr(11,8) : "—";
          tr.innerHTML = `<td><strong>${r.symbol}</strong></td><td>${change1h}</td><td>${dexStr}</td><td>${cexStr}</td><td>${pct_sign} ${pct}</td><td>${last}</td>`;
          tbody.appendChild(tr);
        });
      });
      socket.on("clients", (n) => { clientsEl.innerText = n; });
      socket.on("status", (txt) => { statusBadge.innerHTML = '<span class="badge bg-info">'+txt+'</span>'; setTimeout(()=>statusBadge.innerHTML="",3000); });

      document.getElementById("addForm").addEventListener("submit", (e) => {
        e.preventDefault();
        const sym = document.getElementById("symbol").value.trim().toUpperCase();
        if (!sym) return;
        socket.emit("add_symbol", sym);
        document.getElementById("symbol").value = "";
      });
      document.getElementById("clearBtn").addEventListener("click", () => {
        if (!confirm("Clear all symbols?")) return;
        socket.emit("clear_symbols");
      });
    </script>
  </body>
</html>
"""

@app.route("/", methods=["GET"])
def index():
    return render_template_string(INDEX_HTML)

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
    logger.info("Webhook cmd from %s: %s", cid, text[:200])
    parts = text.split()
    cmd = parts[0].lower()
    try:
        if cmd == "/start":
            tg_send("🤖 Live monitor online. Use /add SYMBOL")
        elif cmd == "/help":
            tg_send("Commands:\n/add SYMBOL\n/remove SYMBOL\n/list\n/clear\n/alert <pct>\n/live on|off\n/status\n/help")
        elif cmd == "/add" and len(parts) >= 2:
            sym = parts[1].upper()
            if sym not in state["symbols"]:
                state["symbols"].append(sym)
                save_state()
                socketio.emit("status", f"Added {sym}")
                tg_send(f"✅ Added {sym}")
            else:
                tg_send(f"⚠️ {sym} already monitored")
        elif cmd == "/remove" and len(parts) >= 2:
            sym = parts[1].upper()
            if sym in state["symbols"]:
                state["symbols"].remove(sym)
                save_state()
                socketio.emit("status", f"Removed {sym}")
                tg_send(f"🗑 Removed {sym}")
            else:
                tg_send(f"⚠️ {sym} not monitored")
        elif cmd == "/list":
            tg_send("Monitored: " + (", ".join(state["symbols"]) if state["symbols"] else "—"))
        elif cmd == "/clear":
            state["symbols"] = []
            save_state()
            socketio.emit("status", "Cleared symbols")
            tg_send("🧹 Cleared all symbols")
        elif cmd == "/alert":
            if len(parts) >= 2:
                try:
                    pct = float(parts[1])
                    state["alert_threshold_pct"] = pct
                    save_state()
                    tg_send(f"✅ Alert threshold set to {pct:.2f}%")
                except Exception:
                    tg_send("Usage: /alert <pct> (numeric)")
            else:
                tg_send(f"Current alert threshold: {state.get('alert_threshold_pct'):.2f}%")
        elif cmd == "/live":
            if len(parts) >= 2 and parts[1].lower() in ("on", "off"):
                state["live_to_telegram"] = (parts[1].lower() == "on")
                save_state()
                tg_send(f"Live-to-Telegram set to {state['live_to_telegram']}")
            else:
                tg_send("Usage: /live on|off")
        elif cmd == "/status":
            syms = state.get("symbols", [])
            txt = [
                f"Symbols: {', '.join(syms) if syms else '—'}",
                f"Alert threshold: {state.get('alert_threshold_pct'):.2f}%",
                f"Live->Telegram: {state.get('live_to_telegram')}",
                f"Active spreads: {len(active_spreads)}",
            ]
            tg_send("\n".join(txt))
        else:
            tg_send("❓ Unknown command. /help")
    except Exception as e:
        logger.exception("cmd error: %s", e)
        tg_send("⚠️ Error processing command.")
    return jsonify({"ok": True})

@socketio.on("connect")
def on_connect():
    try:
        emit("clients", 1)
        # initial update
        emit("live.update", {"rows": []})
    except Exception:
        pass

@socketio.on("add_symbol")
def on_add_symbol(sym):
    s = sym.strip().upper()
    if not s:
        return
    if s not in state["symbols"]:
        state["symbols"].append(s)
        save_state()
        emit("status", f"Added {s}", broadcast=True)
    else:
        emit("status", f"{s} already monitored")

@socketio.on("clear_symbols")
def on_clear_symbols():
    state["symbols"] = []
    save_state()
    emit("status", "Cleared symbols", broadcast=True)

# ---------------- ORCHESTRATION ----------------
class Orchestrator:
    def __init__(self):
        self.loop = None
        self.thread = None
        self.tasks: List[asyncio.Task] = []
        self.running = False

    def start(self):
        if self.running:
            return
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
        # start background coroutines
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
        """
        Periodically compute top tokens and emit live table; also process spread alerts.
        """
        # set webhook if requested
        if TELEGRAM_TOKEN and WEBHOOK_URL:
            try:
                url = WEBHOOK_URL.rstrip("/") + "/webhook"
                r = requests.get(f"{TELEGRAM_API}/setWebhook?url={url}", timeout=8)
                logger.info("Set webhook result: %s", r.text[:200])
            except Exception:
                pass

        while True:
            try:
                # process spreads & alerts
                process_spreads_and_alerts()

                # compute 1h change for all bases we have cex history for
                all_bases = list(mexc_available_bases)
                scored = []
                for b in all_bases:
                    change1h = compute_1h_change_pct_for_base(b)
                    scored.append((b, change1h))
                # pick top by absolute 1h change
                scored.sort(key=lambda x: abs(x[1]), reverse=True)
                top = scored[:TOP_N]

                rows = []
                for b, ch in top:
                    dex = dex_prices.get(b)
                    cex = cex_prices.get(b)
                    pct = None
                    if dex and cex and dex != 0:
                        pct = (cex - dex) / dex * 100.0
                    rows.append({
                        "symbol": b,
                        "change1h": ch,
                        "dex": dex,
                        "cex": cex,
                        "pct": pct if pct is not None else 0.0,
                        "last": last_update.get(b)
                    })

                # emit to websocket clients
                try:
                    socketio.emit("live.update", {"rows": rows})
                except Exception:
                    pass

                # optionally edit Telegram live panel every interval if enabled
                if state.get("live_to_telegram") and state.get("chat_id"):
                    try:
                        lines = ["📡 *Live MEXC ↔ DEX Monitor*",
                                 f"_Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}_",
                                 "`SYMBOL    1hΔ%    DEX(USD)     MEXC(USD)    Δ%`",
                                 "`-------------------------------------------------`"]
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
                                    state["msg_id"] = int(mid)
                                    save_state()
                        else:
                            tg_edit(state["msg_id"], txt)
                    except Exception:
                        pass

            except Exception as e:
                logger.debug("broadcaster iteration error: %s", e)

            await asyncio.sleep(LIVE_BROADCAST_INTERVAL)

    def stop(self):
        if not self.running:
            return
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
    logger.info("🚀 Starting Live MEXC<->DEX monitor (5s poll, short logs)")
    load_state()
    orchestrator.start()
    # run Flask-SocketIO (eventlet recommended)
    socketio.run(app, host="0.0.0.0", port=PORT)