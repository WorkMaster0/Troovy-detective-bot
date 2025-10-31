#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Live DEX <-> CEX monitor with top-10 Δ%/1h

import os
import time
import json
import logging
import asyncio
import requests
from datetime import datetime
from threading import Thread
from typing import Dict, List, Any, Optional
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
POLL_INTERVAL_DEX = 3.0
LIVE_BROADCAST_INTERVAL = 5.0
ALERT_THRESHOLD_PCT = 2.0
CLOSE_THRESHOLD_PCT = 0.5
MAX_SYMBOLS = 80
CEX_PRIMARY = "mexc"

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
last_alert_time: Dict[str, float] = {}
active_spreads: Dict[str, Dict[str, Any]] = {}
dex_history: Dict[str, List[tuple[float, float]]] = {}

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
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

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
        q = symbol.upper()
        url = GMGN_API.format(q=q)
        r = requests.get(url, timeout=7)
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
        r = requests.get(url, timeout=7)
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

def pct_change_1h(symbol: str) -> Optional[float]:
    if symbol not in dex_prices or symbol not in cex_prices:
        return None
    history = dex_history.get(symbol, [])
    if not history:
        return None
    old_price = history[0][1]
    if old_price == 0:
        return None
    pct = (cex_prices[symbol] - old_price) / old_price * 100.0
    return pct

def build_live_table_text() -> str:
    syms = list(state.get("symbols", []))
    valid_syms = [s for s in syms if dex_prices.get(s) is not None and cex_prices.get(s) is not None]
    top_syms = sorted(valid_syms, key=lambda s: abs(pct_change_1h(s) or 0), reverse=True)[:10]

    if not top_syms:
        return "🟡 *No symbols monitored.* Use `/add SYMBOL` to add."

    lines = []
    lines.append("📡 *Live DEX ↔ CEX Monitor*")
    lines.append(f"_Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}_\n")
    lines.append("`SYMBOL    DEX(USD)      CEX(USD)     Δ% (1h)   Last`")
    lines.append("`-----------------------------------------------------------`")

    for s in top_syms:
        dex = dex_prices.get(s)
        cex = cex_prices.get(s)
        dex_str = f"{dex:.8f}" if dex is not None else "—"
        cex_str = f"{cex:.8f}" if cex is not None else "—"
        pct = pct_change_1h(s)
        pct_str = f"{pct:+6.2f}%" if pct is not None else "—"
        lu = last_update.get(s)
        lu_str = datetime.utcfromtimestamp(lu).strftime("%H:%M:%S") if lu else "—"
        lines.append(f"`{s:<7}` {dex_str:>12}  {cex_str:>12}  {pct_str:>9}  {lu_str}")

    lines.append("\n`/add SYMBOL  /remove SYMBOL  /list  /alert <pct>  /live on|off`")
    return "\n".join(lines)

# ---------------- DEX POLLER ----------------
class DexPoller:
    def __init__(self):
        self.running = False
        self.task = None

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

    async def _run(self):
        loop = asyncio.get_event_loop()
        while self.running:
            syms = list(state.get("symbols", []))[:MAX_SYMBOLS]
            if not syms:
                await asyncio.sleep(1)
                continue
            coros = [loop.run_in_executor(None, fetch_price_from_dex, s) for s in syms]
            results = await asyncio.gather(*coros, return_exceptions=True)
            for s, res in zip(syms, results):
                if isinstance(res, Exception) or res is None:
                    continue
                dex_prices[s] = float(res)
                last_update[s] = time.time()
                # зберігаємо історію
                if s not in dex_history:
                    dex_history[s] = []
                dex_history[s].append((time.time(), dex_prices[s]))
                one_hour_ago = time.time() - 3600
                dex_history[s] = [(t, p) for t, p in dex_history[s] if t >= one_hour_ago]
            await asyncio.sleep(POLL_INTERVAL_DEX)

# ---------------- CEX WATCHER ----------------
class CEXWatcher:
    def __init__(self, exchange_id=CEX_PRIMARY):
        self.exchange_id = exchange_id
        self.client = None
        self.task = None
        self.running = False

    async def start(self):
        if self.running:
            return
        try:
            self.client = getattr(ccxt, self.exchange_id)({"enableRateLimit": True})
        except Exception as e:
            logger.warning("CEX init failed: %s", e)
            self.client = None
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

    async def _run(self):
        while self.running:
            syms = list(state.get("symbols", []))[:MAX_SYMBOLS]
            if not syms or not self.client:
                await asyncio.sleep(1)
                continue
            try:
                all_tickers = self.client.fetch_tickers()
            except Exception:
                await asyncio.sleep(2)
                continue
            for s in syms:
                s_upper = s.upper()
                for pair, ticker in all_tickers.items():
                    if s_upper in pair:
                        last = ticker.get("last") or ticker.get("close") or ticker.get("price")
                        if last is not None:
                            cex_prices[s] = float(last)
                            break
            await asyncio.sleep(2)

# ---------------- FLASK + SOCKETIO ----------------
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Live DEX ↔ CEX Monitor</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
</head>
<body class="bg-light">
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
<div class="table-responsive">
<table class="table table-sm table-bordered" id="liveTable">
<thead class="table-light"><tr><th>Symbol</th><th>DEX (USD)</th><th>CEX (USD)</th><th>Δ% (1h)</th><th>Last</th></tr></thead>
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
socket.on("live.update", (data) => {
    const symbols = data.symbols || [];
    tbody.innerHTML = "";
    symbols.forEach(s => {
        const dex = data.dex_prices[s];
        const cex = data.cex_prices[s];
        let dexStr = dex==null?"—":Number(dex).toFixed(8);
        let cexStr = cex==null?"—":Number(cex).toFixed(8);
        let pct = "—";
        if(dex!=null && cex!=null && dex!==0){pct=((cex-dex)/dex*100).toFixed(2)+"%";}
        const lu = data.last_update && data.last_update[s] ? new Date(data.last_update[s]*1000).toISOString().substr(11,8) : "—";
        const tr = document.createElement("tr");
        const stale = data.last_update && data.last_update[s] && (Date.now()/1000 - data.last_update[s]>10);
        tr.innerHTML = `<td><strong>${s}</strong></td><td>${dexStr}</td><td>${cexStr}</td><td>${pct}</td><td>${lu}</td>`;
        if(stale) tr.style.opacity=0.6;
        tbody.appendChild(tr);
    });
});
socket.on("clients", (n)=>{clientsEl.innerText=n;});
socket.on("status",(txt)=>{statusBadge.innerHTML='<span class="badge bg-info">'+txt+'</span>'; setTimeout(()=>statusBadge.innerHTML="",3000);});
document.getElementById("addForm").addEventListener("submit",(e)=>{e.preventDefault();const sym=document.getElementById("symbol").value.trim().toUpperCase();if(!sym)return;socket.emit("add_symbol",sym);document.getElementById("symbol").value="";});
document.getElementById("clearBtn").addEventListener("click",()=>{if(!confirm("Clear all symbols?"))return;socket.emit("clear_symbols");});
</script>
</body>
</html>
"""

@app.route("/", methods=["GET"])
def index():
    return render_template_string(INDEX_HTML)

# ---------------- BROADCASTER ----------------
async def broadcaster():
    while True:
        try:
            if state.get("monitoring", True):
                for s in list(state.get("symbols", []))[:MAX_SYMBOLS]:
                    pass  # process_spread(s) можна додати

            socketio.emit("live.update", {
                "symbols": state.get("symbols", []),
                "dex_prices": dex_prices,
                "cex_prices": cex_prices,
                "last_update": last_update,
                "time": time.time(),
            })

            if state.get("live_to_telegram") and state.get("chat_id"):
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
            logger.exception("broadcaster error: %s", e)
        await asyncio.sleep(LIVE_BROADCAST_INTERVAL)

# ---------------- ORCHESTRATOR ----------------
class Orchestrator:
    def __init__(self):
        self.loop = None
        self.thread = None
        self.cex = CEXWatcher()
        self.dex = DexPoller()
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
        await self.cex.start()
        await self.dex.start()
        self.tasks.append(asyncio.create_task(broadcaster()))
        await asyncio.gather(*self.tasks)

# ---------------- BOOT ----------------
if __name__ == "__main__":
    orchestrator = Orchestrator()
    orchestrator.start()
    socketio.run(app, host="0.0.0.0", port=PORT)