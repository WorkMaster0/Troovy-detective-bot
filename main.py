#!/usr/bin/env python3
"""
main.py - Live DEX <-> CEX monitor (Binance/Bybit/MEXC) + Telegram webhook + web UI
Safe demo: reads public data only; no trading; no API keys required.

How to run (Render):
 - requirements.txt must include: flask, ccxt, requests, gunicorn
 - Start command (Render): gunicorn main:app --bind 0.0.0.0:$PORT --workers 1 --threads 4
 - Set environment variables: TELEGRAM_TOKEN (required), WEBHOOK_URL (optional, e.g. https://your-app.onrender.com)
"""

import os
import time
import json
import logging
import threading
import requests
from datetime import datetime
from typing import Dict, List, Any, Optional
from flask import Flask, request, render_template_string, jsonify

import ccxt  # synchronous; used inside background thread (run blocking safely)

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()  # optional, e.g. https://app.onrender.com
PORT = int(os.getenv("PORT", "10000"))

STATE_FILE = "state.json"
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "11.0"))   # seconds between polls
AUTO_POST_INTERVAL = int(os.getenv("AUTO_POST_INTERVAL", "600"))  # seconds: auto post to telegram when live enabled
ALERT_THRESHOLD_PCT = float(os.getenv("ALERT_THRESHOLD_PCT", "3.0"))  # default open threshold
CLOSE_THRESHOLD_PCT = float(os.getenv("CLOSE_THRESHOLD_PCT", "0.5"))
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "80"))

CEX_IDS = ["binance", "bybit", "mexc"]  # cctx ids order matters for priority display

DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search/?q={q}"
GMGN_API = "https://gmgn.ai/defi/quotation/v1/tokens/search?keyword={q}"

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("arb-monitor")

# ---------------- STATE ----------------
state: Dict[str, Any] = {
    "symbols": [],  # list like ["JELLY","AIA","BTC"]
    "chat_id": None,
    "msg_id": None,
    "live_to_telegram": False,
    "alert_threshold_pct": ALERT_THRESHOLD_PCT,
    "close_threshold_pct": CLOSE_THRESHOLD_PCT
}

dex_prices: Dict[str, float] = {}         # symbol -> price
cex_prices: Dict[str, Dict[str, float]] = {}  # sym -> {exid: price}
last_update: Dict[str, float] = {}        # sym -> ts
price_history: Dict[str, List[tuple]] = {}  # sym -> list of (ts, price) for 1h change
active_spreads: Dict[str, Dict[str, Any]] = {}  # sym -> open info
last_alert_time: Dict[str, float] = {}    # cooldown

_lock = threading.Lock()

# ---------------- STATE PERSISTENCE ----------------
def load_state():
    global state
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                s = json.load(f)
                state.update(s)
                logger.info("Loaded state.json, symbols=%d", len(state.get("symbols", [])))
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

# ---------------- UTIL ----------------
def now_ts() -> float:
    return time.time()

def pretty_ts(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    return datetime.utcfromtimestamp(ts).strftime("%H:%M:%S")

def pct_change(old: float, new: float) -> float:
    try:
        if old == 0:
            return 0.0
        return (new - old) / old * 100.0
    except Exception:
        return 0.0

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

# ---------------- TELEGRAM HELPERS ----------------
def tg_send_text(text: str) -> Optional[dict]:
    if not TELEGRAM_TOKEN or not state.get("chat_id"):
        logger.debug("tg_send skipped (no token/chat_id)")
        return None
    try:
        payload = {"chat_id": state["chat_id"], "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}
        r = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.exception("tg_send_text error: %s", e)
        return None

def tg_edit_text(message_id: int, text: str):
    if not TELEGRAM_TOKEN or not state.get("chat_id"):
        return None
    try:
        payload = {"chat_id": state["chat_id"], "message_id": message_id, "text": text, "parse_mode": "Markdown"}
        r = requests.post(f"{TELEGRAM_API}/editMessageText", json=payload, timeout=8)
        if r.status_code != 200:
            logger.warning("tg_edit failed: %s %s", r.status_code, r.text)
        return r.json()
    except Exception as e:
        logger.exception("tg_edit error: %s", e)
        return None

# ---------------- DEX FETCHERS (blocking; run in bg thread) ----------------
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
    except Exception as e:
        logger.debug("gmgn err %s: %s", symbol, e)
    return None

def fetch_from_dexscreener(symbol: str) -> Optional[float]:
    try:
        q = symbol.upper()
        url = DEXSCREENER_SEARCH.format(q=q)
        r = requests.get(url, timeout=6)
        r.raise_for_status()
        data = r.json()
        pairs = data.get("pairs") or []
        if pairs:
            # choose first available price
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

def fetch_price_from_dex(symbol: str) -> Optional[float]:
    v = fetch_from_gmgn(symbol)
    if v is not None:
        return v
    return fetch_from_dexscreener(symbol)

# ---------------- CEX FETCH (ccxt sync clients used in bg thread) ----------------
def init_cex_clients():
    clients = {}
    for exid in CEX_IDS:
        try:
            clients[exid] = getattr(ccxt, exid)({"enableRateLimit": True})
        except Exception as e:
            logger.exception("init_ccxt %s err: %s", exid, e)
    return clients

# ---------------- PRICE HISTORY ----------------
def push_price_history(sym: str, price: float):
    if not is_valid_price(price):
        return
    lst = price_history.setdefault(sym, [])
    ts = now_ts()
    lst.append((ts, price))
    # keep roughly 1h of history depending on poll interval (margin)
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
    # no 1h old value, return earliest
    return lst[0][1] if lst else None

# ---------------- ALERT LOGIC ----------------
def try_open_or_close(sym: str):
    dex = dex_prices.get(sym)
    cex_map = cex_prices.get(sym, {})
    if dex is None:
        return
    # choose best cex (max) to compare
    best_p = None
    best_ex = None
    for exid in CEX_IDS:
        p = cex_map.get(exid)
        if is_valid_price(p):
            if best_p is None or p > best_p:
                best_p = p
                best_ex = exid
    if best_p is None or dex == 0:
        return
    pct = (best_p - dex) / dex * 100.0
    now = now_ts()
    open_thresh = float(state.get("alert_threshold_pct", ALERT_THRESHOLD_PCT))
    close_thresh = float(state.get("close_threshold_pct", CLOSE_THRESHOLD_PCT))

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
        logger.info("ALERT OPEN %s %.2f%%", sym, pct)
        tg_send_text(msg)
        return

    # close
    if sym in active_spreads:
        opened = active_spreads[sym]
        # close when pct <= close_thresh OR when spread significantly reduced compared to opened_pct
        if pct <= close_thresh or pct <= opened.get("opened_pct", 0) * 0.5:
            duration = int(now - opened.get("open_ts", now))
            msg = (
                "✅ *Spread CLOSED*\n"
                f"Symbol: `{sym}`\n"
                f"Now: DEX `{dex:.8f}` | CEX `{best_p:.8f}` ({best_ex})\n"
                f"Current spread: *{pct:.2f}%*\n"
                f"Opened: *{opened.get('opened_pct'):.2f}%*, duration: {duration}s\n"
                f"Time: {datetime.utcfromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S UTC')}"
            )
            logger.info("ALERT CLOSE %s %.2f%%", sym, pct)
            tg_send_text(msg)
            active_spreads.pop(sym, None)
            last_alert_time[sym] = now
            return

# ---------------- BUILD TELEGRAM/WEB TEXT ----------------
def build_live_text_rows() -> List[Dict[str, Any]]:
    rows = []
    syms = list(state.get("symbols", []))[:MAX_SYMBOLS]
    for s in syms:
        dex = dex_prices.get(s)
        cex_map = cex_prices.get(s, {})
        best_p = None
        best_ex = None
        for exid in CEX_IDS:
            p = cex_map.get(exid)
            if is_valid_price(p):
                if best_p is None or p > best_p:
                    best_p = p
                    best_ex = exid
        pct = None
        if is_valid_price(dex) and is_valid_price(best_p):
            pct = (best_p - dex) / dex * 100.0
        price_1h = get_price_1h_ago(s)
        change_1h = pct_change(price_1h, best_p) if price_1h and is_valid_price(best_p) else 0.0
        rows.append({
            "symbol": s,
            "dex": round(dex, 8) if is_valid_price(dex) else None,
            "best_cex": round(best_p, 8) if is_valid_price(best_p) else None,
            "best_ex": best_ex,
            "spread_pct": round(pct, 4) if pct is not None else None,
            "1h_change_pct": round(change_1h, 4),
            "last": last_update.get(s)
        })
    return rows

def build_live_markdown_text(rows: List[Dict[str, Any]]) -> str:
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
    lines.append("\n`/add SYMBOL  /remove SYMBOL  /list  /alert <pct>  /live on|off`")
    return "\n".join(lines)

# ---------------- BACKGROUND MONITOR THREAD ----------------
class MonitorThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self._stop = threading.Event()
        self.cex_clients = init_cex_clients()
        logger.info("MonitorThread initialized")

    def stop(self):
        self._stop.set()

    def stopped(self):
        return self._stop.is_set()

    def run(self):
        logger.info("MonitorThread started")
        next_auto = now_ts() + AUTO_POST_INTERVAL
        while not self.stopped():
            syms = list(state.get("symbols", []))[:MAX_SYMBOLS]
            if syms:
                # 1) fetch DEX prices (blocking but per-symbol)
                for s in syms:
                    try:
                        v = fetch_price_from_dex(s)
                        if is_valid_price(v):
                            with _lock:
                                dex_prices[s] = float(v)
                                last_update[s] = now_ts()
                                push_price_history(s, float(v))
                    except Exception as e:
                        logger.debug("dex fetch err %s: %s", s, e)
                # 2) fetch CEX prices via ccxt clients
                for exid, client in list(self.cex_clients.items()):
                    for s in syms:
                        # try several variants
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
                        # if not found via fetch_ticker variants, try scanning tickers (less efficient)
                        if not found:
                            try:
                                all_t = client.fetch_tickers()
                                for pair, tk in all_t.items():
                                    try:
                                        if s.upper() in pair.upper() and ("USDT" in pair.upper() or "USD" in pair.upper()):
                                            last = tk.get("last") or tk.get("close") or tk.get("price")
                                            if is_valid_price(last):
                                                with _lock:
                                                    cex_prices.setdefault(s, {})[exid] = float(last)
                                                    last_update[s] = now_ts()
                                                found = True
                                                break
                                    except Exception:
                                        continue
                            except Exception:
                                # fetch_tickers sometimes fails; ignore
                                pass
                # 3) process spreads & alerts
                for s in syms:
                    try:
                        try_open_or_close(s)
                    except Exception:
                        logger.exception("try_open_or_close error for %s", s)
            # auto-post live-to-telegram if enabled and interval passed
            try:
                if state.get("live_to_telegram") and state.get("chat_id"):
                    now = now_ts()
                    if now >= next_auto:
                        rows = build_live_text_rows()
                        txt = build_live_markdown_text(rows)
                        res = tg_send_text(txt)
                        # store message id to edit later
                        if res and isinstance(res, dict):
                            mid = res.get("result", {}).get("message_id")
                            if mid:
                                state["msg_id"] = int(mid)
                                save_state()
                        next_auto = now + AUTO_POST_INTERVAL
            except Exception:
                logger.exception("auto post error")
            # sleep
            time.sleep(max(1.0, POLL_INTERVAL))
        logger.info("MonitorThread stopped")

# ---------------- FLASK APP ----------------
app = Flask(__name__)

INDEX_HTML = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <title>Live DEX ↔ CEX Monitor</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      body { font-family: Arial, sans-serif; max-width:1000px; margin:16px auto; background:#0b1220; color:#e6eef8; }
      table { width:100%; border-collapse:collapse; margin-top:8px; }
      th, td { padding:8px 6px; border-bottom:1px solid #213040; text-align:left; }
      th { color:#9fb4d9; }
      tr:hover { background:#08202f; }
      .small { color:#9fb4d9; font-size:0.9em; }
      .badge { background:#1f6feb; padding:4px 8px; border-radius:6px; color:white; font-size:0.9em; }
    </style>
  </head>
  <body>
    <h2>Live DEX ↔ CEX Monitor</h2>
    <div class="small">Last update: <span id="last">—</span> UTC • Auto refresh: 15s</div>
    <div style="margin-top:10px;">
      <table>
        <thead><tr><th>Symbol</th><th>1h Δ%</th><th>DEX</th><th>Best CEX (ex)</th><th>Spread%</th><th>Last</th></tr></thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>
    <div style="margin-top:8px;" class="small">Commands via Telegram: /add /remove /list /clear /alert &lt;pct&gt; /live on|off /status /help</div>

<script>
async function fetchLatest(){
  try{
    const res = await fetch('/api/latest');
    const j = await res.json();
    document.getElementById('last').innerText = j.time || '—';
    const tbody = document.getElementById('tbody');
    tbody.innerHTML = '';
    (j.rows || []).forEach(r=>{
      const tr=document.createElement('tr');
      const ch = (r["1h_change_pct"]||0).toFixed(2)+'%';
      const dex = r.dex!=null? r.dex.toFixed(8): '—';
      const cex = r.best_cex!=null? r.best_cex.toFixed(8)+' ('+ (r.best_ex||'') +')' : '—';
      const sp = r.spread_pct!=null? r.spread_pct.toFixed(2)+'%':'—';
      const last = r.last? new Date(r.last*1000).toISOString().substr(11,8):'—';
      tr.innerHTML = `<td><strong>${r.symbol}</strong></td><td>${ch}</td><td>${dex}</td><td>${cex}</td><td>${sp}</td><td>${last}</td>`;
      tbody.appendChild(tr);
    });
  }catch(e){
    console.error('fetchLatest', e);
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
    rows = build_live_text_rows()
    return jsonify({"time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"), "rows": rows})

# ---------------- TELEGRAM webhook ----------------
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False}), 400
    if not data:
        return jsonify({"ok": True})
    # message can be in 'message' or 'edited_message'
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
            tg_send_text("🤖 Live monitor online. Use /add SYMBOL")
        elif cmd == "/help":
            tg_send_text("Commands:\n/add SYMBOL\n/remove SYMBOL\n/list\n/clear\n/alert <pct>\n/live on|off\n/status\n/help")
        elif cmd == "/add":
            if len(parts) >= 2:
                sym = parts[1].upper()
                if sym not in state["symbols"]:
                    state["symbols"].append(sym)
                    save_state()
                    tg_send_text(f"✅ Added {sym}")
                else:
                    tg_send_text(f"⚠️ {sym} already monitored")
        elif cmd == "/remove":
            if len(parts) >= 2:
                sym = parts[1].upper()
                if sym in state["symbols"]:
                    state["symbols"].remove(sym)
                    save_state()
                    tg_send_text(f"🗑 Removed {sym}")
                else:
                    tg_send_text(f"⚠️ {sym} not monitored")
        elif cmd == "/list":
            syms = state.get("symbols", [])
            tg_send_text("Monitored: " + (", ".join(syms) if syms else "—"))
        elif cmd == "/clear":
            state["symbols"] = []
            save_state()
            tg_send_text("🧹 Cleared all symbols")
        elif cmd == "/alert":
            if len(parts) >= 2:
                try:
                    pct = float(parts[1])
                    state["alert_threshold_pct"] = pct
                    save_state()
                    tg_send_text(f"✅ Alert threshold set to {pct:.2f}%")
                except Exception:
                    tg_send_text("Usage: /alert <pct> (numeric)")
            else:
                tg_send_text(f"Current alert threshold: {state.get('alert_threshold_pct'):.2f}%")
        elif cmd == "/live":
            if len(parts) >= 2 and parts[1].lower() in ("on", "off"):
                state["live_to_telegram"] = (parts[1].lower()=="on")
                save_state()
                tg_send_text(f"Live-to-Telegram set to {state['live_to_telegram']}")
            else:
                tg_send_text("Usage: /live on|off")
        elif cmd == "/status":
            syms = state.get("symbols", [])
            txt = f"Symbols: {', '.join(syms) if syms else '—'}\nAlert threshold: {state.get('alert_threshold_pct'):.2f}%\nLive->Telegram: {state.get('live_to_telegram')}\nActive spreads: {len(active_spreads)}"
            tg_send_text(txt)
        elif cmd == "/top10":
            # compute top10 by 1h change using cex prices (best_cex)
            rows = build_live_text_rows()
            sorted_rows = sorted(rows, key=lambda r: r.get("1h_change_pct",0), reverse=True)[:10]
            lines = ["Symbol   1hΔ%"]
            for r in sorted_rows:
                lines.append(f"{r['symbol']:<7} {r['1h_change_pct']:+6.2f}%")
            tg_send_text("📈 *Top 10 (1h)*\n```\n" + "\n".join(lines) + "\n```")
        else:
            tg_send_text("❓ Unknown command. /help")
    except Exception as e:
        logger.exception("webhook cmd error: %s", e)
        tg_send_text("⚠️ Error processing command.")
    return jsonify({"ok": True})

# ---------------- BOOT ----------------
# Start background monitor thread at import time so Gunicorn workers run it
_monitor_thread = MonitorThread()
_monitor_thread.start()

# try set webhook automatically if TELEGRAM_TOKEN and WEBHOOK_URL provided
if TELEGRAM_TOKEN and WEBHOOK_URL:
    try:
        url = WEBHOOK_URL.rstrip("/") + "/webhook"
        r = requests.get(f"{TELEGRAM_API}/setWebhook?url={url}", timeout=8)
        logger.info("Set webhook result: %s", r.text[:200])
    except Exception as e:
        logger.warning("Failed to set webhook: %s", e)

if __name__ == "__main__":
    load_state()
    logger.info("Starting Flask dev server")
    app.run(host="0.0.0.0", port=PORT, threaded=True)