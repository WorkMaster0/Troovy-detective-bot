#!/usr/bin/env python3
"""
Live Top-10 (1h change) monitor: MEXC (CEX) + DEX (GMGN/Dextools/Dexscreener)
- Uses Telegram webhook to receive commands
- Periodically (every 5s) computes top-10 by 1h change and edits a single Telegram message
- State persisted in state.json
"""
import os
import time
import json
import logging
import asyncio
import requests
from datetime import datetime
from threading import Thread
from typing import Dict, Optional, List, Any
from flask import Flask, request, jsonify

# try ccxt (sync). ccxt.pro optional but not required here.
try:
    import ccxt
except Exception:
    ccxt = None

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")      # required
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")            # e.g. https://your-app.onrender.com
PORT = int(os.getenv("PORT", "10000"))

STATE_FILE = "state.json"
POLL_INTERVAL = 5.0              # seconds between broadcasts / recomputations
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "400"))  # how many pairs to evaluate for 1h change
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "10"))       # threadpool workers for blocking IO
TOP_N = 10

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# DEX endpoints (priority order)
GMGN_API = "https://gmgn.ai/defi/quotation/v1/tokens/search?keyword={q}"
DEXTOOLS_API = "https://www.dextools.io/shared/analytics/pair-search?query={q}"
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/search/?q={q}"

# default CEX for candidate discovery
CEX_ID = os.getenv("CEX_PRIMARY", "mexc")

# ---------------- logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("top10-monitor")

# ---------------- persistent state ----------------
state: Dict[str, Any] = {
    "running": False,        # whether /top monitoring is active
    "chat_id": None,
    "msg_id": None,          # edited message id in Telegram
    "last_run": None,
}
# caches
cex_markets = []    # list of symbol strings discovered on CEX (USDT)
# runtime data
dex_prices: Dict[str, float] = {}
cex_prices: Dict[str, float] = {}
pct_1h: Dict[str, float] = {}

# ---------------- helpers: state ----------------
def load_state():
    global state
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                s = json.load(f)
                state.update(s)
                logger.info("Loaded state")
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

# ---------------- Telegram helpers ----------------
def tg_send(text: str) -> Optional[dict]:
    if not TELEGRAM_TOKEN or not state.get("chat_id"):
        logger.debug("tg_send: token/chat_id missing")
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

# ---------------- DEX fetchers ----------------
def fetch_from_gmgn(symbol: str) -> Optional[float]:
    try:
        url = GMGN_API.format(q=symbol)
        r = requests.get(url, timeout=7)
        r.raise_for_status()
        data = r.json()
        for it in (data.get("data") or []):
            price = it.get("price_usd") or it.get("priceUsd") or it.get("price")
            if price:
                return float(price)
    except Exception:
        pass
    return None

def fetch_from_dextools(symbol: str) -> Optional[float]:
    try:
        url = DEXTOOLS_API.format(q=symbol)
        r = requests.get(url, timeout=7)
        r.raise_for_status()
        data = r.json()
        for p in (data.get("pairs") or []):
            price = p.get("priceUsd") or p.get("price")
            if price:
                return float(price)
    except Exception:
        pass
    return None

def fetch_from_dexscreener(symbol: str) -> Optional[float]:
    try:
        url = DEXSCREENER_API.format(q=symbol)
        r = requests.get(url, timeout=7)
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
    # priority: GMGN -> Dextools -> Dexscreener
    s = symbol.upper()
    for fn in (fetch_from_gmgn, fetch_from_dextools, fetch_from_dexscreener):
        try:
            p = fn(s)
            if p is not None:
                return p
        except Exception:
            continue
    return None

# ---------------- CEX discovery & 1h change ----------------
def discover_cex_markets(limit: int = MAX_CANDIDATES) -> List[str]:
    """
    Load markets from CEX (ccxt) and return list of candidate symbols
    (filtering for USDT pairs). We keep up to `limit` symbols.
    """
    global cex_markets
    if ccxt is None:
        logger.error("ccxt not installed")
        return []
    try:
        ex = getattr(ccxt, CEX_ID)({"enableRateLimit": True})
        ex.load_markets()
        symbols = []
        for s, m in ex.markets.items():
            if ('USDT' in s.upper()) and (m.get('contract') or m.get('future') or m.get('type') in ('future','swap','spot')):
                symbols.append(s)
        # dedupe & limit
        symbols = sorted(list(set(symbols)))
        cex_markets = symbols[:limit]
        logger.info("Discovered %d candidate CEX markets (limit=%d)", len(cex_markets), limit)
        return cex_markets
    except Exception as e:
        logger.exception("discover_cex_markets error: %s", e)
        return []

def compute_1h_change_for_symbol(symbol: str) -> Optional[float]:
    """
    Try to compute 1h percent change using fetch_ohlcv (1h) if available.
    Return percent change (float) or None.
    """
    if ccxt is None:
        return None
    try:
        ex = getattr(ccxt, CEX_ID)({"enableRateLimit": True})
        # attempt fetch ohlcv 1h with limit=2
        ohlcv = ex.fetch_ohlcv(symbol, timeframe='1h', limit=2)
        if not ohlcv or len(ohlcv) < 2:
            return None
        prev = ohlcv[-2]  # [ts, open, high, low, close, volume]
        cur = ohlcv[-1]
        prev_close = prev[4]
        cur_close = cur[4]
        if prev_close and prev_close != 0:
            pct = (cur_close - prev_close) / prev_close * 100.0
            return float(pct)
    except Exception:
        # some markets may not support ohlcv; ignore
        return None
    return None

def fetch_current_price_cex(symbol: str) -> Optional[float]:
    if ccxt is None:
        return None
    try:
        ex = getattr(ccxt, CEX_ID)({"enableRateLimit": True})
        t = ex.fetch_ticker(symbol)
        last = t.get("last") or t.get("close") or t.get("info", {}).get("lastPrice")
        if last is not None:
            return float(last)
    except Exception:
        return None
    return None

# ---------------- table builder ----------------
def build_table(top_list: List[Dict[str, Any]]) -> str:
    """
    top_list: list of dicts {symbol, cex_price, dex_price, pct_1h}
    Returns markdown text for Telegram message.
    """
    lines = []
    lines.append("📊 *Live Top-10 by 1h change (MEXC + DEX)*")
    lines.append(f"_Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}_\n")
    lines.append("`R  SYMBOL     PRICE(USD)    DEX(USD)     Δ1h%`")
    lines.append("`------------------------------------------------`")
    idx = 1
    for row in top_list:
        sym = row['symbol']
        cex_p = row.get('cex_price')
        dex_p = row.get('dex_price')
        pct = row.get('pct_1h')
        cex_s = f"{cex_p:.8f}" if cex_p is not None else "—"
        dex_s = f"{dex_p:.8f}" if dex_p is not None else "—"
        pct_s = f"{pct:+6.2f}%" if pct is not None else "—"
        lines.append(f"`{idx:>2}` `{sym:<8}` {cex_s:>12}  {dex_s:>10}  {pct_s:>7}")
        idx += 1
    lines.append("\n`/top - toggle top10 monitor`")
    lines.append("`/add SYMBOL /remove SYMBOL /list /help`")
    return "\n".join(lines)

# ---------------- Orchestration: background async worker ----------------
class Top10Orchestrator:
    def __init__(self):
        self.loop = None
        self.thread = None
        self.task = None
        self.running = False
        self.executor = None

    def start(self):
        if self.running:
            return
        self.loop = asyncio.new_event_loop()
        self.executor = None
        self.thread = Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self.running = True

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        # create ThreadPoolExecutor for blocking IO
        import concurrent.futures
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)
        try:
            self.loop.run_until_complete(self._main())
        except Exception as e:
            logger.exception("orchestrator loop error: %s", e)
        finally:
            self.executor.shutdown(wait=False)

    async def _gather_1h_changes(self, symbols: List[str]) -> Dict[str, float]:
        loop = asyncio.get_event_loop()
        coros = []
        results: Dict[str, float] = {}
        # schedule compute_1h_change_for_symbol in executor
        for s in symbols:
            coros.append(loop.run_in_executor(self.executor, compute_1h_change_for_symbol, s))
        res = await asyncio.gather(*coros, return_exceptions=True)
        for s, r in zip(symbols, res):
            if isinstance(r, Exception) or r is None:
                continue
            try:
                results[s] = float(r)
            except Exception:
                continue
        return results

    async def _fetch_dex_prices(self, symbols: List[str]) -> Dict[str, float]:
        loop = asyncio.get_event_loop()
        coros = [loop.run_in_executor(self.executor, fetch_price_from_dex, s) for s in symbols]
        res = await asyncio.gather(*coros, return_exceptions=True)
        out = {}
        for s, r in zip(symbols, res):
            if isinstance(r, Exception) or r is None:
                continue
            try:
                out[s] = float(r)
            except Exception:
                continue
        return out

    async def _fetch_cex_prices(self, symbols: List[str]) -> Dict[str, float]:
        loop = asyncio.get_event_loop()
        coros = [loop.run_in_executor(self.executor, fetch_current_price_cex, s) for s in symbols]
        res = await asyncio.gather(*coros, return_exceptions=True)
        out = {}
        for s, r in zip(symbols, res):
            if isinstance(r, Exception) or r is None:
                continue
            try:
                out[s] = float(r)
            except Exception:
                continue
        return out

    async def _main(self):
        # discover CEX markets once (candidates)
        cand = await asyncio.get_event_loop().run_in_executor(None, discover_cex_markets, MAX_CANDIDATES)
        if not cand:
            logger.warning("No candidate markets discovered; orchestrator will still run but produce no top list.")
        else:
            logger.info("Starting monitor for %d candidate markets", len(cand))

        while True:
            try:
                if not state.get("running"):
                    await asyncio.sleep(1.0)
                    continue

                # compute 1h changes for candidates
                cands = cand[:MAX_CANDIDATES]
                # 1h change (may be empty for many symbols)
                logger.debug("Computing 1h changes for %d candidates", len(cands))
                changes = await self._gather_1h_changes(cands)

                if not changes:
                    logger.debug("No 1h changes available this round")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                # sort by absolute pct descending, pick top N symbols
                sorted_syms = sorted(changes.items(), key=lambda x: abs(x[1]), reverse=True)
                top_syms = [s for s, p in sorted_syms[:TOP_N]]

                # fetch CEX current prices and DEX prices for top symbols in parallel
                cex_map = await self._fetch_cex_prices(top_syms)
                dex_map = await self._fetch_dex_prices(top_syms)

                # prepare top list rows
                top_list = []
                for s in top_syms:
                    top_list.append({
                        "symbol": s,
                        "pct_1h": changes.get(s),
                        "cex_price": cex_map.get(s),
                        "dex_price": dex_map.get(s),
                    })

                # build markdown table and send/edit message
                txt = build_table(top_list)
                # send or edit
                if state.get("chat_id"):
                    if not state.get("msg_id"):
                        res = await asyncio.get_event_loop().run_in_executor(self.executor, tg_send, txt)
                        if res and isinstance(res, dict):
                            mid = res.get("result", {}).get("message_id")
                            if mid:
                                state["msg_id"] = int(mid)
                                save_state()
                    else:
                        # edit
                        await asyncio.get_event_loop().run_in_executor(self.executor, tg_edit, state["msg_id"], txt)
                # small bookkeeping
                state["last_run"] = time.time()
                save_state()
            except Exception as e:
                logger.exception("orchestrator main loop error: %s", e)
            await asyncio.sleep(POLL_INTERVAL)

    def stop(self):
        # stop loop by toggling running flag (the worker keeps polling state['running'])
        pass

# ---------------- Flask webhook ----------------
app = Flask(__name__)
orch = Top10Orchestrator()

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
            tg_send("Commands: /top - toggle top10 monitor; /list - show last top; /help")
        elif cmd == "/top":
            # toggle
            state["running"] = not state.get("running", False)
            save_state()
            tg_send(f"Top10 monitor set to {state['running']}.")
        elif cmd == "/list":
            # quick reply using last known (compute small table if msg exists)
            if state.get("msg_id"):
                tg_send("Live panel is active. Use the web UI to view or wait for next update.")
            else:
                tg_send("No live panel yet. Use /top to start.")
        else:
            tg_send("Unknown command. /help")
    except Exception as e:
        logger.exception("webhook cmd exec error: %s", e)
        tg_send("Error handling command.")
    return jsonify({"ok": True})

# health endpoint
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "running": state.get("running", False), "last_run": state.get("last_run")})

# ---------------- BOOT ----------------
if __name__ == "__main__":
    logger.info("Starting Top10 monitor")
    load_state()
    # set webhook if configured
    if TELEGRAM_TOKEN and WEBHOOK_URL:
        try:
            url = WEBHOOK_URL.rstrip("/") + "/webhook"
            r = requests.get(f"{TELEGRAM_API}/setWebhook?url={url}", timeout=10)
            logger.info("Set webhook result: %s", r.text[:200])
        except Exception as e:
            logger.warning("Failed to set webhook: %s", e)

    # start orchestrator (background thread with asyncio loop)
    orch.start()

    # start Flask (blocking) - Render will expose public URL
    # Note: if you want high concurrency, run Flask via a proper WSGI server.
    app.run(host="0.0.0.0", port=PORT)