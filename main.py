#!/usr/bin/env python3
# main.py - Live DEX <-> CEX monitor (auto-add signals >=3%)
# Requirements: flask, flask_socketio, requests, ccxt, eventlet (recommended)

import os
import time
import json
import logging
import asyncio
import requests
from datetime import datetime
from threading import Thread
from typing import Dict, Optional, List, Any
from flask import Flask, request, render_template_string, jsonify
from flask_socketio import SocketIO, emit

# ccxt (sync) for REST access to MEXC
import ccxt

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # e.g. https://your-app.onrender.com
PORT = int(os.getenv("PORT", "10000"))

STATE_FILE = "state.json"
POLL_INTERVAL_CEX = float(os.getenv("POLL_INTERVAL_CEX", "5.0"))    # scan all mexc tickers every N sec
POLL_INTERVAL_DEX = float(os.getenv("POLL_INTERVAL_DEX", "3.0"))    # dex price fetch TTL uses cache
LIVE_BROADCAST_INTERVAL = float(os.getenv("LIVE_BROADCAST_INTERVAL", "5.0"))  # socketio broadcast
ALERT_THRESHOLD_PCT = float(os.getenv("ALERT_THRESHOLD_PCT", "3.0"))  # open alert threshold %
ALERT_COOLDOWN = float(os.getenv("ALERT_COOLDOWN", "60.0"))  # seconds per-symbol cooldown (unless spread increased)
MAX_MONITORED = int(os.getenv("MAX_MONITORED", "200"))  # safety cap for displayed monitored symbols

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

CEX_PRIMARY = os.getenv("CEX_PRIMARY", "mexc")  # ccxt id

DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search/?q={q}"
GMGN_API = "https://gmgn.ai/defi/quotation/v1/tokens/search?keyword={q}"

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("live-monitor")

# ---------------- STATE ----------------
state: Dict[str, Any] = {
    "symbols": [],        # manual/auto-monitored symbols
    "chat_id": None,
    "msg_id": None,
    "monitoring": True,
    "alert_threshold_pct": ALERT_THRESHOLD_PCT,
    "live_to_telegram": False,
}
# runtime caches and trackers
dex_prices: Dict[str, float] = {}
dex_cache_ts: Dict[str, float] = {}        # timestamp when dex_prices set (TTL)
cex_prices: Dict[str, float] = {}
last_update: Dict[str, float] = {}
last_alert_time: Dict[str, float] = {}
last_alert_pct: Dict[str, float] = {}      # last alert pct for each symbol
active_spreads: Dict[str, Dict[str, Any]] = {}  # optional open spreads

# ---------------- SAVE / LOAD ----------------
def load_state():
    global state
    if os.path.exists(STATE_FILE):
        try:
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
        logger.debug("tg_send skipped (no token/chat_id)")
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
        if r.status_code != 200:
            logger.warning("tg_edit failed: %s %s", r.status_code, r.text)
        return r.json()
    except Exception as e:
        logger.exception("tg_edit error: %s", e)
        return None

# ---------------- UTIL: formatting ----------------
def now_str():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

def build_live_table_text() -> str:
    syms = list(state.get("symbols", []))[:MAX_MONITORED]
    if not syms:
        return "🟡 *No symbols monitored.* Use `/add SYMBOL` to add."
    lines = []
    lines.append("📡 *Live DEX ↔ CEX Monitor*")
    lines.append(f"_Updated: {now_str()}_\n")
    lines.append("`SYMBOL    DEX(USD)      CEX(USD)     Δ%   Last`")
    lines.append("`-----------------------------------------------------------`")
    for s in syms:
        dex = dex_prices.get(s)
        cex = cex_prices.get(s)
        dex_str = f"{dex:.8f}" if dex is not None else "—"
        cex_str = f"{cex:.8f}" if cex is not None else "—"
        pct_str = "—"
        if dex is not None and cex is not None and dex != 0:
            pct = (cex - dex) / dex * 100.0
            pct_str = f"{pct:+6.2f}%"
        lu = last_update.get(s)
        lu_str = datetime.utcfromtimestamp(lu).strftime("%H:%M:%S") if lu else "—"
        lines.append(f"`{s:<7}` {dex_str:>12}  {cex_str:>12}  {pct_str:>7}  {lu_str}")
    lines.append("\n`/add SYMBOL  /remove SYMBOL  /list  /alert <pct>  /live on|off`")
    return "\n".join(lines)

# ---------------- DEX fetchers (GMGN -> Dexscreener) ----------------
def fetch_from_gmgn(symbol: str) -> Optional[float]:
    try:
        url = GMGN_API.format(q=symbol)
        r = requests.get(url, timeout=7)
        r.raise_for_status()
        data = r.json()
        items = data.get("data") or []
        for it in items:
            price = it.get("price_usd") or it.get("priceUsd") or it.get("price")
            if price:
                return float(price)
    except Exception as e:
        logger.debug("gmgn err %s: %s", symbol, e)
    return None

def fetch_from_dexscreener(symbol: str) -> Optional[float]:
    try:
        url = DEXSCREENER_SEARCH.format(q=symbol)
        r = requests.get(url, timeout=7)
        r.raise_for_status()
        data = r.json()
        pairs = data.get("pairs") or []
        for p in pairs:
            price = p.get("priceUsd") or p.get("price")
            if price:
                return float(price)
        # fallback tokens array
        tokens = data.get("tokens") or []
        for t in tokens:
            for p in t.get("pairs", []):
                price = p.get("priceUsd") or p.get("price")
                if price:
                    return float(price)
    except Exception as e:
        logger.debug("dexscreener err %s: %s", symbol, e)
    return None

def fetch_price_from_dex(symbol: str, ttl: float = 20.0) -> Optional[float]:
    """Return cached dex price if TTL not expired, else fetch (GMGN -> Dexscreener)."""
    s = symbol.upper()
    now = time.time()
    ts = dex_cache_ts.get(s)
    if ts and (now - ts) < ttl and dex_prices.get(s) is not None:
        return dex_prices[s]
    # try GMGN then Dexscreener
    p = fetch_from_gmgn(s)
    if p is None:
        p = fetch_from_dexscreener(s)
    if p is not None:
        dex_prices[s] = float(p)
        dex_cache_ts[s] = time.time()
        last_update[s] = time.time()
    return p

# ---------------- CONTRACT MAPPING ----------------
def normalize_symbol_from_pair(pair: str) -> Optional[str]:
    """
    Try to extract base token (e.g. 'PEPE' from 'PEPE/USDT' or 'PEPEUSDT').
    Returns upper-case token or None.
    """
    if "/" in pair:
        base = pair.split("/")[0]
        return base.upper()
    # some exchanges use 'PEPEUSDT' style
    if pair.upper().endswith("USDT"):
        return pair[:-4].upper()
    return None

# ---------------- CEX scanner (scan all MEXC tickers) ----------------
class CEXScanner:
    def __init__(self, exchange_id="mexc"):
        self.exchange_id = exchange_id
        self.client = None

    def start_client(self):
        try:
            self.client = getattr(ccxt, self.exchange_id)({"enableRateLimit": True})
        except Exception as e:
            logger.exception("Init CEX client failed: %s", e)
            self.client = None

    def scan_all_tickers(self):
        """
        Fetch all tickers from CEX, extract USDT pairs, populate cex_prices dict with base token -> last price.
        Also attempts to collect a 1h percent change if available in ticker.info (used for top lists later).
        """
        if self.client is None:
            self.start_client()
            if self.client is None:
                return {}
        try:
            tickers = self.client.fetch_tickers()
        except Exception as e:
            logger.debug("fetch_tickers error: %s", e)
            return {}
        symbol_to_price = {}
        hourly_changes = {}  # optional: store any 1h change if provided
        for pair, t in tickers.items():
            if not pair:
                continue
            pair_u = pair.upper()
            # consider USDT or USD quoted pairs (common)
            if "USDT" not in pair_u and "/USD" not in pair_u and "USD" not in pair_u:
                continue
            base = normalize_symbol_from_pair(pair)
            if not base:
                continue
            last = t.get("last") or t.get("close") or (t.get("info") and (t.get("info").get("last") if isinstance(t.get("info"), dict) else None))
            try:
                if last is not None:
                    symbol_to_price[base] = float(last)
                    # try extract hourly change from info (various field names)
                    info = t.get("info") or {}
                    if isinstance(info, dict):
                        # many exchanges don't supply 1h; try common fields
                        for fld in ("priceChangePercent1h", "percentChange1h", "percent_change_1h", "change1h"):
                            if fld in info:
                                try:
                                    hourly_changes[base] = float(info.get(fld))
                                    break
                                except Exception:
                                    pass
                        # also some provide 'percentage' as 24h; skip
            except Exception:
                continue
        return {"prices": symbol_to_price, "hourly": hourly_changes}

# ---------------- ALERT PROCESS / AUTO-ADD ----------------
def process_symbol_for_signal(sym: str, cex_price: float):
    """Check dex price and evaluate spread; if >= threshold -> auto-add and alert (respect cooldown/increase rule)."""
    if cex_price is None:
        return
    s = sym.upper()
    dex_p = fetch_price_from_dex(s)  # uses cache TTL internally
    if dex_p is None or dex_p == 0:
        return
    pct = (cex_price - dex_p) / dex_p * 100.0
    now = time.time()
    thr = float(state.get("alert_threshold_pct", ALERT_THRESHOLD_PCT))
    last_ts = last_alert_time.get(s, 0)
    last_pct = last_alert_pct.get(s, -9999.0)
    # Should we alert?
    if pct >= thr:
        # allowed if cooldown passed OR pct > last_pct (spread increased)
        if (now - last_ts) >= ALERT_COOLDOWN or pct > last_pct:
            # register alert
            last_alert_time[s] = now
            last_alert_pct[s] = pct
            # add to monitored list if not present
            if s not in state["symbols"]:
                state["symbols"].append(s)
                # keep list size sane
                if len(state["symbols"]) > MAX_MONITORED:
                    state["symbols"] = state["symbols"][-MAX_MONITORED:]
                save_state()
            # compose message
            msg = (
                "🟢 *NEW SIGNAL (auto-added)*\n"
                f"Symbol: `{s}`\n"
                f"CEX ({CEX_PRIMARY.upper()}): `{cex_price:.8f}`\n"
                f"DEX (agg): `{dex_p:.8f}`\n"
                f"Spread: *{pct:.2f}%*  (threshold {thr:.2f}%)\n"
                f"Time: {now_str()}"
            )
            logger.info("AUTO ALERT %s %.2f%% (added to monitoring)", s, pct)
            tg_send(msg)
            # track active spread
            active_spreads[s] = {"opened_pct": pct, "open_ts": now, "dex_price": dex_p, "cex_price": cex_price}
    # also remove closed spreads if previously open and now below threshold/close logic
    if s in active_spreads:
        # if spread dropped below (thr * 0.2) we can close - but keep simple: close when below 0.5*thr
        close_thresh = max(0.5 * thr, 0.5)
        if pct < close_thresh:
            opened = active_spreads.pop(s, None)
            if opened:
                duration = int(now - opened.get("open_ts", now))
                msg = (
                    "✅ *Spread CLOSED*\n"
                    f"Symbol: `{s}`\n"
                    f"Now: CEX `{cex_price:.8f}` | DEX `{dex_p:.8f}`\n"
                    f"Spread: *{pct:.2f}%*\n"
                    f"Opened: *{opened.get('opened_pct'):.2f}%*, duration: {duration}s\n"
                    f"Time: {now_str()}"
                )
                logger.info("AUTO CLOSE %s %.2f%%", s, pct)
                tg_send(msg)

# ---------------- FLASK + SOCKET.IO ----------------
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

INDEX_HTML = """
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Live DEX ↔ CEX Monitor</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
</head><body class="bg-light">
<div class="container py-4">
  <h3>Live DEX ↔ CEX Monitor</h3>
  <div class="mb-2">
    <form id="addForm" class="row g-2">
      <div class="col-auto"><input id="symbol" class="form-control" placeholder="SYMBOL (e.g. PEPE)" autocomplete="off"></div>
      <div class="col-auto"><button class="btn btn-primary">Add</button></div>
      <div class="col-auto"><button id="clearBtn" class="btn btn-danger" type="button">Clear All</button></div>
    </form>
  </div>
  <div id="statusBadge" class="mb-2"></div>
  <div class="table-responsive"><table class="table table-sm table-bordered" id="liveTable">
    <thead class="table-light"><tr><th>Symbol</th><th>DEX (USD)</th><th>CEX (USD)</th><th>Δ%</th><th>Last</th></tr></thead>
    <tbody id="tbody"></tbody>
  </table></div>
  <div class="small text-muted">Connected clients: <span id="clients">0</span></div>
</div>
<script>
  const socket = io();
  const tbody = document.getElementById("tbody");
  const clientsEl = document.getElementById("clients");
  const statusBadge = document.getElementById("statusBadge");
  socket.on("connect", ()=>{console.log("connected");});
  socket.on("live.update", (data)=>{
    const symbols = data.symbols || [];
    tbody.innerHTML = "";
    symbols.forEach(s => {
      const dex = data.dex_prices[s];
      const cex = data.cex_prices[s];
      let dexStr = dex == null ? "—" : Number(dex).toFixed(8);
      let cexStr = cex == null ? "—" : Number(cex).toFixed(8);
      let pct = "—";
      if (dex != null && cex != null && dex !== 0) pct = ((cex - dex)/dex*100).toFixed(2) + "%";
      const lu = data.last_update && data.last_update[s] ? new Date(data.last_update[s]*1000).toISOString().substr(11,8) : "—";
      const tr = document.createElement("tr");
      const stale = data.last_update && data.last_update[s] && (Date.now()/1000 - data.last_update[s] > 10);
      tr.innerHTML = `<td><strong>${s}</strong></td><td>${dexStr}</td><td>${cexStr}</td><td>${pct}</td><td>${lu}</td>`;
      if (stale) tr.style.opacity = 0.6;
      tbody.appendChild(tr);
    });
  });
  socket.on("clients", (n)=>{ clientsEl.innerText = n; });
  socket.on("status", (txt)=>{ statusBadge.innerHTML = '<span class="badge bg-info">'+txt+'</span>'; setTimeout(()=>statusBadge.innerHTML="",3000); });

  document.getElementById("addForm").addEventListener("submit", (e)=>{ e.preventDefault(); const sym = document.getElementById("symbol").value.trim().toUpperCase(); if (!sym) return; socket.emit("add_symbol", sym); document.getElementById("symbol").value = ""; });
  document.getElementById("clearBtn").addEventListener("click", ()=>{ if (!confirm("Clear all symbols?")) return; socket.emit("clear_symbols"); });
</script>
</body></html>
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
            txt_lines = [f"Symbols: {', '.join(syms) if syms else '—'}"]
            txt_lines.append(f"Alert threshold: {state.get('alert_threshold_pct'):.2f}%")
            txt_lines.append(f"Active spreads: {len(active_spreads)}")
            tg_send("\n".join(txt_lines))
        else:
            tg_send("❓ Unknown command. /help")
    except Exception as e:
        logger.exception("cmd error: %s", e)
        tg_send("⚠️ Error processing command.")
    return jsonify({"ok": True})

# ---------------- SocketIO handlers ----------------
@socketio.on("connect")
def on_connect():
    try:
        participants = 1
        if hasattr(socketio, "server") and getattr(socketio, "server") is not None:
            try:
                participants = len(socketio.server.manager.get_participants('/', '/'))
            except Exception:
                participants = 1
        emit("clients", participants)
        emit("live.update", {"symbols": state.get("symbols", []), "dex_prices": dex_prices, "cex_prices": cex_prices, "last_update": last_update, "time": time.time()})
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

# ---------------- ORCHESTRATOR ----------------
class Orchestrator:
    def __init__(self):
        self.loop = None
        self.thread = None
        self.cex_scanner = CEXScanner(CEX_PRIMARY)
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
            logger.exception("orchestrator loop error: %s", e)

    async def _main(self):
        load_state()
        logger.info("Starting background components: CEX scanner + broadcaster")
        # run two coroutines: cex scanner and broadcaster
        async def cex_worker():
            while True:
                try:
                    res = await self.loop.run_in_executor(None, self.cex_scanner.scan_all_tickers)
                    prices = res.get("prices", {}) if isinstance(res, dict) else {}
                    # update cex_prices and last_update
                    ts = time.time()
                    # iterate found prices and process
                    for sym, p in prices.items():
                        # set cex price
                        cex_prices[sym] = float(p)
                        last_update[sym] = ts
                        # process for auto-signal
                        try:
                            process_symbol_for_signal(sym, float(p))
                        except Exception:
                            pass
                    # also keep any existing cex_prices for symbols not in this batch
                except Exception as e:
                    logger.debug("cex_worker error: %s", e)
                await asyncio.sleep(POLL_INTERVAL_CEX)

        async def broadcaster():
            while True:
                try:
                    # emit live update
                    socketio.emit("live.update", {"symbols": state.get("symbols", []), "dex_prices": dex_prices, "cex_prices": cex_prices, "last_update": last_update, "time": time.time()})
                    # optionally edit telegram live panel
                    if state.get("live_to_telegram") and state.get("chat_id"):
                        try:
                            txt = build_live_table_text()
                            if not state.get("msg_id"):
                                res = tg_send(txt)
                                if res and isinstance(res, dict):
                                    mid = res.get("result", {}).get("message_id")
                                    if mid:
                                        state["msg_id"] = int(mid)
                                        save_state()
                            else:
                                tg_edit(state["msg_id"], txt)
                        except Exception as e:
                            logger.debug("tg live edit err: %s", e)
                except Exception as e:
                    logger.exception("broadcaster error: %s", e)
                await asyncio.sleep(LIVE_BROADCAST_INTERVAL)

        # schedule tasks
        t1 = asyncio.create_task(cex_worker())
        t2 = asyncio.create_task(broadcaster())
        try:
            await asyncio.gather(t1, t2)
        except asyncio.CancelledError:
            logger.info("Orchestrator cancelled")
        finally:
            # nothing special to shutdown
            pass

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
    logger.info("🚀 Starting Live DEX<->CEX monitor (auto-add for spread >= %.2f%%)", ALERT_THRESHOLD_PCT)
    load_state()
    # set webhook (if token + WEBHOOK_URL provided)
    if TELEGRAM_TOKEN and WEBHOOK_URL:
        try:
            url = WEBHOOK_URL.rstrip("/") + "/webhook"
            r = requests.get(f"{TELEGRAM_API}/setWebhook?url={url}", timeout=10)
            logger.info("Set webhook result: %s", r.text[:200])
        except Exception as e:
            logger.warning("Failed to set webhook: %s", e)

    # start background orchestrator (async tasks)
    orchestrator.start()

    # run Flask-SocketIO server (eventlet recommended)
    # Note: for production, run behind proper server; eventlet is easiest here.
    socketio.run(app, host="0.0.0.0", port=PORT)