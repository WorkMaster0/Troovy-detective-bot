#!/usr/bin/env python3
# main.py - Live DEX <-> CEX top-10-by-1h-change monitor
# Requirements (suggested):
#   pip install flask flask-socketio eventlet requests ccxt
# Optional:
#   pip install ccxt.pro    # if you have ccxt.pro available
#
# Usage:
#   TELEGRAM_TOKEN and WEBHOOK_URL optional (for webhook commands)
#   Run: python main.py
#   Open http://HOST:PORT/ to see live table.

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
PORT = int(os.getenv("PORT", "10000"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")   # optional
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")         # optional, e.g. https://.../webhook
STATE_FILE = "state.json"

POLL_INTERVAL_DEX = float(os.getenv("POLL_INTERVAL_DEX", "6.0"))   # how often fetch DEX pairs/prices
LIVE_BROADCAST_INTERVAL = float(os.getenv("LIVE_BROADCAST_INTERVAL", "2.0"))
TOP_N = int(os.getenv("TOP_N", "10"))  # top N to show (user asked top 10)
HISTORY_RETENTION_SECONDS = 60 * 60 * 3  # keep 3 hours history to be safe
HISTORY_SAMPLE_INTERVAL = float(os.getenv("HISTORY_SAMPLE_INTERVAL", "6.0"))  # how often we snapshot price
DEX_TIMEOUT = 8.0

# Dex APIs
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search/?q={q}"
DEXSCREENER_PAIRS = "https://api.dexscreener.com/latest/dex/pairs"  # best-effort (may return many)
GMGN_API = "https://gmgn.ai/defi/quotation/v1/tokens/search?keyword={q}"
DECTOOLS_API = "https://www.dextools.io/shared/analytics/pair-search?query={q}"

# CEX (REST) default exchange id
CEX_PRIMARY = os.getenv("CEX_PRIMARY", "mexc")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("top10-monitor")

# ---------------- STATE ----------------
state: Dict[str, Any] = {
    "symbols_watchlist": [],   # optional tokens user added
    "scan_all": True,          # if True, scanner will attempt to discover many pairs
    "alert_threshold_pct": 2.0,
    "chat_id": None,
}
# runtime structures
dex_prices: Dict[str, float] = {}   # key: token symbol -> best dex price (USD)
cex_prices: Dict[str, float] = {}   # key: token symbol -> cex price (USD)
price_history: Dict[str, deque] = defaultdict(lambda: deque())  # symbol -> deque of (ts, price)
last_update: Dict[str, float] = {}  # last time price was updated (for UI)
active_top: List[Tuple[str, float]] = []  # cached top list (symbol, pct_change)

# ---------------- SAVE / LOAD ----------------
def load_state():
    global state
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                state.update(data)
                logger.info("Loaded state: symbols_watchlist=%d", len(state.get("symbols_watchlist", [])))
    except Exception as e:
        logger.debug("load_state err: %s", e)

def save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.debug("save_state err: %s", e)

# ---------------- TELEGRAM ----------------
def tg_send(text: str) -> Optional[dict]:
    if not TELEGRAM_TOKEN or not state.get("chat_id"):
        logger.debug("tg_send: missing token/chat_id")
        return None
    try:
        payload = {"chat_id": state["chat_id"], "text": text, "parse_mode": "Markdown"}
        r = requests.post(TELEGRAM_API + "/sendMessage", json=payload, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug("tg_send error: %s", e)
        return None

# ---------------- DEX FETCH HELPERS ----------------
def fetch_from_gmgn(symbol: str) -> Optional[float]:
    try:
        url = GMGN_API.format(q=symbol)
        r = requests.get(url, timeout=DEX_TIMEOUT)
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

def fetch_from_dextools(symbol: str) -> Optional[float]:
    try:
        url = DECTOOLS_API.format(q=symbol)
        r = requests.get(url, timeout=DEX_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        pairs = data.get("pairs") or []
        for p in pairs:
            price = p.get("priceUsd") or p.get("price")
            if price:
                return float(price)
    except Exception as e:
        logger.debug("dextools err %s: %s", symbol, e)
    return None

def fetch_from_dexscreener(symbol: str) -> Optional[float]:
    try:
        url = DEXSCREENER_SEARCH.format(q=symbol)
        r = requests.get(url, timeout=DEX_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        pairs = data.get("pairs") or []
        if pairs:
            # choose first pair priceUsd available
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
    except Exception as e:
        logger.debug("dexscreener err %s: %s", symbol, e)
    return None

def fetch_best_dex_price(symbol: str) -> Optional[float]:
    # priority: GMGN -> DEXTools -> Dexscreener
    s = symbol.upper()
    res = fetch_from_gmgn(s)
    if res is not None:
        return res
    res = fetch_from_dextools(s)
    if res is not None:
        return res
    return fetch_from_dexscreener(s)

# ---------------- SCANNER: discover many tokens from Dexscreener pairs endpoint ----------------
def discover_tokens_from_dexscreener_top() -> List[str]:
    """
    Best-effort: call Dexscreener /latest/dex/pairs (no query) to get many pairs.
    Returns unique token symbols (base tokens).
    """
    try:
        r = requests.get(DEXSCREENER_PAIRS, timeout=12)
        if r.status_code != 200:
            logger.debug("dexscreener pairs returned %s", r.status_code)
            return []
        data = r.json()
        pairs = data.get("pairs") or []
        symbols = set()
        for p in pairs:
            base = p.get("baseToken", {}) or {}
            sym = base.get("symbol") or p.get("baseTokenSymbol")
            if sym:
                symbols.add(sym.upper())
        return sorted(symbols)
    except Exception as e:
        logger.debug("discover tokens err: %s", e)
        return []

# ---------------- PRICE HISTORY & TOP CALC ----------------
def record_price(symbol: str, price: float, source: str = "dex"):
    """
    Record price snapshot to history deque (symbol -> deque[(ts, price)]).
    """
    try:
        ts = time.time()
        dq = price_history[symbol]
        dq.append((ts, float(price)))
        # remove old items > retention
        cutoff = ts - HISTORY_RETENTION_SECONDS
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        last_update[symbol] = ts
        # update per-source price caches
        if source == "dex":
            dex_prices[symbol] = float(price)
        else:
            cex_prices[symbol] = float(price)
    except Exception as e:
        logger.debug("record_price err: %s", e)

def compute_pct_change_1h(symbol: str) -> Optional[float]:
    """
    Compute percent change between current/latest price and price ~1 hour ago.
    Returns signed percent: (now - then)/then * 100.
    """
    dq = price_history.get(symbol)
    if not dq or len(dq) < 2:
        return None
    now_ts = time.time()
    target_ts = now_ts - 3600.0
    # find nearest older snapshot to target_ts
    prev_price = None
    # iterate from left (oldest) forward to find the last price <= target_ts
    for ts, p in dq:
        if ts <= target_ts:
            prev_price = p
        else:
            break
    # if we didn't find older sample, use earliest if it's within some tolerance
    if prev_price is None:
        # earliest sample
        if dq:
            prev_price = dq[0][1]
        else:
            return None
    # current price is latest appended
    cur_price = dq[-1][1]
    if prev_price == 0:
        return None
    pct = (cur_price - prev_price) / prev_price * 100.0
    return pct

def top_n_by_1h_change(n: int = 10) -> List[Tuple[str, float]]:
    """
    Returns list of (symbol, pct_change) sorted by absolute change desc limited to n.
    Only symbols with a valid 1h change included.
    """
    results = []
    # consider symbols with history
    for sym, dq in price_history.items():
        if not dq:
            continue
        pct = compute_pct_change_1h(sym)
        if pct is None:
            continue
        results.append((sym, pct))
    results.sort(key=lambda x: abs(x[1]), reverse=True)
    return results[:n]

# ---------------- CEX WATCHER (REST fallback) ----------------
class CEXWatcher:
    def __init__(self, exchange_id: str = CEX_PRIMARY):
        self.exchange_id = exchange_id
        self.client = None
        self.task = None
        self.running = False

    async def start(self):
        if self.running:
            return
        try:
            # Use sync ccxt client inside async loop via threadpool
            self.client = getattr(ccxt, self.exchange_id)({"enableRateLimit": True})
            logger.info("CEXWatcher init: %s", self.exchange_id)
            self.running = True
            self.task = asyncio.create_task(self._run())
        except Exception as e:
            logger.exception("CEXWatcher init error: %s", e)
            self.client = None

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except Exception:
                pass
        self.task = None
        self.client = None

    async def _run(self):
        loop = asyncio.get_event_loop()
        while self.running:
            syms = list(price_history.keys())[:200]  # check up to some limit
            if not syms:
                await asyncio.sleep(1.0)
                continue
            try:
                # fetch tickers in threadpool to avoid blocking loop
                tickers = await loop.run_in_executor(None, lambda: self.client.fetch_tickers())
                # tickers: dict pair -> ticker
                # try to match by symbol substring
                for s in syms:
                    found = False
                    s_up = s.upper()
                    for pair, t in tickers.items():
                        if s_up in pair:
                            val = t.get("last") or t.get("close") or t.get("price")
                            if val is not None:
                                # record as CEX price (also store into history as 'cex' sample)
                                record_price(s, float(val), source="cex")
                                found = True
                                break
                    if not found:
                        # no hit for s - skip
                        pass
            except Exception as e:
                logger.debug("CEX fetch error: %s", e)
            await asyncio.sleep(3.0)

# ---------------- MAIN SCANNER & ORCHESTRATOR ----------------
class Scanner:
    """
    Scans DEX data and maintains price_history, dex_prices, etc.
    - If state['scan_all']==True: it will attempt to discover many tokens from Dexscreener top endpoint.
    - Also continuously polls best DEX price for symbols in scanning set.
    """
    def __init__(self):
        self.task = None
        self.running = False
        self.scan_symbols: List[str] = []  # tokens to scan
        self.lock = asyncio.Lock()

    async def start(self):
        if self.running:
            return
        self.running = True
        self.task = asyncio.create_task(self._run())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except Exception:
                pass
        self.task = None

    async def _run(self):
        loop = asyncio.get_event_loop()
        last_discover = 0
        DISCOVER_INTERVAL = 60 * 10  # refresh list every 10 minutes
        while self.running:
            try:
                # refresh scan list periodically
                now = time.time()
                if state.get("scan_all", True) and (now - last_discover > DISCOVER_INTERVAL or not self.scan_symbols):
                    # best-effort discovery
                    cand = await loop.run_in_executor(None, discover_tokens_from_dexscreener_top)
                    if cand:
                        # keep top ~500 tokens to avoid overload
                        self.scan_symbols = cand[:500]
                        logger.info("Scanner discovered %d tokens (using dexscreener)", len(self.scan_symbols))
                        last_discover = now

                # build list to poll: combine discovered + watchlist
                poll_list = []
                if self.scan_symbols:
                    poll_list.extend(self.scan_symbols[:200])  # limit per loop
                poll_list.extend([s.upper() for s in state.get("symbols_watchlist", [])])
                # dedupe
                poll_list = list(dict.fromkeys([p.upper() for p in poll_list]))[:400]

                # parallelized (threadpool) fetch for dex prices
                tasks = [loop.run_in_executor(None, fetch_best_dex_price, s) for s in poll_list]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for s, res in zip(poll_list, results):
                    if isinstance(res, Exception) or res is None:
                        continue
                    try:
                        price = float(res)
                        record_price(s, price, source="dex")
                    except Exception:
                        continue

                # after updating history, prune price_history dict to keep manageable size
                # (keep only tokens with recent updates)
                cutoff = time.time() - HISTORY_RETENTION_SECONDS
                keys = list(price_history.keys())
                for k in keys:
                    dq = price_history[k]
                    if not dq:
                        del price_history[k]
                        dex_prices.pop(k, None)
                        cex_prices.pop(k, None)
                        last_update.pop(k, None)
                        continue
                    # if last sample older than retention and not watched -> remove
                    if dq and dq[-1][0] < cutoff and k not in state.get("symbols_watchlist", []):
                        try:
                            del price_history[k]
                            dex_prices.pop(k, None)
                            cex_prices.pop(k, None)
                            last_update.pop(k, None)
                        except Exception:
                            pass

            except Exception as e:
                logger.exception("Scanner loop error: %s", e)
            await asyncio.sleep(POLL_INTERVAL_DEX)

# ---------------- FLASK + SOCKET.IO UI ----------------
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

INDEX_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Top-10 1h change</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet"></head>
<body class="bg-light">
<div class="container py-4">
  <h4>Top {{top_n}} tokens by 1h change</h4>
  <div class="mb-2">
    <form id="addForm" class="row g-2">
      <div class="col-auto"><input id="symbol" class="form-control" placeholder="SYMBOL (e.g. PEPE)" autocomplete="off"></div>
      <div class="col-auto"><button class="btn btn-primary">Add to watchlist</button></div>
      <div class="col-auto"><button id="clearBtn" class="btn btn-danger" type="button">Clear Watchlist</button></div>
    </form>
  </div>
  <div id="statusBadge" class="mb-2"></div>
  <table class="table table-sm table-bordered">
    <thead class="table-light"><tr><th>#</th><th>Symbol</th><th>DEX (USD)</th><th>CEX (USD)</th><th>Δ% (1h)</th><th>Last</th></tr></thead>
    <tbody id="tbody"></tbody>
  </table>
  <div class="small text-muted">Connected clients: <span id="clients">0</span></div>
  <div class="mt-2">Watchlist: <span id="watchlist"></span></div>
</div>
<script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
<script>
const socket = io();
const tbody = document.getElementById("tbody");
const clientsEl = document.getElementById("clients");
const statusBadge = document.getElementById("statusBadge");
const watchlistEl = document.getElementById("watchlist");

socket.on("connect", () => { console.log("connected"); });
socket.on("live.top", (data) => {
  tbody.innerHTML = "";
  const list = data.top || [];
  list.forEach((item, idx) => {
    const sym = item[0];
    const pct = item[1];
    const dex = data.dex_prices[sym] ?? null;
    const cex = data.cex_prices[sym] ?? null;
    const lu = data.last_update && data.last_update[sym] ? new Date(data.last_update[sym]*1000).toISOString().substr(11,8) : "—";
    const tr = document.createElement("tr");
    let pctStr = (pct===null||pct===undefined) ? "—" : (pct>0? ("+"+pct.toFixed(2)+"%") : (pct.toFixed(2)+"%"));
    tr.innerHTML = `<td>${idx+1}</td><td><strong>${sym}</strong></td><td>${dex==null?"—":Number(dex).toFixed(8)}</td><td>${cex==null?"—":Number(cex).toFixed(8)}</td><td>${pctStr}</td><td>${lu}</td>`;
    if (pct > 0) tr.style.color = "green";
    if (pct < 0) tr.style.color = "crimson";
    tbody.appendChild(tr);
  });
  // watchlist
  watchlistEl.innerText = (data.watchlist || []).join(", ") || "—";
});
socket.on("clients", n => clientsEl.innerText = n);
socket.on("status", txt => { statusBadge.innerHTML = '<span class="badge bg-info">'+txt+'</span>'; setTimeout(()=>statusBadge.innerHTML="",3000); });

document.getElementById("addForm").addEventListener("submit", (e)=> {
  e.preventDefault();
  const s = document.getElementById("symbol").value.trim().toUpperCase();
  if (!s) return;
  socket.emit("add_watch", s);
  document.getElementById("symbol").value = "";
});
document.getElementById("clearBtn").addEventListener("click", ()=>{
  if (!confirm("Clear watchlist?")) return;
  socket.emit("clear_watch");
});
</script>
</body></html>
"""

@app.route("/", methods=["GET"])
def index():
    return render_template_string(INDEX_HTML, top_n=TOP_N)

# webhook for telegram commands
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
        if cmd == "/help":
            tg_send("Commands:\n/top - show top10 now\n/list - watched tokens\n/add <SYM> - add to watchlist\n/remove <SYM>\n/alert <pct>\n/status")
        elif cmd == "/top":
            top = top_n_by_1h_change(TOP_N)
            lines = [f"{i+1}. {t[0]} {t[1]:+.2f}%" for i,t in enumerate(top)]
            tg_send("*Top %d by 1h change:*\n" % TOP_N + "\n".join(lines) if lines else "No data yet")
        elif cmd == "/add" and len(parts)>=2:
            s = parts[1].upper()
            if s not in state["symbols_watchlist"]:
                state["symbols_watchlist"].append(s)
                save_state()
                tg_send(f"Added {s} to watchlist")
                socketio.emit("status", f"Added {s}")
            else:
                tg_send(f"{s} already in watchlist")
        elif cmd == "/remove" and len(parts)>=2:
            s = parts[1].upper()
            if s in state["symbols_watchlist"]:
                state["symbols_watchlist"].remove(s)
                save_state()
                tg_send(f"Removed {s}")
                socketio.emit("status", f"Removed {s}")
            else:
                tg_send(f"{s} not in watchlist")
        elif cmd == "/list":
            tg_send("Watchlist: " + (", ".join(state.get("symbols_watchlist", [])) or "—"))
        elif cmd == "/alert" and len(parts)>=2:
            try:
                v = float(parts[1])
                state["alert_threshold_pct"] = v
                save_state()
                tg_send(f"Alert threshold set to {v:.2f}%")
            except Exception:
                tg_send("Usage: /alert <pct>")
        elif cmd == "/status":
            tg_send(f"TopN={TOP_N} | Watchlist={len(state.get('symbols_watchlist',[]))} | Scan all={state.get('scan_all')}")
        else:
            tg_send("Unknown command. Use /help")
    except Exception as e:
        logger.exception("webhook cmd err: %s", e)
    return jsonify({"ok": True})

# SocketIO handlers
@socketio.on("connect")
def on_connect():
    try:
        # participants best-effort
        count = 1
        try:
            count = len(socketio.server.manager.get_participants('/', '/'))
        except Exception:
            pass
        emit("clients", count)
        emit("live.top", {"top": active_top, "dex_prices": dex_prices, "cex_prices": cex_prices, "last_update": last_update, "watchlist": state.get("symbols_watchlist", [])})
    except Exception:
        pass

@socketio.on("add_watch")
def on_add_watch(sym):
    s = sym.strip().upper()
    if not s:
        return
    if s not in state["symbols_watchlist"]:
        state["symbols_watchlist"].append(s)
        save_state()
        emit("status", f"Added {s}", broadcast=True)
    else:
        emit("status", f"{s} already watched")

@socketio.on("clear_watch")
def on_clear_watch():
    state["symbols_watchlist"] = []
    save_state()
    emit("status", "Cleared watchlist", broadcast=True)

# ---------------- ORCHESTRATOR ----------------
class Orchestrator:
    def __init__(self):
        self.loop = None
        self.thread = None
        self.scanner = Scanner()
        self.cex = CEXWatcher(CEX_PRIMARY)
        self.tasks: List[asyncio.Task] = []
        self.running = False

    def start(self):
        if self.running:
            return
        self.loop = asyncio.new_event_loop()
        self.thread = Thread(target=self._run, daemon=True)
        self.thread.start()
        self.running = True

    def _run(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        except Exception as e:
            logger.exception("orchestrator exception: %s", e)

    async def _main(self):
        load_state()
        # start scanner & cex
        await self.scanner.start()
        # start cex watcher (REST) regardless of ccxt.pro presence to fill cex_prices
        await self.cex.start()

        # broadcaster
        async def broadcaster():
            global active_top
            while True:
                try:
                    # compute top N by 1h change (absolute)
                    top = top_n_by_1h_change(TOP_N)
                    active_top = top
                    # emit to socket clients
                    try:
                        socketio.emit("live.top", {"top": top, "dex_prices": dex_prices, "cex_prices": cex_prices, "last_update": last_update, "watchlist": state.get("symbols_watchlist", [])})
                    except Exception:
                        pass
                    # alerts: optional open/close behaviour (simple: alert when crossing threshold upwards)
                    for sym, pct in top:
                        # if change >= alert threshold -> send one alert for that symbol
                        if abs(pct) >= float(state.get("alert_threshold_pct", 2.0)):
                            # only one alert per symbol per 10 minutes
                            now = time.time()
                            key = f"alert_{sym}"
                            last = last_update.get(key, 0)
                            if now - last > 60 * 10:
                                last_update[key] = now
                                try:
                                    tg_send(f"🔔 *Top alert* {sym}: {pct:+.2f}% (1h)")
                                except Exception:
                                    pass
                    # wait
                except Exception as e:
                    logger.exception("broadcaster loop err: %s", e)
                await asyncio.sleep(LIVE_BROADCAST_INTERVAL)

        btask = asyncio.create_task(broadcaster())
        self.tasks.append(btask)
        try:
            await asyncio.gather(*self.tasks)
        except asyncio.CancelledError:
            logger.info("orchestrator cancelled")
        finally:
            await self._shutdown()

    async def _shutdown(self):
        await self.scanner.stop()
        await self.cex.stop()

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
    logger.info("Starting Top-10 1h-change monitor")
    load_state()
    # configure Telegram webhook if provided
    if TELEGRAM_TOKEN and WEBHOOK_URL:
        try:
            url = WEBHOOK_URL.rstrip("/") + "/webhook"
            r = requests.get(f"{TELEGRAM_API}/setWebhook?url={url}", timeout=8)
            logger.info("Set webhook result: %s", r.text[:200])
        except Exception as e:
            logger.warning("Failed setWebhook: %s", e)

    orchestrator.start()
    # run flask socketio (eventlet recommended)
    socketio.run(app, host="0.0.0.0", port=PORT)