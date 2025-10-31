#!/usr/bin/env python3
# main.py - Live DEX <-> CEX monitor with Flask + SocketIO + Telegram webhook
# Features:
#  - DEX prices from Dexscreener + GMGN (public REST)
#  - optional CEX live via ccxt.pro (MEXC by default)
#  - Flask web UI + SocketIO live table
#  - Telegram webhook commands: /add, /remove, /list, /clear, /alert <pct>, /status, /live on|off, /help
#  - Spread open/close alerts: open when >= alert_threshold, close when <= close_threshold
#  - persistent state in state.json
import os
import time
import json
import logging
import asyncio
import requests
from datetime import datetime, timezone
from threading import Thread
from typing import Dict, Optional, Set, List, Any
from flask import Flask, request, render_template_string, jsonify
from flask_socketio import SocketIO, emit

# optional ccxt.pro for CEX websocket; if not present, cex watcher is disabled
try:
    import ccxt.pro as ccxtpro
except Exception:
    ccxtpro = None
import ccxt  # sync discovery/fallback

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # e.g. https://your-app.onrender.com
PORT = int(os.getenv("PORT", "10000"))

STATE_FILE = "state.json"
POLL_INTERVAL_DEX = float(os.getenv("POLL_INTERVAL_DEX", "3.0"))       # seconds
LIVE_BROADCAST_INTERVAL = float(os.getenv("LIVE_BROADCAST_INTERVAL", "2.0"))
ALERT_THRESHOLD_PCT = float(os.getenv("ALERT_THRESHOLD_PCT", "2.0"))  # default open alert threshold %
CLOSE_THRESHOLD_PCT = float(os.getenv("CLOSE_THRESHOLD_PCT", "0.5"))  # close when spread drops below this %
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "80"))
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# default exchanges (ccxt ids) - mexc is used as CEX watcher if ccxt.pro available
CEX_PRIMARY = os.getenv("CEX_PRIMARY", "mexc")

# DEX APIs
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search/?q={q}"
GMGN_API = "https://gmgn.ai/defi/quotation/v1/tokens/search?keyword={q}"

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("live-monitor")

# ---------------- STATE ----------------
state: Dict[str, Any] = {
    "symbols": [],        # list of str tokens e.g. ["PEPE","DOGE"]
    "chat_id": None,      # telegram chat id
    "msg_id": None,       # telegram live message id
    "monitoring": True,   # whether broadcast & checks run
    "alert_threshold_pct": ALERT_THRESHOLD_PCT,  # dynamic via /alert
    "close_threshold_pct": CLOSE_THRESHOLD_PCT,
    "live_to_telegram": False,  # whether to edit Telegram live panel continuously
}
# runtime caches
dex_prices: Dict[str, float] = {}      # symbol -> latest DEX price
cex_prices: Dict[str, float] = {}      # symbol -> latest CEX price
last_update: Dict[str, float] = {}     # symbol -> timestamp of last update (either source)
last_alert_time: Dict[str, float] = {} # per-symbol alert cooldown for reopen
active_spreads: Dict[str, Dict[str, Any]] = {}  # symbol -> {opened_pct, open_ts, buy_side, sell_side}

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
        logger.debug("tg_send: token or chat_id missing")
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

def tg_delete(message_id: int):
    if not TELEGRAM_TOKEN or not state.get("chat_id"):
        return None
    try:
        r = requests.post(TELEGRAM_API + "/deleteMessage", json={"chat_id": state["chat_id"], "message_id": message_id}, timeout=8)
        return r.json()
    except Exception:
        return None

# ---------------- UTIL: pretty table (markdown) ----------------
def build_live_table_text() -> str:
    syms = list(state.get("symbols", []))
    if not syms:
        return "🟡 *No symbols monitored.* Use `/add SYMBOL` to add."
    lines = []
    lines.append("📡 *Live DEX ↔ CEX Monitor*")
    lines.append(f"_Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}_\n")
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

# ---------------- DEX fetchers ----------------
def fetch_from_gmgn(symbol: str) -> Optional[float]:
    try:
        q = symbol.upper()
        url = GMGN_API.format(q=q)
        r = requests.get(url, timeout=7)
        r.raise_for_status()
        data = r.json()
        items = data.get("data") or []
        if items:
            # pick first with price_usd
            for it in items:
                price = it.get("price_usd") or it.get("priceUsd") or it.get("price")
                if price:
                    return float(price)
    except Exception as e:
        logger.debug("gmgn fetch err %s: %s", symbol, e)
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
            # prefer pairs on high-liquidity chains; take first with priceUsd
            for p in pairs:
                price = p.get("priceUsd") or p.get("price")
                if price:
                    return float(price)
        # fallback tokens structure
        tokens = data.get("tokens") or []
        for t in tokens:
            for p in t.get("pairs", []):
                price = p.get("priceUsd") or p.get("price")
                if price:
                    return float(price)
    except Exception as e:
        logger.debug("dexscreener fetch err %s: %s", symbol, e)
    return None

# unified DEX fetch with priority GMGN -> Dexscreener
def fetch_price_from_dex(symbol: str) -> Optional[float]:
    res = fetch_from_gmgn(symbol)
    if res is not None:
        return res
    return fetch_from_dexscreener(symbol)

# ---------------- CONTRACT MAPPING ----------------
def generate_candidates(symbol: str) -> List[str]:
    s = symbol.upper()
    return [f"{s}/USDT", f"{s}USDT", f"{s}/USD", f"{s}/USDT:USDT", f"{s}/PERP"]  # common variants

# ---------------- CEX (MEXC Futures via REST API) ----------------
class CEXWatcher:
    def __init__(self, exchange_id="mexc"):
        self.exchange_id = exchange_id
        self.client = None
        self.task = None
        self.running = False

    async def start(self):
        if self.running:
            return
        try:
            # Використовуємо синхронний ccxt для REST
            self.client = getattr(ccxt, self.exchange_id)({"enableRateLimit": True})
        except Exception as e:
            logger.exception("Failed to init %s: %s", self.exchange_id, e)
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
        self.task = None
        self.client = None

    async def _run(self):
        """Періодичне опитування цін через REST"""
        logger.info("%s watcher started (REST mode)", self.exchange_id)
        while self.running:
            syms = list(state.get("symbols", []))[:MAX_SYMBOLS]
            if not syms or not self.client:
                await asyncio.sleep(1.0)
                continue
            try:
                all_tickers = self.client.fetch_tickers()
            except Exception as e:
                logger.warning("CEX fetch_tickers failed: %s", e)
                await asyncio.sleep(2.0)
                continue

            for s in syms:
                found = False
                s_upper = s.upper()
                for pair, ticker in all_tickers.items():
                    if s_upper in pair:
                        last = ticker.get("last") or ticker.get("close") or ticker.get("price")
                        if last is not None:
                            cex_prices[s] = float(last)
                            found = True
                            break
                if not found:
                    logger.debug("No CEX price found for %s", s)
            await asyncio.sleep(2.0)

# ---------------- DEX poller ----------------
class DexPoller:
    def __init__(self):
        self.task = None
        self.running = False

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
        logger.info("DEX poller started (GMGN + Dexscreener)")
        loop = asyncio.get_event_loop()
        while self.running:
            syms = list(state.get("symbols", []))[:MAX_SYMBOLS]
            if not syms:
                await asyncio.sleep(1.0)
                continue
            # run in threadpool to avoid blocking
            coros = [loop.run_in_executor(None, fetch_price_from_dex, s) for s in syms]
            try:
                results = await asyncio.gather(*coros, return_exceptions=True)
                for s, res in zip(syms, results):
                    if isinstance(res, Exception) or res is None:
                        continue
                    try:
                        dex_prices[s] = float(res)
                        last_update[s] = time.time()
                    except Exception:
                        continue
            except Exception as e:
                logger.debug("dex gather error: %s", e)
            await asyncio.sleep(POLL_INTERVAL_DEX)

# ---------------- SPREAD logic (open / close alerts) ----------------
def process_spread(sym: str):
    dex = dex_prices.get(sym)
    cex = cex_prices.get(sym)
    if dex is None or cex is None or dex == 0:
        return
    pct = (cex - dex) / dex * 100.0
    now = time.time()
    open_thresh = float(state.get("alert_threshold_pct", ALERT_THRESHOLD_PCT))
    close_thresh = float(state.get("close_threshold_pct", CLOSE_THRESHOLD_PCT))

    # if not active and exceed open threshold -> open alert
    if sym not in active_spreads and pct >= open_thresh:
        active_spreads[sym] = {"opened_pct": pct, "open_ts": now, "dex_price": dex, "cex_price": cex}
        last_alert_time[sym] = now
        msg = (
            "🔔 *Spread OPENED*\n"
            f"Symbol: `{sym}`\n"
            f"DEX price: `{dex:.8f}`\n"
            f"CEX price: `{cex:.8f}`\n"
            f"Spread: *{pct:.2f}%*\n"
            f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        logger.info("ALERT OPEN %s %.2f%%", sym, pct)
        tg_send(msg)
        return

    # if active and falls below close_thresh -> close alert
    if sym in active_spreads:
        # don't close immediately if just opened (give small grace)
        opened = active_spreads[sym]
        # consider close
        if pct <= close_thresh:
            duration = now - opened.get("open_ts", now)
            msg = (
                "✅ *Spread CLOSED*\n"
                f"Symbol: `{sym}`\n"
                f"Now: DEX `{dex:.8f}` | CEX `{cex:.8f}`\n"
                f"Current spread: *{pct:.2f}%*\n"
                f"Opened: *{opened.get('opened_pct'):.2f}%*, duration: {int(duration)}s\n"
                f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
            )
            logger.info("ALERT CLOSE %s %.2f%%", sym, pct)
            tg_send(msg)
            active_spreads.pop(sym, None)
            last_alert_time[sym] = now
            return

# ---------------- FLASK + SOCKET.IO ----------------
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

INDEX_HTML = """<!doctype html>..."""  # shorted below; we'll use same template as before for page content
# (For brevity, we reuse a minimal page similar to earlier. The real template is below.)

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
          <thead class="table-light"><tr><th>Symbol</th><th>DEX (USD)</th><th>CEX (USD)</th><th>Δ%</th><th>Last</th></tr></thead>
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
        const symbols = data.symbols || [];
        tbody.innerHTML = "";
        symbols.forEach(s => {
          const dex = data.dex_prices[s];
          const cex = data.cex_prices[s];
          let dexStr = dex == null ? "—" : Number(dex).toFixed(8);
          let cexStr = cex == null ? "—" : Number(cex).toFixed(8);
          let pct = "—";
          if (dex != null && cex != null && dex !== 0) {
            pct = ((cex - dex)/dex*100).toFixed(2) + "%";
          }
          const lu = data.last_update && data.last_update[s] ? new Date(data.last_update[s]*1000).toISOString().substr(11,8) : "—";
          const tr = document.createElement("tr");
          // highlight stale rows (>10s)
          const stale = data.last_update && data.last_update[s] && (Date.now()/1000 - data.last_update[s] > 10);
          tr.innerHTML = `<td><strong>${s}</strong></td><td>${dexStr}</td><td>${cexStr}</td><td>${pct}</td><td>${lu}</td>`;
          if (stale) tr.style.opacity = 0.6;
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

# ---------------- Telegram webhook endpoint ----------------
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

# ---------------- SocketIO handlers ----------------
@socketio.on("connect")
def on_connect():
    try:
        participants = 1
        if hasattr(socketio, "server") and getattr(socketio, "server") is not None:
            # number of connected clients - best-effort (may vary by async mode)
            try:
                participants = len(socketio.server.manager.get_participants('/', '/'))  # may fail on some modes
            except Exception:
                participants = 1
        emit("clients", participants)
        emit("live.update", {"symbols": state.get("symbols", []), "dex_prices": dex_prices, "cex_prices": cex_prices, "last_update": last_update, "time": time.time()})
    except Exception:
        pass

@socketio.on("add_symbol")
def on_add_symbol(sym):
    s = sym.strip().upper()
    if not s: return
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
        self.thclass Orchestrator:
    def __init__(self):
        self.loop = None
        self.thread = None
        self.cex = CEXWatcher(CEX_PRIMARY)
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
            logger.exception("Orchestrator loop error: %s", e)

    async def _main(self):
        load_state()
        logger.info("Starting background components")

        # старт DEX і CEX poller
        if ccxtpro:
            await self.cex.start()
        else:
            logger.warning("ccxt.pro not available; CEX websocket disabled, REST fallback active")
        await self.dex.start()

        # ------------------- Top-10 auto-update -------------------
        async def top10_loop():
            while True:
                try:
                    self.update_top10_symbols()
                except Exception as e:
                    logger.debug("top10_loop error: %s", e)
                await asyncio.sleep(300)  # кожні 5 хв
        asyncio.create_task(top10_loop())

        # ------------------- broadcaster -------------------
        async def broadcaster():
            while True:
                try:
                    # process spreads
                    if state.get("monitoring", True):
                        for s in list(state.get("symbols", []))[:MAX_SYMBOLS]:
                            try:
                                process_spread(s)
                            except Exception:
                                pass

                    # emit live update
                    socketio.emit(
                        "live.update",
                        {
                            "symbols": state.get("symbols", []),
                            "dex_prices": dex_prices,
                            "cex_prices": cex_prices,
                            "last_update": last_update,
                            "time": time.time(),
                        },
                    )

                    # optionally edit telegram live panel if enabled
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

                await asyncio.sleep(5)  # live update кожні 5 секунд

        btask = asyncio.create_task(broadcaster())
        self.tasks.append(btask)
        try:
            await asyncio.gather(*self.tasks)
        except asyncio.CancelledError:
            logger.info("Orchestrator cancelled")
        finally:
            await self._shutdown()

    async def _shutdown(self):
        try:
            await self.cex.stop()
        except Exception:
            pass
        try:
            await self.dex.stop()
        except Exception:
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

    # ------------------- Top-10 helper -------------------
    def update_top10_symbols(self):
        """Автоматично оновлює топ-10 токенів за 1h % зміни ціни (MEXC USDT пари)"""
        global state
        try:
            client = getattr(ccxt, CEX_PRIMARY)({"enableRateLimit": True})
            tickers = client.fetch_tickers()
            usdt_pairs = [t for t in tickers.keys() if t.endswith("USDT")]
            changes = []
            for pair in usdt_pairs:
                ticker = tickers.get(pair, {})
                pct = ticker.get("percentage") or ticker.get("priceChangePercent1h") or 0.0
                try:
                    pct = float(pct)
                except Exception:
                    pct = 0.0
                symbol = pair.replace("USDT", "").upper()
                changes.append({"symbol": symbol, "pct": pct})
            # сортуємо по абсолютній зміні
            changes.sort(key=lambda x: abs(x["pct"]), reverse=True)
            top_symbols = [c["symbol"] for c in changes[:10]]
            if top_symbols != state.get("symbols"):
                state["symbols"] = top_symbols
                save_state()
                logger.info("Updated top-10 symbols: %s", ", ".join(top_symbols))
        except Exception as e:
            logger.exception("update_top10_symbols error: %s", e)

# ---------------- BOOT ----------------
orchestrator = Orchestrator()

if __name__ == "__main__":
    logger.info("🚀 Starting Live DEX<->CEX monitor")
    load_state()
    # set Telegram webhook if provided
    if TELEGRAM_TOKEN and WEBHOOK_URL:
        try:
            url = WEBHOOK_URL.rstrip("/") + "/webhook"
            r = requests.get(f"{TELEGRAM_API}/setWebhook?url={url}", timeout=10)
            logger.info("Set webhook result: %s", r.text[:200])
        except Exception as e:
            logger.warning("Failed to set webhook: %s", e)

    # start background orchestrator (async tasks)
    orchestrator.start()

    # run Flask-SocketIO server (use eventlet)
    # Note: make sure eventlet is installed for production-like concurrency
    socketio.run(app, host="0.0.0.0", port=PORT)