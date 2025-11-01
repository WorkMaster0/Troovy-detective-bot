#!/usr/bin/env python3
"""
main.py - Live MEXC (CEX) <-> DEX monitor
- Scans all MEXC futures pairs (via ccxt REST), extracts short token symbols
- Polls DEX prices (GMGN, Dexscreener, optional Dextools)
- Auto-adds tokens to monitoring when spread >= AUTO_ADD_THRESHOLD_PCT (default 3%)
- Broadcasts live table via Flask+SocketIO, and optionally edits one Telegram message
- Alerts to Telegram when spread opens/closes (cooldown rules)
"""
import os
import time
import json
import logging
import asyncio
import requests
from datetime import datetime
from threading import Thread
from typing import Dict, Optional, Any, List, Set
from flask import Flask, request, render_template_string, jsonify
from flask_socketio import SocketIO, emit

# optional ccxt.pro; we use sync ccxt for discovery & REST to avoid pro dependency
try:
    import ccxt.pro as ccxtpro  # not required
except Exception:
    ccxtpro = None
import ccxt

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # e.g. https://your-app.onrender.com
PORT = int(os.getenv("PORT", "10000"))

STATE_FILE = "state.json"
POLL_INTERVAL_DEX = float(os.getenv("POLL_INTERVAL_DEX", "3.0"))       # seconds for dex poll
LIVE_BROADCAST_INTERVAL = float(os.getenv("LIVE_BROADCAST_INTERVAL", "5.0"))  # 5s as requested
MEXC_SYMBOL_REFRESH = int(os.getenv("MEXC_SYMBOL_REFRESH", "600"))    # refresh list every 10 minutes
SCAN_ALL_INTERVAL = int(os.getenv("SCAN_ALL_INTERVAL", "60"))         # how often to scan all MEXC for auto-add
AUTO_ADD_THRESHOLD_PCT = float(os.getenv("AUTO_ADD_THRESHOLD_PCT", "3.0"))  # 3% auto-add threshold
MIN_ABS_DIFF = float(os.getenv("MIN_ABS_DIFF", "0.000001"))           # minimal absolute price diff to consider
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "200.0"))          # filter absurd spreads >200%
ALERT_COOLDOWN = int(os.getenv("ALERT_COOLDOWN", "60"))              # 60s per-symbol cooldown unless spread increases
MAX_SYMBOL_NAME_LEN = int(os.getenv("MAX_SYMBOL_NAME_LEN", "10"))     # keep short names
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

CEX_PRIMARY = os.getenv("CEX_PRIMARY", "mexc")  # ccxt id

# DEX sources
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search/?q={q}"
GMGN_API = "https://gmgn.ai/defi/quotation/v1/tokens/search?keyword={q}"
DEXTOOLS_API = "https://www.dextools.io/shared/analytics/pair-search?query={q}"  # optional fallback

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("mexc-dex-monitor")

# ---------------- STATE ----------------
state: Dict[str, Any] = {
    "symbols": [],        # user-monitored symbols (strings)
    "chat_id": None,
    "msg_id": None,
    "monitoring": True,
    "live_to_telegram": False,
    "auto_add_enabled": True,
    "auto_add_threshold_pct": AUTO_ADD_THRESHOLD_PCT,
}
# runtime caches
dex_prices: Dict[str, float] = {}
cex_prices: Dict[str, float] = {}
last_update: Dict[str, float] = {}
last_alert_time: Dict[str, float] = {}
open_spreads: Dict[str, Dict[str, Any]] = {}  # active spreads with metadata

# MEXC symbols pool
mexc_symbols: Set[str] = set()
mexc_symbol_lock = asyncio.Lock() if hasattr(asyncio, 'Lock') else None

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

# ---------------- UTIL: pretty text table ----------------
def build_live_table_text(monitored: List[str]) -> str:
    if not monitored:
        return "🟡 *No symbols monitored.* Use `/add SYMBOL` to add."
    lines = []
    lines.append("📡 *Live MEXC ↔ DEX Monitor*")
    lines.append(f"_Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}_\n")
    lines.append("`SYMBOL    1hΔ%    DEX(USD)     MEXC(USD)    Δ%`")
    lines.append("`-------------------------------------------------`")
    for s in monitored:
        dex = dex_prices.get(s)
        cex = cex_prices.get(s)
        oneh = compute_1h_change_text(s)
        dex_str = f"{dex:.8f}" if dex is not None else "—"
        cex_str = f"{cex:.8f}" if cex is not None else "—"
        pct_str = "—"
        if dex is not None and cex is not None and dex != 0:
            pct = (cex - dex) / dex * 100.0
            pct_str = f"{pct:+.2f}%"
        lines.append(f"`{s:<8}` {oneh:>6}  {dex_str:>12}  {cex_str:>12}  {pct_str:>7}")
    lines.append("\n`/add SYMBOL  /remove SYMBOL  /list  /alert <pct>  /live on|off`")
    return "\n".join(lines)

# ---------------- DEX fetchers ----------------
def fetch_from_gmgn(symbol: str) -> Optional[float]:
    try:
        q = symbol.upper()
        url = GMGN_API.format(q=q)
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
        q = symbol.upper()
        url = DEXSCREENER_SEARCH.format(q=q)
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

def fetch_from_dextools(symbol: str) -> Optional[float]:
    try:
        q = symbol.upper()
        url = DEXTOOLS_API.format(q=q)
        r = requests.get(url, timeout=6)
        r.raise_for_status()
        data = r.json()
        pairs = data.get("pairs") or []
        for p in pairs:
            price = p.get("priceUsd") or p.get("price")
            if price:
                return float(price)
    except Exception:
        pass
    return None

def fetch_price_from_dex(symbol: str) -> Optional[float]:
    # priority: GMGN -> Dexscreener -> Dextools
    val = fetch_from_gmgn(symbol)
    if val is not None:
        return val
    val = fetch_from_dexscreener(symbol)
    if val is not None:
        return val
    return fetch_from_dextools(symbol)

# ---------------- MEXC discovery & polling ----------------
def discover_mexc_futures_symbols() -> Set[str]:
    """
    Use ccxt to load markets for mexc and return set of base symbols (short names),
    only include contract/future markets quoted in USDT or USD and short base ticker.
    """
    out = set()
    try:
        ex = getattr(ccxt, CEX_PRIMARY)({"enableRateLimit": True})
        ex.load_markets(True)
        for m in ex.markets.values():
            try:
                # filter futures/contract
                is_contract = bool(m.get("contract") or m.get("future") or m.get("type") == "future" or m.get("info", {}).get("contractType"))
                quote = (m.get("quote") or "").upper()
                symbol = (m.get("base") or m.get("symbol") or "").upper()
                if (is_contract or "PERP" in (m.get("symbol") or "")) and (quote in ("USDT", "USD", "USDC")):
                    # only short base names (sane)
                    if symbol and 1 <= len(symbol) <= MAX_SYMBOL_NAME_LEN and symbol.isalnum():
                        out.add(symbol)
            except Exception:
                continue
    except Exception as e:
        logger.exception("discover_mexc_futures_symbols error: %s", e)
    return out

async def refresh_mexc_symbols_periodically():
    global mexc_symbols
    while True:
        try:
            new = discover_mexc_futures_symbols()
            if new:
                mexc_symbols = new
                logger.info("MEXC symbols refreshed: %d", len(mexc_symbols))
        except Exception as e:
            logger.debug("refresh mexc symbols err: %s", e)
        await asyncio.sleep(MEXC_SYMBOL_REFRESH)

# Fetch current MEXC prices for a list of symbols (via ccxt.fetch_tickers)
def fetch_mexc_prices_for(symbols: List[str]):
    """Synchronous fetch via ccxt (called in threadpool). Returns dict symbol->price"""
    res = {}
    try:
        ex = getattr(ccxt, CEX_PRIMARY)({"enableRateLimit": True})
        # attempt to fetch all tickers (may be large) — then map
        tickers = ex.fetch_tickers()
        for pair, t in tickers.items():
            # try to extract base symbol from pair
            try:
                # pair formats vary: "PEPE/USDT", "PEPEUSDT", "PEPE/USDT:USDT"
                base = pair.split("/")[0].upper() if "/" in pair else ''.join(filter(str.isalpha, pair.split(":")[0].upper()))
                if base in symbols:
                    last = t.get("last") or t.get("close") or t.get("price")
                    if last is not None:
                        res[base] = float(last)
            except Exception:
                continue
    except Exception as e:
        logger.exception("fetch_mexc_prices_for error: %s", e)
    return res

# ---------------- 1h change calculation ----------------
def compute_1h_change_text(symbol: str) -> str:
    # Try to get 1h change from CEX tickers info if available; fallback: return "—"
    try:
        ex = getattr(ccxt, CEX_PRIMARY)({"enableRateLimit": True})
        # look for pairs that contain symbol
        tickers = ex.fetch_tickers()
        for pair, t in tickers.items():
            if symbol in pair:
                # some tickers may have info with percent fields
                pct = t.get("percentage")
                if pct is not None:
                    return f"{pct:+.2f}%"
                # try info fields
                info = t.get("info", {}) or {}
                cand = info.get("priceChangePercent") or info.get("priceChangePercent1h") or info.get("percentChange")
                if cand:
                    return f"{float(cand):+.2f}%"
                # fallback: calculate using OHLCV last 2 1h candles (heavy) - try once
                try:
                    ohlcv = ex.fetch_ohlcv(pair, timeframe="1h", limit=2)
                    if len(ohlcv) >= 2:
                        open1 = ohlcv[0][1]  # open of previous hour
                        close2 = ohlcv[-1][4]  # last close
                        if open1 and close2:
                            pctcalc = (close2 - open1) / open1 * 100.0
                            return f"{pctcalc:+.2f}%"
                except Exception:
                    pass
        return "—"
    except Exception:
        return "—"

# ---------------- AUTO-SCAN & AUTO-ADD logic ----------------
async def scan_all_and_auto_add_loop():
    """
    Periodically scans through all known MEXC symbols, queries DEX price,
    computes spread vs MEXC CEX price; if spread >= threshold and passes sanity filters,
    auto-adds symbol to monitoring and sends Telegram alert (subject to cooldown).
    """
    loop = asyncio.get_event_loop()
    while True:
        try:
            syms = list(mexc_symbols)
            if not syms:
                await asyncio.sleep(SCAN_ALL_INTERVAL)
                continue
            # fetch cex prices (run in threadpool)
            cex_map = await loop.run_in_executor(None, fetch_mexc_prices_for, syms)
            now = time.time()
            for s in syms:
                try:
                    cex = cex_map.get(s)
                    if cex is None:
                        continue
                    # fetch DEX price (sync function in threadpool)
                    dex = await loop.run_in_executor(None, fetch_price_from_dex, s)
                    if dex is None:
                        continue
                    # sanity filters
                    absdiff = abs(cex - dex)
                    if absdiff < MIN_ABS_DIFF:
                        continue
                    if dex == 0:
                        continue
                    pct = (cex - dex) / dex * 100.0
                    if pct < 0:
                        # we care about CEX > DEX (arbitrage) — but still could monitor negative spreads if desired
                        continue
                    if pct < state.get("auto_add_threshold_pct", AUTO_ADD_THRESHOLD_PCT):
                        continue
                    if pct > MAX_SPREAD_PCT:
                        # skip absurd spikes
                        continue
                    # passed — decide to auto-add & alert
                    if s not in state["symbols"]:
                        state["symbols"].append(s)
                        save_state()
                        socketio.emit("status", f"Auto-added {s} ({pct:+.2f}%)", broadcast=True)
                    # handle open alert once per symbol with cooldown / expansion rule
                    last = last_alert_time.get(s, 0)
                    prev = open_spreads.get(s, {}).get("opened_pct", 0.0)
                    should_alert = False
                    if now - last >= ALERT_COOLDOWN:
                        should_alert = True
                    elif pct > prev * 1.05:  # if spread increased >5% relative to previously opened pct
                        should_alert = True
                    if should_alert:
                        # open spread alert
                        open_spreads[s] = {"opened_pct": pct, "open_ts": now, "dex_price": dex, "cex_price": cex}
                        last_alert_time[s] = now
                        msg = (
                            "🔔 *Spread OPENED (auto)*\n"
                            f"Symbol: `{s}`\n"
                            f"DEX price: `{dex:.8f}`\n"
                            f"MEXC price: `{cex:.8f}`\n"
                            f"Spread: *{pct:.2f}%*\n"
                            f"Time: {datetime.utcfromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S UTC')}"
                        )
                        logger.info("Auto OPEN %s %.2f%%", s, pct)
                        tg_send(msg)
                    # update runtime caches
                    dex_prices[s] = dex
                    cex_prices[s] = cex
                    last_update[s] = now
                except Exception:
                    continue
        except Exception as e:
            logger.exception("scan_all_and_auto_add_loop error: %s", e)
        await asyncio.sleep(SCAN_ALL_INTERVAL)

# ---------------- DEX poller (monitored symbols) ----------------
async def dex_poll_loop():
    loop = asyncio.get_event_loop()
    while True:
        try:
            syms = list(state.get("symbols", []))[:200]
            if not syms:
                await asyncio.sleep(POLL_INTERVAL_DEX)
                continue
            coros = [loop.run_in_executor(None, fetch_price_from_dex, s) for s in syms]
            results = await asyncio.gather(*coros, return_exceptions=True)
            now = time.time()
            for s, res in zip(syms, results):
                if isinstance(res, Exception) or res is None:
                    continue
                try:
                    dex_prices[s] = float(res)
                    last_update[s] = now
                except Exception:
                    continue
        except Exception as e:
            logger.debug("dex_poll_loop error: %s", e)
        await asyncio.sleep(POLL_INTERVAL_DEX)

# ---------------- CEX poller (monitored symbols) ----------------
async def cex_poll_loop():
    loop = asyncio.get_event_loop()
    while True:
        try:
            syms = list(state.get("symbols", []))[:200]
            if not syms:
                await asyncio.sleep(2.0)
                continue
            # fetch many prices using threadpool
            cex_map = await loop.run_in_executor(None, fetch_mexc_prices_for, syms)
            now = time.time()
            for s in syms:
                if s in cex_map and cex_map[s] is not None:
                    cex_prices[s] = cex_map[s]
                    last_update[s] = now
        except Exception as e:
            logger.debug("cex_poll_loop error: %s", e)
        await asyncio.sleep(2.0)

# ---------------- SPREAD processing (close alerts) ----------------
def process_spreads_for_monitored():
    now = time.time()
    for s in list(state.get("symbols", [])):
        try:
            dex = dex_prices.get(s)
            cex = cex_prices.get(s)
            if dex is None or cex is None or dex == 0:
                continue
            pct = (cex - dex) / dex * 100.0
            if s in open_spreads:
                # close condition: spread dropped below threshold or reversed
                if pct <= max(1.0, state.get("close_threshold_pct", 0.5)):
                    opened = open_spreads.pop(s, None)
                    last_alert_time[s] = now
                    if opened:
                        msg = (
                            "✅ *Spread CLOSED*\n"
                            f"Symbol: `{s}`\n"
                            f"Now: DEX `{dex:.8f}` | MEXC `{cex:.8f}`\n"
                            f"Current spread: *{pct:.2f}%*\n"
                            f"Opened: *{opened.get('opened_pct'):.2f}%*\n"
                            f"Time: {datetime.utcfromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S UTC')}"
                        )
                        logger.info("CLOSE %s %.2f%%", s, pct)
                        tg_send(msg)
            else:
                # if not open, maybe open (but auto-add loop handles auto-opening)
                pass
        except Exception:
            continue

# ---------------- TOP 10 by 1h change (for display) ----------------
def compute_top10_1h_from_mexc() -> List[str]:
    """Return top 10 base symbols by 1h change from MEXC tickers (best-effort)."""
    try:
        ex = getattr(ccxt, CEX_PRIMARY)({"enableRateLimit": True})
        tickers = ex.fetch_tickers()
        scored = []
        for pair, t in tickers.items():
            try:
                # find base
                base = pair.split("/")[0].upper() if "/" in pair else ''.join(filter(str.isalpha, pair.split(":")[0].upper()))
                if not base or len(base) > MAX_SYMBOL_NAME_LEN:
                    continue
                # try to get pct 1h from info
                pct = t.get("percentage")
                if pct is None:
                    info = t.get("info", {}) or {}
                    pct = info.get("priceChangePercent") or info.get("priceChangePercent1h")
                    if pct is None:
                        continue
                    pct = float(pct)
                scored.append((base, float(pct)))
            except Exception:
                continue
        scored.sort(key=lambda x: abs(x[1]), reverse=True)
        top = []
        seen = set()
        for b, p in scored:
            if b in seen: continue
            top.append(b)
            seen.add(b)
            if len(top) >= 10: break
        return top
    except Exception:
        return []

# ---------------- FLASK + SOCKET.IO ----------------
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

INDEX_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Live MEXC ↔ DEX Monitor</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
<style>
  body{padding:18px;background:#f8f9fa}
  .pos-green{background:#e6ffed}
  .pos-red{background:#ffe6e6}
  .smallcode{font-family:monospace}
</style>
</head><body>
<div class="container">
  <h4>📡 Live MEXC ↔ DEX Monitor</h4>
  <div class="mb-2">
    <form id="addForm" class="row g-2">
      <div class="col-auto"><input id="symbol" class="form-control" placeholder="SYMBOL (e.g. PEPE)" autocomplete="off"></div>
      <div class="col-auto"><button class="btn btn-primary">Add</button></div>
      <div class="col-auto"><button id="clearBtn" class="btn btn-danger" type="button">Clear All</button></div>
    </form>
  </div>
  <div id="statusBadge" class="mb-2"></div>

  <div class="row">
    <div class="col-md-8">
      <div class="table-responsive">
        <table class="table table-sm table-bordered" id="liveTable">
          <thead class="table-light"><tr><th>Symbol</th><th>1hΔ%</th><th>DEX (USD)</th><th>MEXC (USD)</th><th>Δ%</th><th>Last</th></tr></thead>
          <tbody id="tbody"></tbody>
        </table>
      </div>
    </div>
    <div class="col-md-4">
      <h6>Top movers (1h)</h6>
      <ul id="top10" class="list-group"></ul>
    </div>
  </div>

  <div class="small text-muted mt-2">Connected clients: <span id="clients">0</span></div>
</div>

<script>
  const socket = io();
  const tbody = document.getElementById("tbody");
  const clientsEl = document.getElementById("clients");
  const statusBadge = document.getElementById("statusBadge");
  const top10El = document.getElementById("top10");

  socket.on("connect", () => console.log("connected"));
  socket.on("live.update", (data) => {
    const symbols = data.symbols || [];
    tbody.innerHTML = "";
    for (const s of symbols) {
      const dex = data.dex_prices && data.dex_prices[s] != null ? Number(data.dex_prices[s]) : null;
      const cex = data.cex_prices && data.cex_prices[s] != null ? Number(data.cex_prices[s]) : null;
      let dexStr = dex == null ? "—" : dex.toFixed(8);
      let cexStr = cex == null ? "—" : cex.toFixed(8);
      let pct = "—";
      if (dex != null && cex != null && dex !== 0) {
        pct = ((cex - dex)/dex*100).toFixed(2) + "%";
      }
      const oneh = data.oneh && data.oneh[s] ? data.oneh[s] : "—";
      const lu = data.last_update && data.last_update[s] ? new Date(data.last_update[s]*1000).toISOString().substr(11,8) : "—";
      const tr = document.createElement("tr");
      tr.innerHTML = `<td><strong>${s}</strong></td><td>${oneh}</td><td>${dexStr}</td><td>${cexStr}</td><td>${pct}</td><td class="smallcode">${lu}</td>`;
      // color highlight
      if (pct !== "—") {
        const num = parseFloat(pct.replace("%",""));
        if (num >= 3) tr.classList.add("pos-green");
        else if (num < 0) tr.classList.add("pos-red");
      }
      tbody.appendChild(tr);
    }
    // top10
    top10El.innerHTML = "";
    const top = data.top10 || [];
    top.forEach(t => {
      const li = document.createElement("li");
      li.className = "list-group-item";
      li.textContent = t;
      top10El.appendChild(li);
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
                socketio.emit("status", f"Added {sym}", broadcast=True)
                tg_send(f"✅ Added {sym}")
            else:
                tg_send(f"⚠️ {sym} already monitored")
        elif cmd == "/remove" and len(parts) >= 2:
            sym = parts[1].upper()
            if sym in state["symbols"]:
                state["symbols"].remove(sym)
                save_state()
                socketio.emit("status", f"Removed {sym}", broadcast=True)
                tg_send(f"🗑 Removed {sym}")
            else:
                tg_send(f"⚠️ {sym} not monitored")
        elif cmd == "/list":
            tg_send("Monitored: " + (", ".join(state["symbols"]) if state["symbols"] else "—"))
        elif cmd == "/clear":
            state["symbols"] = []
            save_state()
            socketio.emit("status", "Cleared symbols", broadcast=True)
            tg_send("🧹 Cleared all symbols")
        elif cmd == "/alert":
            if len(parts) >= 2:
                try:
                    pct = float(parts[1])
                    state["auto_add_threshold_pct"] = pct
                    save_state()
                    tg_send(f"✅ Auto-add threshold set to {pct:.2f}%")
                except Exception:
                    tg_send("Usage: /alert <pct>")
            else:
                tg_send(f"Current auto-add threshold: {state.get('auto_add_threshold_pct'):.2f}%")
        elif cmd == "/live":
            if len(parts) >= 2 and parts[1].lower() in ("on","off"):
                state["live_to_telegram"] = (parts[1].lower()=="on")
                save_state()
                tg_send(f"Live-to-Telegram set to {state['live_to_telegram']}")
            else:
                tg_send("Usage: /live on|off")
        elif cmd == "/status":
            syms = state.get("symbols", [])
            lines = [f"Symbols: {', '.join(syms) if syms else '—'}",
                     f"Auto-add threshold: {state.get('auto_add_threshold_pct'):.2f}%",
                     f"Auto-add enabled: {state.get('auto_add_enabled')}",
                     f"Active spreads: {len(open_spreads)}"]
            tg_send("\n".join(lines))
        else:
            tg_send("❓ Unknown command. /help")
    except Exception as e:
        logger.exception("webhook cmd error: %s", e)
        tg_send("⚠️ Error processing command.")
    return jsonify({"ok": True})

@socketio.on("connect")
def on_connect():
    try:
        participants = 1
        emit("clients", participants)
        emit("live.update", {
            "symbols": state.get("symbols", []),
            "dex_prices": dex_prices,
            "cex_prices": cex_prices,
            "last_update": last_update,
            "time": time.time(),
            "top10": compute_top10_1h_from_mexc(),
            "oneh": {s: compute_1h_change_text(s) for s in state.get("symbols", [])}
        })
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
            logger.exception("orchestrator loop error: %s", e)

    async def _main(self):
        load_state()
        # start periodic tasks
        tasks = []
        tasks.append(asyncio.create_task(refresh_mexc_symbols_periodically()))
        tasks.append(asyncio.create_task(scan_all_and_auto_add_loop()))
        tasks.append(asyncio.create_task(dex_poll_loop()))
        tasks.append(asyncio.create_task(cex_poll_loop()))
        # broadcaster: emit live updates and optionally edit Telegram
        async def broadcaster():
            while True:
                try:
                    # process spreads close conditions
                    process_spreads_for_monitored()
                    # build and emit live update
                    top10 = compute_top10_1h_from_mexc()
                    oneh_map = {s: compute_1h_change_text(s) for s in state.get("symbols", [])}
                    socketio.emit("live.update", {
                        "symbols": state.get("symbols", []),
                        "dex_prices": dex_prices,
                        "cex_prices": cex_prices,
                        "last_update": last_update,
                        "time": time.time(),
                        "top10": top10,
                        "oneh": oneh_map
                    })
                    # optionally edit Telegram live panel
                    if state.get("live_to_telegram") and state.get("chat_id"):
                        try:
                            txt = build_live_table_text(state.get("symbols", []))
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
                            logger.debug("tg edit err: %s", e)
                except Exception:
                    logger.exception("broadcaster error")
                await asyncio.sleep(LIVE_BROADCAST_INTERVAL)
        tasks.append(asyncio.create_task(broadcaster()))
        self.tasks = tasks
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("orchestrator cancelled")
        finally:
            for t in tasks:
                try:
                    t.cancel()
                except Exception:
                    pass

    def stop(self):
        if not self.running:
            return
        async def _stop_all():
            for t in list(asyncio.all_tasks(loop=self.loop)):
                try:
                    t.cancel()
                except Exception:
                    pass
        fut = asyncio.run_coroutine_threadsafe(_stop_all(), self.loop)
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
    logger.info("🚀 Starting Live MEXC ↔ DEX monitor")
    load_state()
    # set Telegram webhook if provided
    if TELEGRAM_TOKEN and WEBHOOK_URL:
        try:
            url = WEBHOOK_URL.rstrip("/") + "/webhook"
            r = requests.get(f"{TELEGRAM_API}/setWebhook?url={url}", timeout=10)
            logger.info("Set webhook result: %s", r.text[:200])
        except Exception as e:
            logger.warning("Failed to set webhook: %s", e)

    orchestrator.start()
    # run Flask-SocketIO server (eventlet recommended)
    socketio.run(app, host="0.0.0.0", port=PORT)