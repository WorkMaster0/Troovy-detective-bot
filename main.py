#!/usr/bin/env python3
# main.py - Live MEXC <-> DEX <-> BIN monitor
# Updated: supports automatic MEXC futures discovery, 1h-change top10, 11s updates, Telegram alerts (>=3%), and filtering.

import os
import time
import json
import logging
import asyncio
import requests
from datetime import datetime
from threading import Thread
from typing import Dict, Optional, List, Any, Tuple
from flask import Flask, request, render_template_string, jsonify
from flask_socketio import SocketIO, emit

# ccxt (sync) for REST market/ticker queries
import ccxt

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # e.g. https://yourapp.onrender.com
PORT = int(os.getenv("PORT", "10000"))

STATE_FILE = "state.json"
POLL_INTERVAL_DEX = 11.0          # seconds between polling DEX/CEX for prices (also broadcast interval)
MEXC_REFRESH_INTERVAL = 300.0     # refresh MEXC symbols list every 5 minutes
BINANCE_REFRESH_INTERVAL = 300.0  # refresh Binance symbols list every 5 minutes
ALERT_THRESHOLD_PCT = float(os.getenv("ALERT_THRESHOLD_PCT", "3.0"))  # open alert threshold %
CLOSE_THRESHOLD_PCT = float(os.getenv("CLOSE_THRESHOLD_PCT", "0.5"))  # close threshold
TOP_N = 10                         # top N tokens by 1h change
CEHISTORY_RETENTION = 4000        # keep enough price points (seconds / 11s ~ 327 for 1h); generous buffer
MAX_ABS_SPREAD_PCT = 5000.0       # ignore absurd spreads larger than this percent
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# default exchanges
CEX_PRIMARY = os.getenv("CEX_PRIMARY", "mexc")
CEX_SECONDARY = os.getenv("CEX_SECONDARY", "binance")

# DEX APIs
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search/?q={q}"
GMGN_API = "https://gmgn.ai/defi/quotation/v1/tokens/search?keyword={q}"

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("live-monitor")

# ---------------- STATE & RUNTIME ----------------
state: Dict[str, Any] = {
    "symbols": [],             # manually added symbols (not used for auto-top)
    "chat_id": None,
    "msg_id": None,
    "monitoring": True,
    "alert_threshold_pct": ALERT_THRESHOLD_PCT,
    "close_threshold_pct": CLOSE_THRESHOLD_PCT,
    "live_to_telegram": False,
}

# runtime caches
mexc_symbols: List[str] = []         # discovered MEXC futures symbols (e.g. 'ABC/USDT' or 'ABCUSDT')
binance_symbols: List[str] = []
dex_prices: Dict[str, float] = {}    # latest DEX price per SYMBOL (symbol key normalized)
mexc_prices: Dict[str, float] = {}   # latest MEXC price
binance_prices: Dict[str, float] = {}# latest Binance price
last_update: Dict[str, float] = {}  # symbol -> timestamp of last update

# price history for computing 1h change (we use MEXC prices as canonical for 1h change)
price_history: Dict[str, List[Tuple[float, float]]] = {}  # symbol -> list of (ts, price)

# alert state
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
                logger.info("Loaded state with %d saved symbols", len(state.get("symbols", [])))
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

# ---------------- TELEGRAM HELPERS ----------------
def tg_send(text: str) -> Optional[dict]:
    if not TELEGRAM_TOKEN or not state.get("chat_id"):
        logger.debug("tg_send skipped (missing token/chat_id)")
        return None
    try:
        payload = {
            "chat_id": state["chat_id"],
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
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
        if r.status_code != 200:
            logger.warning("tg_edit failed: %s %s", r.status_code, r.text)
        return r.json()
    except Exception as e:
        logger.exception("tg_edit error: %s", e)
        return None

# ---------------- UTIL ----------------
def now_ts() -> float:
    return time.time()

def pretty_ts(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    return datetime.utcfromtimestamp(ts).strftime("%H:%M:%S")

# compute percent change from earlier price, handle 0 safely
def pct_change(old: float, new: float) -> float:
    try:
        if old == 0:
            return 0.0
        return (new - old) / old * 100.0
    except Exception:
        return 0.0

# safe numeric check (ignore NaN/inf)
def is_valid_price(p: float) -> bool:
    try:
        if p is None:
            return False
        if p != p:  # NaN
            return False
        if p == float("inf") or p == float("-inf"):
            return False
        if p <= 0:
            return False
        if p < 1e-12:
            return False
        return True
    except Exception:
        return False

# ---------------- DEX FETCHERS ----------------
def fetch_from_gmgn(symbol: str) -> Optional[float]:
    try:
        q = symbol.upper()
        url = GMGN_API.format(q=q)
        r = requests.get(url, timeout=7)
        r.raise_for_status()
        data = r.json()
        items = data.get("data") or []
        if items:
            for it in items:
                price = it.get("price_usd") or it.get("priceUsd") or it.get("price")
                if price:
                    return float(price)
    except Exception:
        pass
    return None

def fetch_from_dexscreener(symbol: str) -> Optional[float]:
    try:
        q = symbol.upper()
        url = DEXSCREENER_SEARCH.format(q=q)
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

def fetch_price_from_dex(symbol: str) -> Optional[float]:
    res = fetch_from_gmgn(symbol)
    if res is not None:
        return res
    return fetch_from_dexscreener(symbol)

# ---------------- CEX SYMBOL REFRESH ----------------
def refresh_mexc_symbols(client: ccxt.Exchange) -> List[str]:
    """
    Fetch available MEXC markets and keep likely USDT futures/perp markets.
    Returns list of symbol keys normalized (we will map to token symbol like 'ABC' -> 'ABC' or 'ABC/USDT').
    """
    out: List[str] = []
    try:
        markets = client.fetch_markets()
        for m in markets:
            # keep only markets that reference USDT (common for futures/perp)
            pair = m.get('symbol') or m.get('id') or ""
            if "USDT" not in pair.upper():
                continue
            # Many markets are 'ABC/USDT' or 'ABCUSDT' etc. Normalize to token symbol (left part)
            try:
                base = m.get('base') or pair.split('/')[0]
                if base:
                    tok = base.upper()
                    if tok not in out:
                        out.append(tok)
            except Exception:
                continue
    except Exception as e:
        logger.exception("refresh_mexc_symbols error: %s", e)
    return out

def refresh_binance_symbols(client: ccxt.Exchange) -> List[str]:
    out: List[str] = []
    try:
        markets = client.fetch_markets()
        for m in markets:
            pair = m.get('symbol') or ""
            if "USDT" not in pair.upper():
                continue
            try:
                base = m.get('base') or pair.split('/')[0]
                if base:
                    tok = base.upper()
                    if tok not in out:
                        out.append(tok)
            except Exception:
                continue
    except Exception as e:
        logger.exception("refresh_binance_symbols error: %s", e)
    return out

# ---------------- CEX PRICE POLLING ----------------
def fetch_cex_tickers_sync(client: ccxt.Exchange) -> Dict[str, Any]:
    """
    Fetch tickers (sync). Return mapping symbol->ticker dict.
    """
    try:
        return client.fetch_tickers()
    except Exception as e:
        logger.debug("fetch_tickers failed: %s", e)
        # fallback: try per-symbol fetch? skip for now
        return {}

# ---------------- PRICE HISTORY ----------------
def push_price_history(sym: str, price: float):
    if not is_valid_price(price):
        return
    lst = price_history.setdefault(sym, [])
    ts = now_ts()
    lst.append((ts, price))
    # trim
    max_len = int(3600 / max(1, POLL_INTERVAL_DEX)) + 20
    if len(lst) > max_len:
        # keep latest
        del lst[:len(lst)-max_len]

def get_price_1h_ago(sym: str) -> Optional[float]:
    """
    Return the price closest to 1 hour ago for given symbol from price_history.
    If no data that old, return earliest available (or None).
    """
    lst = price_history.get(sym)
    if not lst:
        return None
    target = now_ts() - 3600.0
    # find entry with timestamp <= target (closest older), or the earliest if none
    best = None
    for ts, p in lst:
        if ts <= target:
            best = (ts, p)
        else:
            break
    if best:
        return best[1]
    # if none older than target, return earliest (so change will be small)
    return lst[0][1] if lst else None

# ---------------- ALERT / SPREAD DECISION ----------------
def check_and_alert(sym: str, dex_price: Optional[float], mexc_price: Optional[float], bin_price: Optional[float]):
    # require valid both prices to compute spread
    if not is_valid_price(mexc_price) or not is_valid_price(dex_price):
        return
    pct = pct_change(dex_price, mexc_price)  # positive = mexc higher than dex
    if abs(pct) > MAX_ABS_SPREAD_PCT:
        # ignore absurd
        return
    now = now_ts()
    open_thresh = float(state.get("alert_threshold_pct", ALERT_THRESHOLD_PCT))
    close_thresh = float(state.get("close_threshold_pct", CLOSE_THRESHOLD_PCT))

    # open
    if sym not in active_spreads and pct >= open_thresh:
        # cooldown: if very recently alerted same symbol, ignore unless increase in spread
        last = last_alert_time.get(sym, 0)
        if now - last < 60 and sym in active_spreads:
            return
        active_spreads[sym] = {"opened_pct": pct, "open_ts": now, "dex": dex_price, "mexc": mexc_price}
        last_alert_time[sym] = now
        msg = (
            "🔔 *Spread OPENED*\n"
            f"Symbol: `{sym}`\n"
            f"DEX price: `{dex_price:.8f}`\n"
            f"MEXC price: `{mexc_price:.8f}`\n"
            f"Spread: *{pct:.2f}%*\n"
            f"Time: {datetime.utcfromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        logger.info("ALERT OPEN %s %.2f%%", sym, pct)
        tg_send(msg)
        return

    # close
    if sym in active_spreads:
        if pct <= close_thresh or pct < active_spreads[sym].get("opened_pct", 0) * 0.9:
            opened = active_spreads.pop(sym)
            last_alert_time[sym] = now
            duration = int(now - opened.get("open_ts", now))
            msg = (
                "✅ *Spread CLOSED*\n"
                f"Symbol: `{sym}`\n"
                f"Now: DEX `{dex_price:.8f}` | MEXC `{mexc_price:.8f}`\n"
                f"Current spread: *{pct:.2f}%*\n"
                f"Opened: *{opened.get('opened_pct', 0):.2f}%*, duration: {duration}s\n"
                f"Time: {datetime.utcfromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S UTC')}"
            )
            logger.info("ALERT CLOSE %s %.2f%%", sym, pct)
            tg_send(msg)
            return

# ---------------- BUILD LIVE TABLE (MARKDOWN for Telegram & JSON for UI) ----------------
def build_live_text(rows: List[Dict[str, Any]]) -> str:
    lines = []
    lines.append("📡 *Live MEXC ↔ DEX ↔ BIN Monitor*")
    lines.append(f"_Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}_\n")
    lines.append("`SYMBOL     1hΔ%    DEX(USD)     MEXC(USD)    BIN(USD)    Δ%`")
    lines.append("`----------------------------------------------------------------`")
    for r in rows:
        s = r.get("symbol", "—")
        delta1h = r.get("1h_change", 0.0)
        dex = r.get("dex") or None
        mexc = r.get("mexc") or None
        binp = r.get("bin") or None
        dex_s = f"{dex:.8f}" if is_valid_price(dex) else "—"
        mexc_s = f"{mexc:.8f}" if is_valid_price(mexc) else "—"
        bin_s = f"{binp:.8f}" if is_valid_price(binp) else "—"
        pct = r.get("spread_pct")
        pct_s = f"{pct:+6.2f}%" if pct is not None else "—"
        lines.append(f"`{s:<9}` {delta1h:+6.2f}%  {dex_s:>12}  {mexc_s:>12}  {bin_s:>12}  {pct_s}")
    lines.append("\n`/status  /live on|off  /alert X`")
    return "\n".join(lines)

# ---------------- FLASK + SOCKET.IO UI ----------------
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

INDEX_HTML = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Live MEXC ↔ DEX ↔ BIN Monitor</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
    <style>
      .spread-pos { background:#ffecec; }   /* CEX >> DEX */
      .spread-neg { background:#eaffea; }   /* DEX >> CEX */
    </style>
  </head>
  <body class="bg-light">
    <div class="container py-4">
      <h3>Live MEXC ↔ DEX ↔ BIN Monitor</h3>
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
          <thead class="table-light"><tr>
            <th>Symbol</th><th>1hΔ%</th><th>DEX (USD)</th><th>MEXC (USD)</th><th>BIN (USD)</th><th>Δ%</th><th>Last</th>
          </tr></thead>
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
          const s = r.symbol || "—";
          const delta = (r["1h_change"]!=null)? r["1h_change"].toFixed(2)+"%":"—";
          const dex = r.dex!=null? Number(r.dex).toFixed(8):"—";
          const mexc = r.mexc!=null? Number(r.mexc).toFixed(8):"—";
          const bin = r.bin!=null? Number(r.bin).toFixed(8):"—";
          const pct = r.spread_pct!=null? ((r.spread_pct).toFixed(2)+"%"):"—";
          const last = r.last? new Date(r.last*1000).toISOString().substr(11,8) : "—";
          const tr = document.createElement("tr");
          if (r.spread_pct != null) {
            if (r.spread_pct > 0.0) tr.className = "spread-pos";
            else if (r.spread_pct < 0.0) tr.className = "spread-neg";
          }
          tr.innerHTML = `<td><strong>${s}</strong></td><td>${delta}</td><td>${dex}</td><td>${mexc}</td><td>${bin}</td><td>${pct}</td><td>${last}</td>`;
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

# ---------------- TELEGRAM WEBHOOK ----------------
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
            tg_send("Commands:\n/add SYMBOL\n/remove SYMBOL\n/list\n/clear\n/alert <pct> - set open threshold\n/live on|off - toggle editing live panel in Telegram\n/status - show status\n/help")
        elif cmd == "/add":
            if len(parts) >= 2:
                sym = parts[1].upper()
                if sym not in state["symbols"]:
                    state["symbols"].append(sym)
                    save_state()
                    socketio.emit("status", f"Added {sym}")
                    tg_send(f"✅ Added {sym}")
                else:
                    tg_send(f"⚠️ {sym} already monitored")
        elif cmd == "/remove":
            if len(parts) >= 2:
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
                    tg_send("Usage: /alert <pct>  (numeric)")
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
            txt_lines = [f"Symbols: {', '.join(syms) if syms else '—'}"]
            txt_lines.append(f"Alert threshold: {state.get('alert_threshold_pct'):.2f}%")
            txt_lines.append(f"Live->Telegram: {state.get('live_to_telegram')}")
            txt_lines.append(f"Active spreads: {len(active_spreads)}")
            tg_send("\n".join(txt_lines))
        else:
            tg_send("❓ Unknown command. /help")
    except Exception as e:
        logger.exception("cmd error: %s", e)
        tg_send("⚠️ Error processing command.")
    return jsonify({"ok": True})

# ---------------- SOCKETIO HANDLERS ----------------
@socketio.on("connect")
def on_connect():
    try:
        participants = 1
        try:
            if hasattr(socketio, "server") and getattr(socketio, "server") is not None:
                participants = len(socketio.server.manager.get_participants('/', '/'))
        except Exception:
            participants = 1
        emit("clients", participants)
        # initial empty update
        emit("live.update", {"rows": []})
    except Exception:
        pass

@socketio.on("add_symbol")
def on_add_symbol(sym):
    s = sym.strip().upper()
    if not s: return
    if s not in state["symbols"]:
        state["symbols"].append(s)
        save_state()
        socketio.emit("status", f"Added {s}")
    else:
        socketio.emit("status", f"{s} already monitored")

@socketio.on("clear_symbols")
def on_clear_symbols():
    state["symbols"] = []
    save_state()
    socketio.emit("status", "Cleared symbols")

# ---------------- ORCHESTRATOR ----------------
class Orchestrator:
    def __init__(self):
        self.loop = None
        self.thread = None
        self.running = False
        self.mexc_client = None
        self.binance_client = None
        self.last_mexc_refresh = 0.0
        self.last_binance_refresh = 0.0

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
            logger.exception("orchestrator loop error: %s", e)

    async def _main(self):
        load_state()
        logger.info("Starting background tasks")
        # init ccxt clients (sync objects, used inside thread)
        try:
            self.mexc_client = getattr(ccxt, CEX_PRIMARY)({"enableRateLimit": True})
        except Exception as e:
            logger.exception("Failed to init mexc client: %s", e)
            self.mexc_client = None
        try:
            self.binance_client = getattr(ccxt, CEX_SECONDARY)({"enableRateLimit": True})
        except Exception as e:
            logger.exception("Failed to init binance client: %s", e)
            self.binance_client = None

        # initial refresh
        await self._refresh_exchange_symbols()

        # main loop
        while True:
            try:
                # refresh symbols lists periodically
                now = now_ts()
                if now - self.last_mexc_refresh > MEXC_REFRESH_INTERVAL:
                    await self._refresh_exchange_symbols()
                # fetch tickers
                await self._poll_cex_and_dex()
            except Exception as e:
                logger.exception("orchestrator iteration error: %s", e)
            await asyncio.sleep(POLL_INTERVAL_DEX)

    async def _refresh_exchange_symbols(self):
        now = now_ts()
        if self.mexc_client:
            try:
                syms = await asyncio.get_event_loop().run_in_executor(None, refresh_mexc_symbols, self.mexc_client)
                global mexc_symbols
                mexc_symbols = syms
                logger.info("MEXC symbols refreshed: %d", len(mexc_symbols))
            except Exception as e:
                logger.exception("refresh mexc error: %s", e)
        if self.binance_client:
            try:
                syms = await asyncio.get_event_loop().run_in_executor(None, refresh_binance_symbols, self.binance_client)
                global binance_symbols
                binance_symbols = syms
                logger.info("Binance (spot/futures) symbols refreshed: %d", len(binance_symbols))
            except Exception as e:
                logger.exception("refresh binance error: %s", e)
        self.last_mexc_refresh = now
        self.last_binance_refresh = now

    async def _poll_cex_and_dex(self):
        """
        Poll CEX tickers (MEXC + BIN), poll DEX prices for top candidates,
        build top-N list by 1h change, broadcast to UI and optionally Telegram.
        """
               # --- Fetch current prices ---
        # --- Фʼючерсний монітор: DEX (spot) ↔ MEXC Futures ↔ Binance Futures ---
rows = []

# Отримуємо ціни з усіх джерел
dex_prices = await self._get_dex_prices()
mexc_fut = await self._get_mexc_futures_prices()
bin_fut = await self._get_binance_futures_prices()

# Перетворюємо у зручний вигляд
common_symbols = set(dex_prices.keys()) & set(mexc_fut.keys()) & set(bin_fut.keys())

for sym in common_symbols:
    dex_price = dex_prices.get(sym)
    mex_price = mexc_fut.get(sym)
    bin_price = bin_fut.get(sym)

    # Пропускаємо некоректні або нульові значення
    valid = [p for p in [dex_price, mex_price, bin_price] if p and p > 0]
    if len(valid) < 2:
        continue

    # Обчислюємо спред
    high = max(valid)
    low = min(valid)
    spread = (high - low) / low * 100

    # Ігноруємо незначні коливання
    if spread < 3:
        continue

    # Додаємо у таблицю
    rows.append({
        "symbol": sym,
        "dex": f"{dex_price:.6f}" if dex_price else "—",
        "mexc": f"{mex_price:.6f}" if mex_price else "—",
        "bin": f"{bin_price:.6f}" if bin_price else "—",
        "spread": f"{spread:+.2f}%"
    })

# --- Формуємо текст для Telegram ---
if not rows:
    text = (
        "📡 *Live MEXC Futures ↔ Binance Futures ↔ DEX Monitor*\n"
        f"_Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}_\n\n"
        "Немає активних спредів > 3%.\n\n"
        "/status  /live on|off  /alert X"
    )
else:
    header = "SYMBOL     DEX(USD)     MEXC(FUT)     BIN(FUT)     Δ%\n" + "-" * 60
    lines = [header]
    for r in rows[:15]:
        lines.append(f"{r['symbol']:<8} {r['dex']:<12} {r['mexc']:<12} {r['bin']:<12} {r['spread']}")
    text = (
        "📡 *Live MEXC Futures ↔ Binance Futures ↔ DEX Monitor*\n"
        f"_Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}_\n\n"
        + "\n".join(lines)
        + "\n\n/status  /live on|off  /alert X"
    )

await self._send_or_edit_live_message(text)

        # 1️⃣ Отримуємо ціни з бірж (ccxt)
        mexc_tickers = {}
        bin_tickers = {}
        if self.mexc_client:
            mexc_tickers = await asyncio.get_event_loop().run_in_executor(None, fetch_cex_tickers_sync, self.mexc_client)
        if self.binance_client:
            bin_tickers = await asyncio.get_event_loop().run_in_executor(None, fetch_cex_tickers_sync, self.binance_client)

        # 2️⃣ Зберігаємо ціни
        for k, v in mexc_tickers.items():
            price = v.get("last") or v.get("close") or 0
            if is_valid_price(price):
                base = v.get("symbol").split("/")[0] if "/" in v.get("symbol") else v.get("symbol").replace("USDT", "")
                mexc_prices[base.upper()] = price
                push_price_history(base.upper(), price)

        for k, v in bin_tickers.items():
            price = v.get("last") or v.get("close") or 0
            if is_valid_price(price):
                base = v.get("symbol").split("/")[0] if "/" in v.get("symbol") else v.get("symbol").replace("USDT", "")
                binance_prices[base.upper()] = price

        # 3️⃣ Формуємо список токенів (автоматично з MEXC)
        symbols_to_check = set(mexc_prices.keys())

        # 4️⃣ Отримуємо ціни з DEX для цих токенів
        for sym in list(symbols_to_check)[:100]:  # обмежимо для швидкодії
            dex_p = await asyncio.get_event_loop().run_in_executor(None, fetch_price_from_dex, sym)
            if dex_p and is_valid_price(dex_p):
                dex_prices[sym] = dex_p
                last_update[sym] = now_ts()

        # 5️⃣ Розрахунок спредів
        for sym in symbols_to_check:
            dex_price = dex_prices.get(sym)
            mexc_price = mexc_prices.get(sym)
            bin_price = binance_prices.get(sym)

            if not mexc_price or mexc_price <= 0:
                continue

            valid_prices = [p for p in [dex_price, mexc_price, bin_price] if is_valid_price(p)]
            if len(valid_prices) < 2:
                continue

            high = max(valid_prices)
            low = min(valid_prices)
            spread_percent = (high - low) / low * 100

            if spread_percent < ALERT_THRESHOLD_PCT:
                continue

            price_1h_ago = get_price_1h_ago(sym)
            change_1h = pct_change(price_1h_ago, mexc_price) if price_1h_ago else 0

            rows.append({
                "symbol": sym,
                "dex": dex_price,
                "mexc": mexc_price,
                "bin": bin_price,
                "1h_change": change_1h,
                "spread_pct": spread_percent,
                "last": last_update.get(sym, now_ts())
            })

            check_and_alert(sym, dex_price, mexc_price, bin_price)

        # 6️⃣ Сортування за 1h зміною
        rows.sort(key=lambda r: abs(r["1h_change"]), reverse=True)
        rows = rows[:TOP_N]

        # 7️⃣ Оновлення UI
        socketio.emit("live.update", {"rows": rows})

        # 8️⃣ Якщо включено /live on — оновлюємо Telegram
        if state.get("live_to_telegram"):
            txt = build_live_text(rows)
            msg_id = state.get("msg_id")
            if msg_id:
                tg_edit(msg_id, txt)
            else:
                sent = tg_send(txt)
                if sent and "result" in sent:
                    state["msg_id"] = sent["result"]["message_id"]
                    save_state()

# ---------------- BOOT ----------------
orchestrator = Orchestrator()

if __name__ == "__main__":
    logger.info("Starting Live MEXC<->DEX<->BIN Monitor")
    load_state()

    # set Telegram webhook if provided
    if TELEGRAM_TOKEN and WEBHOOK_URL:
        try:
            url = WEBHOOK_URL.rstrip("/") + "/webhook"
            r = requests.get(f"{TELEGRAM_API}/setWebhook?url={url}", timeout=8)
            logger.info("Set webhook result: %s", r.text[:200])
        except Exception as e:
            logger.warning("Failed to set webhook: %s", e)

    # start orchestrator (background thread)
    orchestrator.start()

    # run Flask-SocketIO server using eventlet (recommended)
    # in some environments Werkzeug warns; using eventlet by installing eventlet recommended
    socketio.run(app, host="0.0.0.0", port=PORT)