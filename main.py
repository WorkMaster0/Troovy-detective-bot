#!/usr/bin/env python3
# main.py - Integrated monitor: DEX (GMGN/Dexscreener) + CEX (Binance/Bybit/MEXC) + CoinGecko fallback
# + Telegram webhook + web UI. Reads TELEGRAM_TOKEN from environment.

import os
import time
import json
import logging
import threading
import requests
from datetime import datetime
from typing import Dict, List, Any, Optional
from flask import Flask, request, render_template_string, jsonify

import ccxt

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()  # optional
PORT = int(os.getenv("PORT", "10000"))

STATE_FILE = "state.json"
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "3.0"))   # seconds between price polls (user asked 3s)
TOP10_REFRESH = int(os.getenv("TOP10_REFRESH", "300"))    # 5 minutes
AUTO_POST_INTERVAL = int(os.getenv("AUTO_POST_INTERVAL", "600"))  # auto post interval when live
ALERT_OPEN_PCT = float(os.getenv("ALERT_OPEN_PCT", "2.0"))  # open threshold
ALERT_CLOSE_PCT = float(os.getenv("ALERT_CLOSE_PCT", "0.5"))  # close threshold
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "80"))

CEX_IDS = ["binance", "bybit", "mexc"]

DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search/?q={q}"
GMGN_API = "https://gmgn.ai/defi/quotation/v1/tokens/search?keyword={q}"
COINGECKO_SEARCH = "https://api.coingecko.com/api/v3/search?query={q}"
COINGECKO_PRICE = "https://api.coingecko.com/api/v3/simple/price"

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("monitor")

# ---------------- STATE ----------------
state: Dict[str, Any] = {
    "symbols": [],        # e.g. ["PEPE","BTC","AIA"]
    "chat_id": None,
    "msg_id": None,
    "live_to_telegram": False,
    "alert_open_pct": ALERT_OPEN_PCT,
    "alert_close_pct": ALERT_CLOSE_PCT
}

dex_prices: Dict[str, float] = {}
cex_prices: Dict[str, Dict[str, float]] = {}  # symbol -> {exid: price}
last_update: Dict[str, float] = {}
price_history: Dict[str, List[tuple]] = {}  # symbol -> [(ts, price_for_history)]
active_spreads: Dict[str, Dict[str, Any]] = {}
last_alert_time: Dict[str, float] = {}

_lock = threading.Lock()

# ---------------- STATE PERSISTENCE ----------------
def load_state():
    global state
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                s = json.load(f)
                state.update(s)
                logger.info("Loaded state.json: symbols=%d", len(state.get("symbols", [])))
    except Exception:
        logger.exception("load_state failed")

def save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception:
        logger.exception("save_state failed")

# ---------------- UTIL ----------------
def now_ts() -> float:
    return time.time()

def pretty_ts(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    return datetime.utcfromtimestamp(ts).strftime("%H:%M:%S")

def is_valid_price(p) -> bool:
    try:
        if p is None:
            return False
        p = float(p)
        if p != p or p <= 0 or p > 1e18:
            return False
        return True
    except Exception:
        return False

def pct_change(old: float, new: float) -> float:
    try:
        if old == 0:
            return 0.0
        return (new - old) / old * 100.0
    except Exception:
        return 0.0

def push_price_history(sym: str, price: float):
    if not is_valid_price(price):
        return
    lst = price_history.setdefault(sym, [])
    ts = now_ts()
    lst.append((ts, price))
    max_points = int(3600 / max(1, POLL_INTERVAL)) + 20
    if len(lst) > max_points:
        del lst[:len(lst) - max_points]

def get_price_1h_ago(sym: str) -> Optional[float]:
    lst = price_history.get(sym)
    if not lst:
        return None
    target = now_ts() - 3600.0
    best = None
    for ts, p in lst:
        if ts <= target:
            best = p
        else:
            break
    if best:
        return best
    return lst[0][1] if lst else None

# ---------------- TELEGRAM HELPERS ----------------
def tg_send(text: str) -> Optional[dict]:
    if not TELEGRAM_TOKEN or not state.get("chat_id"):
        logger.debug("tg_send skipped")
        return None
    try:
        payload = {"chat_id": state["chat_id"], "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}
        r = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception:
        logger.exception("tg_send failed")
        return None

def tg_edit(mid: int, text: str):
    if not TELEGRAM_TOKEN or not state.get("chat_id"):
        return None
    try:
        payload = {"chat_id": state["chat_id"], "message_id": mid, "text": text, "parse_mode": "Markdown"}
        r = requests.post(f"{TELEGRAM_API}/editMessageText", json=payload, timeout=8)
        if r.status_code != 200:
            logger.warning("tg_edit failed: %s", r.text)
        return r.json()
    except Exception:
        logger.exception("tg_edit failed")
        return None

# ---------------- DEX fetchers ----------------
HEADERS = {"User-Agent": "arb-monitor/1.0 (+https://example.com)"}

def fetch_from_gmgn(symbol: str) -> Optional[float]:
    try:
        q = symbol.upper()
        url = GMGN_API.format(q=q)
        r = requests.get(url, timeout=6, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
        items = data.get("data") or []
        for it in items:
            price = it.get("price_usd") or it.get("priceUsd") or it.get("price")
            if price:
                return float(price)
    except Exception:
        logger.debug("gmgn no data for %s", symbol)
    return None

def fetch_from_dexscreener(symbol: str) -> Optional[float]:
    try:
        q = symbol.upper()
        url = DEXSCREENER_SEARCH.format(q=q)
        r = requests.get(url, timeout=6, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
        pairs = data.get("pairs") or []
        if pairs:
            # pick first pair that has priceUsd
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
        logger.debug("dexscreener no data for %s", symbol)
    return None

# CoinGecko fallback: search symbol -> id -> simple/price
def coingecko_price(symbol: str) -> Optional[float]:
    try:
        s = symbol.lower()
        url = COINGECKO_SEARCH.format(q=s)
        r = requests.get(url, timeout=6, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
        coins = data.get("coins", [])
        if not coins:
            return None
        # prefer exact symbol match
        cg_id = None
        for c in coins:
            if c.get("symbol", "").lower() == s:
                cg_id = c.get("id")
                break
        if not cg_id:
            cg_id = coins[0].get("id")
        if not cg_id:
            return None
        pr = requests.get(COINGECKO_PRICE, params={"ids": cg_id, "vs_currencies": "usd"}, timeout=6, headers=HEADERS)
        pr.raise_for_status()
        j = pr.json()
        v = j.get(cg_id, {}).get("usd")
        if v:
            return float(v)
    except Exception:
        logger.debug("coingecko fallback failed for %s", symbol)
    return None

def fetch_price_from_dex_with_fallback(symbol: str) -> Optional[float]:
    v = fetch_from_gmgn(symbol)
    if v is not None:
        return v
    v = fetch_from_dexscreener(symbol)
    if v is not None:
        return v
    # fallback to coingecko
    v = coingecko_price(symbol)
    return v

# ---------------- CEX clients ----------------
def init_ccxt_clients():
    clients = {}
    for exid in CEX_IDS:
        try:
            clients[exid] = getattr(ccxt, exid)({"enableRateLimit": True})
        except Exception:
            logger.exception("init client %s failed", exid)
    return clients

# ---------------- ALERT logic ----------------
def process_alerts_for(sym: str):
    dex = dex_prices.get(sym)
    cex_map = cex_prices.get(sym, {})
    # pick best CEX (max) as before
    best_p = None
    best_ex = None
    for exid in CEX_IDS:
        p = cex_map.get(exid)
        if is_valid_price(p):
            if best_p is None or p > best_p:
                best_p = p
                best_ex = exid
    if not is_valid_price(dex) or not is_valid_price(best_p):
        return
    pct = (best_p - dex) / dex * 100.0
    now = now_ts()
    open_thresh = float(state.get("alert_open_pct", ALERT_OPEN_PCT))
    close_thresh = float(state.get("alert_close_pct", ALERT_CLOSE_PCT))
    # open
    if sym not in active_spreads and pct >= open_thresh:
        active_spreads[sym] = {"opened_pct": pct, "open_ts": now, "dex_price": dex, "cex_price": best_p, "cex_ex": best_ex}
        last_alert_time[sym] = now
        msg = (
            "🔔 *Spread OPENED*\n"
            f"Symbol: `{sym}`\n"
            f"DEX price: `{dex:.8f}`\n"
            f"CEX price ({best_ex}): `{best_p:.8f}`\n"
            f"Spread: *{pct:.2f}%*\n"
            f"Time: {datetime.utcfromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        logger.info("OPEN ALERT %s %.2f%%", sym, pct)
        tg_send(msg)
        return
    # close
    if sym in active_spreads:
        opened = active_spreads[sym]
        if pct <= close_thresh or pct <= opened.get("opened_pct", 0) * 0.5:
            dur = int(now - opened.get("open_ts", now))
            msg = (
                "✅ *Spread CLOSED*\n"
                f"Symbol: `{sym}`\n"
                f"Now: DEX `{dex:.8f}` | CEX `{best_p:.8f}` ({best_ex})\n"
                f"Current spread: *{pct:.2f}%*\n"
                f"Opened: *{opened.get('opened_pct'):.2f}%*, duration: {dur}s\n"
                f"Time: {datetime.utcfromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S UTC')}"
            )
            logger.info("CLOSE ALERT %s %.2f%%", sym, pct)
            tg_send(msg)
            active_spreads.pop(sym, None)
            last_alert_time[sym] = now
            return

# ---------------- BUILD ROWS / MD ----------------
def build_rows() -> List[Dict[str, Any]]:
    rows = []
    syms = list(state.get("symbols", []))[:MAX_SYMBOLS]
    for s in syms:
        dex = dex_prices.get(s)
        c_map = cex_prices.get(s, {})
        best = None
        best_ex = None
        for exid in CEX_IDS:
            p = c_map.get(exid)
            if is_valid_price(p):
                if best is None or p > best:
                    best = p
                    best_ex = exid
        spread = None
        if is_valid_price(dex) and is_valid_price(best):
            spread = (best - dex) / dex * 100.0
        p1h = get_price_1h_ago(s)
        ch1h = pct_change(p1h, best) if p1h and is_valid_price(best) else 0.0
        rows.append({
            "symbol": s,
            "dex": round(dex, 8) if is_valid_price(dex) else None,
            "best_cex": round(best, 8) if is_valid_price(best) else None,
            "best_ex": best_ex,
            "spread_pct": round(spread, 4) if spread is not None else None,
            "1h_change_pct": round(ch1h, 4),
            "last": last_update.get(s)
        })
    return rows

def build_markdown(rows: List[Dict[str, Any]]) -> str:
    lines = []
    lines.append("📡 *Live DEX ↔ CEX Monitor*")
    lines.append(f"_Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}_\n")
    lines.append("`SYMBOL   1hΔ%    DEX(USD)     CEX(USD)    Δ%`")
    lines.append("`--------------------------------------------------`")
    for r in rows:
        s = r["symbol"]
        ch = f"{r['1h_change_pct']:+6.2f}%"
        dex = f"{r['dex']:.8f}" if r["dex"] is not None else "—"
        cex = f"{r['best_cex']:.8f}({r['best_ex']})" if r["best_cex"] is not None else "—"
        sp = f"{r['spread_pct']:+6.2f}%" if r["spread_pct"] is not None else "—"
        lu = pretty_ts(r["last"])
        lines.append(f"`{s:<7}` {ch:>7}  {dex:>12}  {cex:>14}  {sp:>7}  {lu}")
    lines.append("\n`/add SYMBOL  /remove SYMBOL  /list  /alert <pct>  /live on|off  /top10  /alerts`")
    return "\n".join(lines)

# ---------------- TOP10 (CoinGecko primary) ----------------
def fetch_top10_coingecko() -> List[str]:
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": 1, "price_change_percentage": "1h"}
        r = requests.get(url, params=params, timeout=8, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
        # filter by price_change_percentage_1h_in_currency
        arr = []
        for c in data:
            pct1h = c.get("price_change_percentage_1h_in_currency")
            symbol = c.get("symbol", "").upper()
            if symbol and isinstance(pct1h, (int, float)):
                arr.append((symbol, pct1h))
        arr.sort(key=lambda x: x[1], reverse=True)
        top = [s for s, _ in arr[:10]]
        # ensure we return token symbols (not include stablecoins)
        return top
    except Exception:
        logger.exception("coingecko top10 failed")
    return []

# ---------------- BACKGROUND THREAD ----------------
class Monitor(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self._stop = threading.Event()
        self.ccxt_clients = init_ccxt_clients()
        self.next_top10 = 0
        self.next_auto = now_ts() + AUTO_POST_INTERVAL

    def stop(self):
        self._stop.set()

    def stopped(self):
        return self._stop.is_set()

    def run(self):
        logger.info("Background monitor started")
        while not self.stopped():
            now = now_ts()
            try:
                # refresh top10 periodically and set state.symbols automatically
                if now >= self.next_top10:
                    top = fetch_top10_coingecko()
                    if top:
                        with _lock:
                            # Replace monitored symbols with top list (user added symbols still preserved? We'll prioritize top list)
                            # We'll set state["symbols"] = unique(top + previous user symbols)
                            prev = state.get("symbols", [])
                            # keep user custom symbols that are not in top
                            combined = []
                            for s in top:
                                if s not in combined:
                                    combined.append(s)
                            for s in prev:
                                if s not in combined:
                                    combined.append(s)
                            state["symbols"] = combined[:MAX_SYMBOLS]
                            save_state()
                    self.next_top10 = now + TOP10_REFRESH

                syms = list(state.get("symbols", []))[:MAX_SYMBOLS]
                if syms:
                    # 1) DEX prices (blocking per-symbol)
                    for s in syms:
                        try:
                            v = fetch_price_from_dex_with_fallback(s)
                            if is_valid_price(v):
                                with _lock:
                                    dex_prices[s] = float(v)
                                    last_update[s] = now_ts()
                                    push_price_history(s, float(v))
                        except Exception:
                            logger.debug("dex fetch exception for %s", s)
                    # 2) CEX via ccxt (synchronous calls inside thread)
                    for exid, client in list(self.ccxt_clients.items()):
                        for s in syms:
                            # try variants
                            found = False
                            variants = [f"{s}/USDT", f"{s}USDT", f"{s}/USD"]
                            for v in variants:
                                try:
                                    tk = client.fetch_ticker(v)
                                    last = tk.get("last") or tk.get("close") or tk.get("price")
                                    if is_valid_price(last):
                                        with _lock:
                                            cex_prices.setdefault(s, {})[exid] = float(last)
                                            last_update[s] = now_ts()
                                        found = True
                                        break
                                except Exception:
                                    continue
                            if not found:
                                # try scanning tickers once (may be heavy)
                                try:
                                    all_t = client.fetch_tickers()
                                    for pair, tk in all_t.items():
                                        if s.upper() in pair.upper() and ("USDT" in pair.upper() or "USD" in pair.upper()):
                                            last = tk.get("last") or tk.get("close") or tk.get("price")
                                            if is_valid_price(last):
                                                with _lock:
                                                    cex_prices.setdefault(s, {})[exid] = float(last)
                                                    last_update[s] = now_ts()
                                                break
                                except Exception:
                                    pass
                    # 3) process alerts for symbols
                    for s in syms:
                        try:
                            process_alerts_for(s)
                        except Exception:
                            logger.exception("alerts error %s", s)
                # auto-post if enabled
                try:
                    if state.get("live_to_telegram") and state.get("chat_id"):
                        if now >= self.next_auto:
                            rows = build_rows()
                            md = build_markdown(rows)
                            res = tg_send(md)
                            if res and isinstance(res, dict):
                                mid = res.get("result", {}).get("message_id")
                                if mid:
                                    state["msg_id"] = int(mid)
                                    save_state()
                            self.next_auto = now + AUTO_POST_INTERVAL
                except Exception:
                    logger.exception("auto post failed")
            except Exception:
                logger.exception("monitor loop top exception")
            time.sleep(max(0.5, POLL_INTERVAL))
        logger.info("Background monitor stopped")

def init_ccxt_clients():
    clients = {}
    for exid in CEX_IDS:
        try:
            clients[exid] = getattr(ccxt, exid)({"enableRateLimit": True})
        except Exception:
            logger.exception("init ccxt client failed: %s", exid)
    return clients

# ---------------- FLASK UI + API + WEBHOOK ----------------
app = Flask(__name__)

INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Live Monitor</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    body{font-family:Arial,Helvetica,sans-serif;background:#0b1220;color:#e6eef8;max-width:1000px;margin:18px auto;padding:10px}
    table{width:100%;border-collapse:collapse;margin-top:8px}
    th,td{padding:8px 6px;border-bottom:1px solid #213040;text-align:left}
    th{color:#9fb4d9}
    tr:hover{background:#08202f}
    .small{color:#9fb4d9;font-size:0.9em}
    .badge{background:#1f6feb;padding:4px 8px;border-radius:6px;color:white;font-size:0.85em}
  </style>
</head>
<body>
  <h2>Live DEX ↔ CEX Monitor</h2>
  <div class="small">Last update: <span id="last">—</span> UTC • Refresh: 15s</div>
  <div style="margin-top:10px">
    <table><thead><tr><th>Symbol</th><th>1h Δ%</th><th>DEX</th><th>Best CEX (ex)</th><th>Spread%</th><th>Last</th></tr></thead>
    <tbody id="tbody"></tbody></table>
  </div>
  <div class="small" style="margin-top:8px">Commands via Telegram: /add /remove /list /clear /alert &lt;pct&gt; /live on|off /top10 /alerts</div>
<script>
async function fetchLatest(){
  try{
    const res = await fetch('/api/latest');
    const j = await res.json();
    document.getElementById('last').innerText = j.time || '—';
    const tbody = document.getElementById('tbody');
    tbody.innerHTML = '';
    (j.rows || []).forEach(r=>{
      const tr = document.createElement('tr');
      const ch = (r['1h_change_pct']||0).toFixed(2)+'%';
      const dex = r.dex!=null? r.dex.toFixed(8) : '—';
      const cex = r.best_cex!=null ? r.best_cex.toFixed(8)+' ('+ (r.best_ex||'') +')' : '—';
      const sp = r.spread_pct!=null ? r.spread_pct.toFixed(2)+'%' : '—';
      const last = r.last? new Date(r.last*1000).toISOString().substr(11,8) : '—';
      tr.innerHTML = `<td><strong>${r.symbol}</strong></td><td>${ch}</td><td>${dex}</td><td>${cex}</td><td>${sp}</td><td>${last}</td>`;
      tbody.appendChild(tr);
    });
  }catch(e){
    console.error(e);
  }
}
fetchLatest();
setInterval(fetchLatest, 15000);
</script>
</body>
</html>
"""

@app.route("/", methods=["GET"])
def index():
    return render_template_string(INDEX_HTML)

@app.route("/api/latest", methods=["GET"])
def api_latest():
    rows = build_rows()
    return jsonify({"time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"), "rows": rows})

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False}), 400
    if not data:
        return jsonify({"ok": True})
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
            tg_send("🤖 Monitor online. Use /add SYMBOL")
        elif cmd == "/help":
            tg_send("Commands: /add /remove /list /clear /alert <pct> /live on|off /top10 /alerts /status /help")
        elif cmd == "/add":
            if len(parts) >= 2:
                sym = parts[1].upper()
                if sym not in state["symbols"]:
                    state["symbols"].append(sym)
                    save_state()
                    tg_send(f"✅ Added {sym}")
                else:
                    tg_send(f"⚠️ {sym} already monitored")
        elif cmd == "/remove":
            if len(parts) >= 2:
                sym = parts[1].upper()
                if sym in state["symbols"]:
                    state["symbols"].remove(sym)
                    save_state()
                    tg_send(f"🗑 Removed {sym}")
                else:
                    tg_send(f"⚠️ {sym} not monitored")
        elif cmd == "/list":
            tg_send("Monitored: " + (", ".join(state["symbols"]) if state["symbols"] else "—"))
        elif cmd == "/clear":
            state["symbols"] = []
            save_state()
            tg_send("🧹 Cleared all symbols")
        elif cmd == "/alert":
            if len(parts) >= 2:
                try:
                    pct = float(parts[1])
                    state["alert_open_pct"] = pct
                    save_state()
                    tg_send(f"✅ Alert threshold set to {pct:.2f}%")
                except Exception:
                    tg_send("Usage: /alert <pct> (numeric)")
            else:
                tg_send(f"Current alert threshold: {state.get('alert_open_pct'):.2f}%")
        elif cmd == "/live":
            if len(parts) >= 2 and parts[1].lower() in ("on","off"):
                state["live_to_telegram"] = (parts[1].lower()=="on")
                save_state()
                tg_send(f"Live-to-Telegram set to {state['live_to_telegram']}")
            else:
                tg_send("Usage: /live on|off")
        elif cmd == "/status":
            syms = state.get("symbols", [])
            txt = f"Symbols: {', '.join(syms) if syms else '—'}\nAlert open: {state.get('alert_open_pct'):.2f}%\nLive->Telegram: {state.get('live_to_telegram')}\nActive spreads: {len(active_spreads)}"
            tg_send(txt)
        elif cmd == "/top10":
            rows = build_rows()
            sorted_rows = sorted(rows, key=lambda r: r.get("1h_change_pct",0), reverse=True)[:10]
            lines = ["Symbol   1hΔ%"]
            for r in sorted_rows:
                lines.append(f"{r['symbol']:<7} {r['1h_change_pct']:+6.2f}%")
            tg_send("📈 *Top 10 (1h)*\n```\n" + "\n".join(lines) + "\n```")
        elif cmd == "/alerts":
            if not active_spreads:
                tg_send("No active spreads.")
            else:
                lines = []
                for s, info in active_spreads.items():
                    lines.append(f"{s}: opened {info.get('opened_pct'):.2f}% at {pretty_ts(info.get('open_ts'))}")
                tg_send("Active spreads:\n```\n" + "\n".join(lines) + "\n```")
        else:
            tg_send("❓ Unknown command. /help")
    except Exception:
        logger.exception("webhook cmd error")
        tg_send("⚠️ Error processing command.")
    return jsonify({"ok": True})

# ---------------- BOOT ----------------
load_state()
_monitor = Monitor()
_monitor.start()

# Try set webhook if provided
if TELEGRAM_TOKEN and WEBHOOK_URL:
    try:
        url = WEBHOOK_URL.rstrip("/") + "/webhook"
        r = requests.get(f"{TELEGRAM_API}/setWebhook?url={url}", timeout=8)
        logger.info("setWebhook result: %s", r.text[:200])
    except Exception:
        logger.exception("setWebhook call failed")

if __name__ == "__main__":
    logger.info("Starting dev server")
    app.run(host="0.0.0.0", port=PORT, threaded=True)