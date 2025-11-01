#!/usr/bin/env python3
# main.py - Live MEXC <-> DEX (± BINANCE) monitor
# - auto-scan MEXC futures markets
# - compute 1h % change (via fetch_ohlcv)
# - top-10 by 1h% shown in web + Telegram
# - filter real spreads, threshold 3%
# - cooldown 60s unless spread increases
# - live updates every 11s
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

try:
    import ccxt.pro as ccxtpro  # optional
except Exception:
    ccxtpro = None
import ccxt  # used for REST fetches (supports many exchanges)

# -------- CONFIG --------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # e.g. https://yourapp.example.com
PORT = int(os.getenv("PORT", "10000"))

STATE_FILE = "state.json"
LIVE_BROADCAST_INTERVAL = float(os.getenv("LIVE_BROADCAST_INTERVAL", "11.0"))  # seconds
POLL_INTERVAL_MARKETS = float(os.getenv("POLL_INTERVAL_MARKETS", "300"))  # refresh market list every N sec
SPREAD_ALERT_PCT = float(os.getenv("SPREAD_ALERT_PCT", "3.0"))  # alert threshold (3%)
COOLDOWN_S = int(os.getenv("COOLDOWN_S", "60"))  # cooldown after first alert (seconds)
MAX_TOP = int(os.getenv("MAX_TOP", "10"))

CEX_PRIMARY = os.getenv("CEX_PRIMARY", "mexc")   # main exchange to scan (mexc)
CEX_SECONDARY = os.getenv("CEX_SECONDARY", "binance")  # optional second CEX for comparison

DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search/?q={q}"
GMGN_API = "https://gmgn.ai/defi/quotation/v1/tokens/search?keyword={q}"

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# -------- LOGGING --------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("mexc-monitor")

# -------- STATE --------
state: Dict[str, Any] = {
    "symbols": [],  # user-added symbols (kept for manual control); auto-added signals stored separately
    "chat_id": None,
    "msg_id": None,
    "monitoring": True,
    "live_to_telegram": False,
}

# runtime caches
cex_markets_mexc: Dict[str, Any] = {}
cex_markets_binance: Dict[str, Any] = {}
dex_prices: Dict[str, float] = {}
cex_prices: Dict[str, Dict[str, float]] = {}  # symbol -> {"mexc": price, "binance": price}
oneh_changes: Dict[str, float] = {}  # symbol -> 1h % change (from CEX primary)
last_signal: Dict[str, Dict[str, Any]] = {}  # symbol -> {"ts":..., "spread":..., "opened_pct": ...}

# -------- persistence --------
def load_state():
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
        with open(STATE_FILE + ".tmp", "w") as f:
            json.dump(state, f, indent=2)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except Exception as e:
        logger.exception("save_state error: %s", e)

# -------- Telegram helpers --------
def tg_send(text: str) -> Optional[dict]:
    if not TELEGRAM_TOKEN or not state.get("chat_id"):
        logger.debug("tg_send skipped (no token/chat_id)")
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

# -------- DEX fetchers (simple, best-effort) --------
def fetch_from_gmgn(symbol: str) -> Optional[float]:
    try:
        url = GMGN_API.format(q=symbol)
        r = requests.get(url, timeout=6)
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
        url = DEXSCREENER_SEARCH.format(q=symbol)
        r = requests.get(url, timeout=6)
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
    res = fetch_from_gmgn(symbol)
    if res is not None:
        return res
    return fetch_from_dexscreener(symbol)

# -------- CEX helpers: use ccxt for market list + ohlcv --------
def init_ccxt_exchange(id: str, params: Optional[dict] = None) -> ccxt.Exchange:
    params = params or {}
    ex = None
    try:
        ex_cls = getattr(ccxt, id)
        ex = ex_cls({"enableRateLimit": True, **params})
        # ensure futures mode if possible
        try:
            if hasattr(ex, "options"):
                ex.options = {**(ex.options or {}), "defaultType": "future"}
        except Exception:
            pass
        return ex
    except Exception as e:
        logger.exception("init ccxt %s error: %s", id, e)
        raise

async def refresh_mexc_markets(loop) -> None:
    """Load MEXC futures markets (store normalized symbols)"""
    global cex_markets_mexc
    try:
        ex = await loop.run_in_executor(None, lambda: init_ccxt_exchange("mexc"))
        # some ccxt builds need load_markets() call
        markets = await loop.run_in_executor(None, ex.load_markets)
        # filter futures/perp markets where quote == USDT
        out = {}
        for sym, info in markets.items():
            m = info or {}
            quote = m.get("quote") or m.get("quoteId") or m.get("symbol", "")
            # include if contract or future/perp
            is_future = m.get("contract") or (m.get("type") == "future") or ("PERP" in sym.upper())
            if is_future and ("/USDT" in sym or sym.endswith("USDT")):
                out[sym] = m
        cex_markets_mexc = out
        logger.info("MEXC symbols refreshed: %d", len(cex_markets_mexc))
    except Exception as e:
        logger.exception("refresh_mexc_markets error: %s", e)

async def refresh_binance_markets(loop) -> None:
    global cex_markets_binance
    try:
        ex = await loop.run_in_executor(None, lambda: init_ccxt_exchange("binance"))
        markets = await loop.run_in_executor(None, ex.load_markets)
        out = {}
        for sym, m in markets.items():
            is_future = m.get("contract") or (m.get("info", {}).get("contractType") is not None)
            if is_future and ("/USDT" in sym or sym.endswith("USDT")):
                out[sym] = m
        cex_markets_binance = out
        logger.info("Binance (spot/futures) symbols refreshed: %d", len(cex_markets_binance))
    except Exception as e:
        logger.exception("refresh_binance_markets error: %s", e)

# helper to fetch last price via ccxt fetch_ticker (sync wrapped)
def fetch_ticker_price_sync(exchange_id: str, pair: str) -> Optional[float]:
    try:
        ex = init_ccxt_exchange(exchange_id)
        t = ex.fetch_ticker(pair)
        last = t.get("last") or t.get("close") or t.get("info", {}).get("lastPrice")
        if last is None:
            return None
        return float(last)
    except Exception:
        return None

# helper to fetch 1h change via ohlcv (uses last two 1h closes)
def fetch_1h_change_sync(exchange_id: str, pair: str) -> Optional[float]:
    try:
        ex = init_ccxt_exchange(exchange_id)
        # many exchanges support '1h' timeframe
        ohlcv = ex.fetch_ohlcv(pair, timeframe='1h', limit=2)
        if not ohlcv or len(ohlcv) < 2:
            return None
        prev_close = float(ohlcv[-2][4])
        last_close = float(ohlcv[-1][4])
        if prev_close == 0:
            return None
        pct = (last_close - prev_close) / prev_close * 100.0
        return pct
    except Exception:
        return None

# -------- Core monitoring logic --------
def is_reasonable_spread(dex_price: float, cex_price: float) -> bool:
    if dex_price is None or cex_price is None:
        return False
    if dex_price <= 0 or cex_price <= 0:
        return False
    ratio = cex_price / dex_price if dex_price != 0 else float('inf')
    # filter out absurd ratios
    if ratio < 0.0001 or ratio > 10000:
        return False
    # also absolute price sanity
    if dex_price < 1e-12 and cex_price < 1e-12:
        return False
    return True

def maybe_alert(symbol: str, dex_price: float, cex_price: float, spread_pct: float):
    now = time.time()
    rec = last_signal.get(symbol)
    # if no prior, send if >= threshold
    if rec is None:
        if spread_pct >= SPREAD_ALERT_PCT:
            last_signal[symbol] = {"ts": now, "spread": spread_pct}
            msg = (
                "🔔 *Spread OPENED*\n"
                f"Symbol: `{symbol}`\n"
                f"DEX price: `{dex_price:.8f}`\n"
                f"MEXC price: `{cex_price:.8f}`\n"
                f"Spread: *{spread_pct:.2f}%*\n"
                f"1h Δ%: `{oneh_changes.get(symbol, 0):+.2f}%`\n"
                f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
            )
            logger.info("ALERT %s %.2f%%", symbol, spread_pct)
            tg_send(msg)
        return
    # if prior exists, check cooldown
    elapsed = now - rec.get("ts", 0)
    prev_spread = rec.get("spread", 0)
    # allow re-alert if spread increased meaningfully (> prev + 1%) even within cooldown
    if spread_pct > prev_spread + 1.0:
        last_signal[symbol] = {"ts": now, "spread": spread_pct}
        msg = (
            "🔔 *Spread INCREASED*\n"
            f"Symbol: `{symbol}`\n"
            f"DEX price: `{dex_price:.8f}`\n"
            f"MEXC price: `{cex_price:.8f}`\n"
            f"Spread: *{spread_pct:.2f}%*  (was {prev_spread:.2f}%)\n"
            f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        logger.info("ALERT INCREASE %s %.2f%%", symbol, spread_pct)
        tg_send(msg)
        return
    # otherwise only allow new alert after cooldown
    if elapsed >= COOLDOWN_S and spread_pct >= SPREAD_ALERT_PCT:
        last_signal[symbol] = {"ts": now, "spread": spread_pct}
        msg = (
            "🔔 *Spread RE-OPENED*\n"
            f"Symbol: `{symbol}`\n"
            f"DEX price: `{dex_price:.8f}`\n"
            f"MEXC price: `{cex_price:.8f}`\n"
            f"Spread: *{spread_pct:.2f}%*\n"
            f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        logger.info("ALERT REOPEN %s %.2f%%", symbol, spread_pct)
        tg_send(msg)
        return

# -------- Web UI template --------
INDEX_HTML = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Live MEXC ↔ DEX ↔ BIN Monitor</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
    <style>body{font-family:system-ui, -apple-system, "Segoe UI", Roboto;} .muted{opacity:.6}</style>
  </head>
  <body class="bg-light">
    <div class="container py-3">
      <h4>📡 Live MEXC ↔ DEX ↔ BIN Monitor</h4>
      <div class="mb-2">
        <form id="addForm" class="row g-2">
          <div class="col-auto"><input id="symbol" class="form-control" placeholder="SYMBOL (e.g. PEPE/USDT)" autocomplete="off"></div>
          <div class="col-auto"><button class="btn btn-primary">Add</button></div>
          <div class="col-auto"><button id="clearBtn" class="btn btn-danger" type="button">Clear auto list</button></div>
        </form>
      </div>
      <div id="statusBadge" class="mb-2"></div>
      <div class="table-responsive">
        <table class="table table-sm table-bordered" id="liveTable">
          <thead class="table-light"><tr><th>Symbol</th><th>1h Δ%</th><th>DEX(USD)</th><th>MEXC(USD)</th><th>BIN(USD)</th><th>Δ% (MEXC vs DEX)</th></tr></thead>
          <tbody id="tbody"></tbody>
        </table>
      </div>
      <div class="small text-muted">Updated: <span id="updated">—</span></div>
    </div>
<script>
  const socket = io();
  const tbody = document.getElementById("tbody");
  const updatedEl = document.getElementById("updated");
  socket.on("live.update", (data) => {
    updatedEl.innerText = new Date(data.time*1000).toISOString().replace('T',' ').substr(0,19) + " UTC";
    const rows = data.rows || [];
    tbody.innerHTML = "";
    rows.forEach(r=>{
      const tr = document.createElement("tr");
      // highlight spread >= threshold
      const spread = r.spread_pct;
      if (spread !== null && Math.abs(spread) >= data.spread_threshold) {
        tr.style.background = 'rgba(255,230,180,0.6)';
      }
      const dexStr = r.dex_price === null ? '—' : Number(r.dex_price).toFixed(8);
      const mexcStr = r.mexc_price === null ? '—' : Number(r.mexc_price).toFixed(8);
      const binStr = r.binance_price === null ? '—' : Number(r.binance_price).toFixed(8);
      const oneh = isNaN(r.oneh) ? '—' : (r.oneh>0?'+':'') + Number(r.oneh).toFixed(2) + '%';
      const spreadStr = r.spread_pct===null?'—':(r.spread_pct>0?'+':'') + Number(r.spread_pct).toFixed(2) + '%';
      tr.innerHTML = `<td><strong>${r.symbol}</strong></td><td>${oneh}</td><td>${dexStr}</td><td>${mexcStr}</td><td>${binStr}</td><td>${spreadStr}</td>`;
      tbody.appendChild(tr);
    });
  });
  socket.on("status", (txt) => { document.getElementById("statusBadge").innerHTML = '<span class="badge bg-info">'+txt+'</span>'; setTimeout(()=>document.getElementById("statusBadge").innerHTML="",3000); });

  document.getElementById("addForm").addEventListener("submit",(e)=>{ e.preventDefault(); const v=document.getElementById("symbol").value.trim(); if(!v) return; socket.emit("add_symbol", v); document.getElementById("symbol").value='';});
  document.getElementById("clearBtn").addEventListener("click", ()=>{ if(confirm("Clear user symbols?")) socket.emit("clear_symbols"); });
</script>
</body>
</html>
"""

# -------- Flask + SocketIO --------
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

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
            tg_send("🤖 Live monitor online.")
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
        elif cmd == "/list":
            tg_send("Monitored: " + (", ".join(state["symbols"]) if state["symbols"] else "—"))
        elif cmd == "/clear":
            state["symbols"] = []
            save_state()
            socketio.emit("status", "Cleared symbols")
            tg_send("🧹 Cleared all symbols")
        elif cmd == "/live":
            if len(parts) >= 2 and parts[1].lower() in ("on", "off"):
                state["live_to_telegram"] = (parts[1].lower() == "on")
                save_state()
                tg_send(f"Live->Telegram set to {state['live_to_telegram']}")
            else:
                tg_send("Usage: /live on|off")
        elif cmd == "/status":
            tg_send(f"Symbols: {', '.join(state['symbols']) if state['symbols'] else '—'}\nAlert threshold: {SPREAD_ALERT_PCT}%\nLive->Telegram: {state.get('live_to_telegram')}")
        else:
            tg_send("❓ Unknown command. /help")
    except Exception as e:
        logger.exception("webhook cmd error: %s", e)
        tg_send("⚠️ Error processing command.")
    return jsonify({"ok": True})

@socketio.on("connect")
def on_connect():
    emit("status", "connected")
    emit("live.update", {"rows": [], "time": time.time(), "spread_threshold": SPREAD_ALERT_PCT})

@socketio.on("add_symbol")
def on_add_symbol(sym):
    s = sym.strip().upper()
    if not s:
        return
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

# -------- Orchestrator / background tasks --------
class Orchestrator:
    def __init__(self):
        self.loop = None
        self.thread = None
        self.running = False
        self.last_markets_refresh = 0

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
        # initial refresh
        await refresh_mexc_markets(self.loop)
        await refresh_binance_markets(self.loop)
        self.last_markets_refresh = time.time()
        while True:
            try:
                # refresh market lists periodically
                if time.time() - self.last_markets_refresh > POLL_INTERVAL_MARKETS:
                    await refresh_mexc_markets(self.loop)
                    await refresh_binance_markets(self.loop)
                    self.last_markets_refresh = time.time()

                # build list of candidate symbols from MEXC markets (BASE token symbol)
                # We'll normalize to base token name (e.g. "PEPE/USDT" -> "PEPE/USDT")
                cand_pairs = list(cex_markets_mexc.keys())
                # add user-specified symbols too
                cand_pairs = list(dict.fromkeys(state.get("symbols", []) + cand_pairs))  # preserve order, unique

                rows: List[Dict[str, Any]] = []

                # We'll iterate candidate pairs and compute:
                #  - CEX price (MEXC)
                #  - DEX price (dexscreener/GMGN) for base token (strip /USDT)
                #  - BIN price (if available)
                #  - 1h change (from MEXC via ohlcv)
                # Keep lightweight by sampling up to e.g. 1000 pairs per cycle (but cand_pairs usually ~couple hundred)
                # We'll run blocking ccxt calls in executor to not block event loop.
                max_check = min(len(cand_pairs), 1200)
                check_pairs = cand_pairs[:max_check]

                tasks = []
                for pair in check_pairs:
                    tasks.append(self.loop.run_in_executor(None, self._process_pair_sync, pair))
                # gather with timeout
                results = await asyncio.gather(*tasks, return_exceptions=True)

                # filter valid results
                for res in results:
                    if isinstance(res, Exception) or res is None:
                        continue
                    # res = (symbol, oneh_pct, dex_price, mexc_price, bin_price, spread_pct)
                    symbol, oneh_pct, dex_price, mexc_price, bin_price, spread_pct = res
                    # only keep if sensible prices & spread reasonable
                    if dex_price is None or mexc_price is None:
                        continue
                    if not is_reasonable_spread(dex_price, mexc_price):
                        continue
                    # guard insane spread numbers
                    if abs(spread_pct) > 2000:
                        continue
                    rows.append({
                        "symbol": symbol,
                        "oneh": oneh_pct,
                        "dex_price": dex_price,
                        "mexc_price": mexc_price,
                        "binance_price": bin_price,
                        "spread_pct": spread_pct
                    })
                    # keep caches
                    oneh_changes[symbol] = oneh_pct if oneh_pct is not None else 0.0
                    dex_prices[symbol] = dex_price
                    cex_prices.setdefault(symbol, {})["mexc"] = mexc_price
                    if bin_price is not None:
                        cex_prices.setdefault(symbol, {})["binance"] = bin_price

                    # maybe alert
                    try:
                        maybe_alert(symbol, dex_price, mexc_price, spread_pct)
                    except Exception:
                        pass

                # sort by absolute 1h% desc and pick top N
                rows.sort(key=lambda r: abs(r["oneh"] or 0.0), reverse=True)
                top = rows[:MAX_TOP]

                # emit to clients
                socketio.emit("live.update", {"rows": top, "time": time.time(), "spread_threshold": SPREAD_ALERT_PCT})

                # optionally update Telegram live panel
                if state.get("live_to_telegram") and state.get("chat_id"):
                    try:
                        txt = self.build_telegram_table(top)
                        if not state.get("msg_id"):
                            res = tg_send(txt)
                            if res and isinstance(res, dict):
                                mid = res.get("result", {}).get("message_id")
                                if mid:
                                    state["msg_id"] = int(mid); save_state()
                        else:
                            tg_edit(state["msg_id"], txt)
                    except Exception as e:
                        logger.debug("tg edit err: %s", e)

            except Exception as e:
                logger.exception("Main loop error: %s", e)

            await asyncio.sleep(LIVE_BROADCAST_INTERVAL)

    def _process_pair_sync(self, pair: str) -> Optional[Tuple[str, Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]]:
        """
        Blocking sync worker executed in threadpool.
        Returns tuple: (pair, 1h%, dex_price, mexc_price, bin_price, spread_pct)
        """
        try:
            # prefer raw pair as is
            mexc_price = fetch_ticker_price_sync("mexc", pair)
            if mexc_price is None:
                return None
            # base token (strip /USDT)
            base = pair.split("/")[0] if "/" in pair else pair.replace("USDT","")
            dex_price = fetch_price_from_dex(base)
            # binance
            bin_price = None
            try:
                bin_price = fetch_ticker_price_sync("binance", pair)
            except Exception:
                bin_price = None
            # compute 1h change using mexc (prefer), fallback to binance
            oneh_pct = fetch_1h_change_sync("mexc", pair)
            if oneh_pct is None:
                oneh_pct = fetch_1h_change_sync("binance", pair)
            # spread: (mexc - dex)/dex*100
            spread_pct = None
            if dex_price and mexc_price:
                spread_pct = (mexc_price - dex_price) / dex_price * 100.0
            return (pair, oneh_pct, dex_price, mexc_price, bin_price, spread_pct)
        except Exception as e:
            logger.debug("pair process err %s: %s", pair, e)
            return None

    def build_telegram_table(self, rows: List[Dict[str, Any]]) -> str:
        lines = []
        lines.append("📡 *Live MEXC ↔ DEX Monitor*")
        lines.append(f"_Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}_\n")
        lines.append("`SYMBOL     1hΔ%    DEX(USD)     MEXC(USD)    Δ%`")
        lines.append("`-------------------------------------------------`")
        for r in rows:
            dex = r["dex_price"] or 0
            mexc = r["mexc_price"] or 0
            oneh = r["oneh"] or 0.0
            spread = r["spread_pct"] if r["spread_pct"] is not None else 0.0
            lines.append(f"`{r['symbol']:<10}` {oneh:+6.2f}%  {dex:>12.8f}  {mexc:>10.8f}  {spread:+7.2f}%")
        lines.append("\n`/status  /live on|off  /alert X`")
        return "\n".join(lines)

    async def _shutdown(self):
        pass

orchestrator = Orchestrator()

# -------- BOOT --------
if __name__ == "__main__":
    logger.info("Starting Live MEXC<->DEX Monitor")
    load_state()
    # set webhook if provided
    if TELEGRAM_TOKEN and WEBHOOK_URL:
        try:
            url = WEBHOOK_URL.rstrip("/") + "/webhook"
            r = requests.get(f"{TELEGRAM_API}/setWebhook?url={url}", timeout=10)
            logger.info("Set webhook result: %s", r.text[:200])
        except Exception as e:
            logger.warning("Failed to set webhook: %s", e)

    # start background orchestrator
    orchestrator.start()

    # run Flask-SocketIO server (eventlet recommended)
    # WARNING: in production use a proper WSGI server; for Render dev use allow_unsafe_werkzeug=True if needed
    try:
        socketio.run(app, host="0.0.0.0", port=PORT)
    except RuntimeError as e:
        # some environments (Render) prevent Werkzeug as production; allow unsafe if needed:
        logger.warning("socketio.run raised: %s", e)
        socketio.run(app, host="0.0.0.0", port=PORT, allow_unsafe_werkzeug=True)