#!/usr/bin/env python3
"""
main.py - Live MEXC <-> DEX <-> Binance monitor
Auto-scans MEXC tickers, fetches DEX prices (GMGN/Dexscreener),
fetches Binance tickers, computes spreads, shows top-10 by Δ%,
sends Telegram alerts via webhook (edits message when live_to_telegram).
"""

import os
import time
import json
import logging
import asyncio
import requests
from datetime import datetime
from threading import Thread
from typing import Dict, Any, List, Optional, Tuple
from flask import Flask, request, render_template_string, jsonify
from flask_socketio import SocketIO, emit

# Blocking ccxt used in executor (no ccxt.pro)
try:
    import ccxt
except Exception:
    ccxt = None

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # https://anom-nyc9.onrender.com
YOUR_TELEGRAM_ID = 6053907025              # send alerts only to this id
PORT = int(os.getenv("PORT", "10000"))

POLL_INTERVAL_DEX = 11.0       # seconds between DEX polls
POLL_INTERVAL_CEX = 11.0       # seconds between CEX polls
LIVE_BROADCAST_INTERVAL = 11.0 # socketio + telegram edit interval
ALERT_OPEN_PCT = 3.0           # open alert threshold %
ALERT_CLOSE_PCT = 0.5         # close threshold (close when drops below this)
MIN_PRICE_FILTER = 0.001    # ignore tokens cheaper than this (usd)
MAX_SPREAD_FILTER = 100.0      # ignore absurd spreads > 300%
ALERT_COOLDOWN = 60.0          # seconds per-symbol cooldown
TOP_SHOW = 10                  # top N to display

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# DEX APIs
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search/?q={q}"
GMGN_API = "https://gmgn.ai/defi/quotation/v1/tokens/search?keyword={q}"

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("mexc-dex-monitor")

# ---------------- STATE ----------------
state: Dict[str, Any] = {
    "symbols": [],            # optional manual list (we also auto-add from MEXC)
    "chat_id": None,
    "msg_id": None,
    "live_to_telegram": True,
}
# runtime caches
dex_prices: Dict[str, float] = {}
mexc_prices: Dict[str, float] = {}
binance_prices: Dict[str, float] = {}
last_update: Dict[str, float] = {}
active_alerts: Dict[str, Dict[str, Any]] = {}  # sym -> {opened_pct, open_ts}
last_alert_time: Dict[str, float] = {}

# store list of MEXC symbols discovered
mexc_available_symbols: List[str] = []

# ---------------- SAVE / LOAD ----------------
STATE_FILE = "state.json"
def load_state():
    global state
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                s = json.load(f)
                state.update(s)
                logger.info("Loaded state with %d symbols", len(state.get("symbols", [])))
    except Exception as e:
        logger.debug("load_state error: %s", e)

def save_state():
    try:
        with open(STATE_FILE + ".tmp", "w") as f:
            json.dump(state, f, indent=2)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except Exception as e:
        logger.debug("save_state error: %s", e)

# ---------------- TELEGRAM ----------------
def tg_send_to(chat_id: int, text: str) -> Optional[dict]:
    if not TELEGRAM_TOKEN or not chat_id:
        return None
    try:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}
        r = requests.post(TELEGRAM_API + "/sendMessage", json=payload, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug("tg_send_to error: %s", e)
        return None

def tg_edit(chat_id: int, message_id: int, text: str):
    if not TELEGRAM_TOKEN or not chat_id:
        return None
    try:
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"}
        r = requests.post(TELEGRAM_API + "/editMessageText", json=payload, timeout=10)
        if r.status_code != 200:
            logger.debug("tg_edit failed %s %s", r.status_code, r.text)
        return r.json()
    except Exception as e:
        logger.debug("tg_edit error: %s", e)
        return None

# ---------------- DEX fetchers (sync) ----------------
def fetch_from_gmgn(symbol: str) -> Optional[float]:
    try:
        q = symbol.upper()
        url = GMGN_API.format(q=q)
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()
        items = data.get("data") or []
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
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()
        pairs = data.get("pairs") or []
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
    # priority: gmgn -> dexscreener
    v = fetch_from_gmgn(symbol)
    if v is not None:
        return v
    return fetch_from_dexscreener(symbol)

# ---------------- CEX helpers (sync via ccxt) ----------------
def discover_mexc_symbols() -> List[str]:
    global ccxt
    if ccxt is None:
        logger.warning("ccxt not installed; cannot discover MEXC symbols")
        return []
    try:
        mex = getattr(ccxt, "mexc")({"enableRateLimit": True})
        markets = mex.fetch_markets()
        syms = []
        for k, m in markets.items():
            # keep linear/perp/usdt pairs
            if isinstance(k, str) and ("USDT" in k.upper() or k.upper().endswith("USD")):
                syms.append(k)
        syms = sorted(set(syms))
        logger.info("Discovered %d MEXC symbols", len(syms))
        return syms
    except Exception as e:
        logger.debug("discover_mexc_symbols error: %s", e)
        return []

def fetch_cex_tickers(exchange_id: str) -> Dict[str, Dict]:
    """Return tickers dict from ccxt.fetch_tickers (sync)"""
    out = {}
    if ccxt is None:
        return out
    try:
        ex = getattr(ccxt, exchange_id)({"enableRateLimit": True})
        tickers = ex.fetch_tickers()
        return tickers or {}
    except Exception as e:
        logger.debug("fetch_cex_tickers %s error: %s", exchange_id, e)
        return {}

# ---------------- SPREAD / ALERT logic ----------------
def compute_spread_and_rank() -> List[Dict[str, Any]]:
    """
    For all known symbols (mexc_available_symbols), compute dex price + cex prices,
    compute percent spreads (cex - dex) / dex * 100, apply filters, and return
    sorted list by absolute pct desc. Each item: {symbol, dex, mexc, binance, pct}
    """
    rows = []
    for pair in mexc_available_symbols:
        # canonical token symbol (strip /USDT suffix)
        # we will use pair formats like 'PEPE/USDT' -> token 'PEPE'
        tok = pair.split('/')[0] if '/' in pair else pair
        tok = tok.upper()
        # get dex price by token name
        dex = dex_prices.get(tok)
        # get mexc price by token pair: attempt direct mapping
        mexp = mexc_prices.get(tok)
        binp = binance_prices.get(tok)
        # if none available, skip
        if dex is None and mexp is None and binp is None:
            continue
        # prefer dex taken from direct mapping; ensure numeric
        if dex is None or dex == 0:
            # try other heuristics if dex missing: attempt fetch from API on the fly (cheap)
            dex = fetch_price_from_dex(tok)
            if dex:
                dex_prices[tok] = dex
        # apply min price filter
        if dex is None and mexp is None and binp is None:
            continue
        # compute best cex price (prefer mexc then binance)
        # compute pct for each cex vs dex if dex exists
        best_pct = None
        best_side = None
        for ex_name, price in (("MEXC", mexp), ("BINANCE", binp)):
            if price is None or dex is None or dex == 0:
                continue
            pct = (price - dex) / dex * 100.0
            if abs(pct) > MAX_SPREAD_FILTER:
                continue
            if best_pct is None or abs(pct) > abs(best_pct):
                best_pct = pct
                best_side = ex_name
        # if dex exists but pct tiny, we still include (for top sorting later)
        if dex is None:
            # skip if only cex and no dex price
            continue
        # price sanity
        if dex < MIN_PRICE_FILTER and (mexp := mexp) is not None and mexp < MIN_PRICE_FILTER:
            continue
        rows.append({
            "symbol": tok,
            "pair": pair,
            "dex": dex,
            "mexc": mexp,
            "binance": binp,
            "best_pct": best_pct if best_pct is not None else 0.0,
            "best_side": best_side,
            "last_update": last_update.get(tok, 0)
        })
    # sort by absolute best_pct desc
    rows_sorted = sorted(rows, key=lambda r: abs(r.get("best_pct", 0.0)), reverse=True)
    return rows_sorted

def try_alert_for_row(row: Dict[str, Any]):
    sym = row["symbol"]
    pct = row.get("best_pct", 0.0) or 0.0
    now = time.time()
    # filters
    if abs(pct) < ALERT_OPEN_PCT:
        # if active and dropped below close threshold -> send close
        if sym in active_alerts and abs(pct) <= ALERT_CLOSE_PCT:
            opened = active_alerts.pop(sym, None)
            if opened:
                # send close message
                txt = ("✅ *Spread CLOSED*\n"
                       f"Symbol: `{sym}`\n"
                       f"Opened: *{opened.get('opened_pct'):.2f}%*\n"
                       f"Now: *{pct:.2f}%*  (dex {row['dex']:.8f} mexc {row.get('mexc') or 0:.8f})\n"
                       f"Duration: {int(now - opened.get('open_ts'))}s\n"
                       f"Time: {datetime.utcfromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S UTC')}")
                logger.info("ALERT CLOSE %s %.2f%%", sym, pct)
                tg_send_to(YOUR_TELEGRAM_ID, txt)
                last_alert_time[sym] = now
        return

    # open condition: if not active and passes threshold and not in cooldown
    last = last_alert_time.get(sym, 0)
    active = active_alerts.get(sym)
    if active is None:
        if now - last < ALERT_COOLDOWN:
            # allow only if pct increased by >0.5% vs last opened_pct (if any)
            return
        # open
        active_alerts[sym] = {"opened_pct": abs(pct), "open_ts": now}
        last_alert_time[sym] = now
        txt = ("🔔 *Spread OPENED*\n"
               f"Symbol: `{sym}`\n"
               f"Side: *{row.get('best_side') or 'CEX'}*\n"
               f"Spread: *{pct:.2f}%* (dex {row['dex']:.8f})\n"
               f"MEXC: `{row.get('mexc') or 0:.8f}`  BIN: `{row.get('binance') or 0:.8f}`\n"
               f"Time: {datetime.utcfromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        logger.info("ALERT OPEN %s %.2f%%", sym, pct)
        tg_send_to(YOUR_TELEGRAM_ID, txt)
    else:
        # already active: if spread increased significantly (> previous + 0.5%) then notify update
        prev = active.get("opened_pct", 0.0)
        if abs(pct) > (prev + 0.5):
            active_alerts[sym]["opened_pct"] = abs(pct)
            active_alerts[sym]["open_ts"] = active.get("open_ts", time.time())
            # update message
            txt = ("🔺 *Spread INCREASED*\n"
                   f"Symbol: `{sym}`\n"
                   f"New spread: *{pct:.2f}%* (was {prev:.2f}%)\n"
                   f"Time: {datetime.utcfromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S UTC')}")
            logger.info("ALERT INCREASE %s %.2f%%", sym, pct)
            tg_send_to(YOUR_TELEGRAM_ID, txt)
            last_alert_time[sym] = now

# ---------------- FLASK + SOCKET.IO UI ----------------
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Live MEXC ↔ DEX Monitor</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
  <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
</head>
<body class="bg-light">
<div class="container py-4">
  <h4>📡 Live MEXC ↔ DEX ↔ Binance Monitor</h4>
  <div class="mb-2">
    <button id="refresh" class="btn btn-sm btn-secondary">Refresh</button>
    <span class="ms-3">Top <strong>{{top}}</strong> by |Δ%|</span>
  </div>
  <div class="table-responsive">
    <table class="table table-sm table-bordered" id="liveTable">
      <thead><tr><th>Symbol</th><th>1hΔ%</th><th>DEX (USD)</th><th>MEXC (USD)</th><th>BIN (USD)</th><th>Δ% (best)</th><th>Last</th></tr></thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
  <div class="small text-muted">Connected: <span id="clients">0</span></div>
</div>
<script>
  const socket = io();
  const tbody = document.getElementById("tbody");
  socket.on("connect", () => console.log("connected"));
  socket.on("live.update", (data) => {
    const rows = data.rows || [];
    tbody.innerHTML = "";
    for (const r of rows) {
      const dex = r.dex == null ? "—" : Number(r.dex).toFixed(8);
      const mex = r.mexc == null ? "—" : Number(r.mexc).toFixed(8);
      const bin = r.binance == null ? "—" : Number(r.binance).toFixed(8);
      const pct = r.best_pct == null ? "—" : (Number(r.best_pct).toFixed(2) + "%");
      const lu = r.last_update ? new Date(r.last_update*1000).toISOString().substr(11,8) : "—";
      const tr = document.createElement("tr");
      const highlight = Math.abs(r.best_pct || 0) >= {{alert_pct}} ? "table-warning" : "";
      tr.className = highlight;
      tr.innerHTML = `<td><strong>${r.symbol}</strong></td><td>—</td><td>${dex}</td><td>${mex}</td><td>${bin}</td><td>${pct}</td><td>${lu}</td>`;
      tbody.appendChild(tr);
    }
  });
  socket.on("clients", (n) => { document.getElementById("clients").innerText = n; });
  document.getElementById("refresh").addEventListener("click", () => socket.emit("force_refresh"));
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(INDEX_HTML, top=TOP_SHOW, alert_pct=ALERT_OPEN_PCT)

# Telegram webhook endpoint
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
    logger.info("Webhook cmd from %s: %s", cid, text.splitlines()[0][:200])
    # Basic commands
    parts = text.split()
    cmd = parts[0].lower()
    try:
        if cmd == "/start":
            tg_send_to(cid, "Live monitor online.")
        elif cmd == "/help":
            tg_send_to(cid, "Commands: /status /live on|off /top N /alert X")
        elif cmd == "/status":
            tg_send_to(cid, f"Symbols tracked: {len(mexc_available_symbols)}  Alerts active: {len(active_alerts)}")
        elif cmd == "/live":
            if len(parts) >= 2 and parts[1].lower() in ("on","off"):
                state["live_to_telegram"] = parts[1].lower()=="on"
                save_state()
                tg_send_to(cid, f"live_to_telegram = {state['live_to_telegram']}")
        elif cmd == "/top":
            if len(parts) >= 2:
                try:
                    n = int(parts[1])
                    global TOP_SHOW
                    TOP_SHOW = max(1, min(50, n))
                    tg_send_to(cid, f"Top display set to {TOP_SHOW}")
                except:
                    tg_send_to(cid, "Usage: /top N")
        elif cmd == "/alert":
            if len(parts) >= 2:
                try:
                    v = float(parts[1])
                    global ALERT_OPEN_PCT
                    ALERT_OPEN_PCT = max(0.1, v)
                    tg_send_to(cid, f"Alert threshold set to {ALERT_OPEN_PCT}%")
                except:
                    tg_send_to(cid, "Usage: /alert X")
        else:
            tg_send_to(cid, "Unknown command. /help")
    except Exception as e:
        logger.debug("webhook cmd error: %s", e)
    return jsonify({"ok": True})

# SocketIO handlers
@socketio.on("connect")
def on_connect():
    try:
        emit("clients", 1)
        # initial push
        emit("live.update", {"rows": []})
    except Exception:
        pass

@socketio.on("force_refresh")
def on_force_refresh():
    # noop; background loop will emit next
    emit("status", "Refresh scheduled")

# ---------------- ORCHESTRATION: background async loop ----------------
class Orchestrator:
    def __init__(self):
        self.loop = None
        self.thread = None
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
        # initial discovery of MEXC symbols
        await self._discover_mexc_symbols()
        # start periodic tasks
        tasks = [
            asyncio.create_task(self._periodic_fetch_cex()),
            asyncio.create_task(self._periodic_fetch_dex()),
            asyncio.create_task(self._broadcaster_loop())
        ]
        await asyncio.gather(*tasks)

    async def _discover_mexc_symbols(self):
        global mexc_available_symbols
        def sync_discover():
            return discover_mexc_symbols()
        try:
            mexc_available_symbols = await asyncio.get_event_loop().run_in_executor(None, sync_discover)
        except Exception as e:
            logger.debug("discover exec error: %s", e)
            mexc_available_symbols = []

    async def _periodic_fetch_cex(self):
        """Fetch tickers from MEXC and Binance periodically (blocking ccxt in executor)"""
        while True:
            try:
                loop = asyncio.get_event_loop()
                def sync_fetch():
                    mex = fetch_cex_tickers("mexc")
                    binance = fetch_cex_tickers("binance")
                    return mex, binance
                mex_t, bin_t = await loop.run_in_executor(None, sync_fetch)
                # update mexc_prices and binance_prices by token symbol
                # map pair names to token (strip /USDT etc.)
                now = time.time()
                if mex_t:
                    for pair, tk in mex_t.items():
                        try:
                            token = pair.split('/')[0].upper()
                            last = tk.get("last") or tk.get("close") or tk.get("price")
                            if last is not None:
                                mexc_prices[token] = float(last)
                                last_update[token] = now
                        except Exception:
                            continue
                if bin_t:
                    for pair, tk in bin_t.items():
                        try:
                            token = pair.split('/')[0].upper()
                            last = tk.get("last") or tk.get("close") or tk.get("price")
                            if last is not None:
                                binance_prices[token] = float(last)
                                last_update[token] = now
                        except Exception:
                            continue
                # refresh discovered MEXC symbols occasionally (every minute)
                if not mexc_available_symbols or (int(now) % 60 < 3):
                    await self._discover_mexc_symbols()
            except Exception as e:
                logger.debug("periodic_fetch_cex error: %s", e)
            await asyncio.sleep(POLL_INTERVAL_CEX)

    async def _periodic_fetch_dex(self):
        """Periodically fetch dex prices for tokens seen on CEX (in executor)"""
        while True:
            try:
                # build tokens to query: from mexc_available_symbols convert to tokens
                tokens = set()
                for pair in mexc_available_symbols:
                    token = pair.split('/')[0].upper() if '/' in pair else pair.upper()
                    tokens.add(token)
                # run fetches in threadpool
                loop = asyncio.get_event_loop()
                coros = [loop.run_in_executor(None, fetch_price_from_dex, tok) for tok in list(tokens)]
                results = await asyncio.gather(*coros, return_exceptions=True)
                now = time.time()
                for tok, res in zip(list(tokens), results):
                    if isinstance(res, Exception) or res is None:
                        continue
                    try:
                        dex_prices[tok] = float(res)
                        last_update[tok] = now
                    except Exception:
                        continue
            except Exception as e:
                logger.debug("periodic_fetch_dex error: %s", e)
            await asyncio.sleep(POLL_INTERVAL_DEX)

    async def _broadcaster_loop(self):
        """Compute spreads, alert and broadcast top rows to UI/telegram"""
        while True:
            try:
                rows = compute_spread_and_rank()
                # process top candidates for alerts
                for r in rows[:50]:  # evaluate first 50 for possible alerts
                    try:
                        try_alert_for_row(r)
                    except Exception:
                        continue
                # prepare top-N for UI
                topn = rows[:TOP_SHOW]
                socketio.emit("live.update", {"rows": topn})
                # optionally edit Telegram live panel
                if state.get("live_to_telegram") and state.get("chat_id"):
                    try:
                        txt = build_telegram_table_text(topn)
                        if not state.get("msg_id"):
                            res = tg_send_to(state["chat_id"], txt)
                            if isinstance(res, dict):
                                mid = res.get("result", {}).get("message_id")
                                if mid:
                                    state["msg_id"] = int(mid)
                                    save_state()
                        else:
                            tg_edit(state["chat_id"], state["msg_id"], txt)
                    except Exception as e:
                        logger.debug("tg live edit err: %s", e)
            except Exception as e:
                logger.debug("broadcaster loop error: %s", e)
            await asyncio.sleep(LIVE_BROADCAST_INTERVAL)

# ---------------- Telegram table builder ----------------
def build_telegram_table_text(rows: List[Dict[str, Any]]) -> str:
    lines = []
    lines.append("📡 *Live MEXC ↔ DEX ↔ BIN Monitor*")
    lines.append(f"_Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}_\n")
    lines.append("`SYMBOL     DEX(USD)      MEXC(USD)     BIN(USD)     Δ%`")
    lines.append("`------------------------------------------------------------`")
    for r in rows:
        sym = r["symbol"]
        dex = r.get("dex")
        mex = r.get("mexc")
        binp = r.get("binance")
        dex_s = f"{dex:.8f}" if dex else "—"
        mex_s = f"{mex:.8f}" if mex else "—"
        bin_s = f"{binp:.8f}" if binp else "—"
        pct = r.get("best_pct") or 0.0
        pct_s = f"{pct:+6.2f}%"
        lines.append(f"`{sym:<9}` {dex_s:>12}  {mex_s:>12}  {bin_s:>12}  {pct_s:>7}")
    lines.append("\n`/status  /live on|off  /alert X`")
    return "\n".join(lines)

# ---------------- BOOT ----------------
orchestrator = Orchestrator()

if __name__ == "__main__":
    logger.info("Starting Live MEXC<->DEX Monitor")
    load_state()
    # set webhook to provided WEBHOOK_URL (if token + URL set)
    if TELEGRAM_TOKEN and WEBHOOK_URL:
        try:
            url = WEBHOOK_URL.rstrip("/") + "/webhook"
            r = requests.get(f"{TELEGRAM_API}/setWebhook?url={url}", timeout=10)
            logger.info("Set webhook result: %s", r.text[:200])
        except Exception as e:
            logger.warning("Failed to set webhook: %s", e)
    # ensure chat_id stored if you already want direct alerts to YOUR_TELEGRAM_ID
    if not state.get("chat_id"):
        state["chat_id"] = YOUR_TELEGRAM_ID
        save_state()

    orchestrator.start()
    # run flask socketio app (threading mode)
    socketio.run(app, host="0.0.0.0", port=PORT)