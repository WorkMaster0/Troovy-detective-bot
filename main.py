# main.py — Dex Flip Bot + Flask для Render
import os, json, logging, threading, time, io
from datetime import datetime, timezone

import pandas as pd
import numpy as np
import requests
import websocket
import mplfinance as mpf
from scipy.signal import find_peaks
from flask import Flask, jsonify

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
PORT = int(os.getenv("PORT", 10000))
TOP_LIMIT = 50
EMA_SCAN_LIMIT = 500
CONF_THRESHOLD = 0.5
STATE_FILE = "state.json"

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("dex-flip-bot")

# ---------------- STATE ----------------
def load_state(path, default):
    try:
        if os.path.exists(path):
            return json.load(open(path))
    except:
        pass
    return default

def save_state(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

state = load_state(STATE_FILE, {"signals": {}, "last_update": None})

# ---------------- TELEGRAM ----------------
def send_telegram(text, photo=None):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.info("Telegram token or chat_id missing, printing instead:\n%s", text)
        return
    try:
        if photo:
            files = {'photo': ('signal.png', photo, 'image/png')}
            data = {'chat_id': CHAT_ID, 'caption': text, 'parse_mode': 'MarkdownV2'}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto", data=data, files=files, timeout=10)
        else:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          json={"chat_id": CHAT_ID, "text": text, "parse_mode":"MarkdownV2"}, timeout=10)
    except Exception as e:
        logger.exception("send_telegram error: %s", e)

# ---------------- MARKET DATA ----------------
symbol_dfs = {}
lock = threading.Lock()

def get_symbols_binance():
    try:
        ex = requests.get("https://api.binance.com/api/v3/exchangeInfo", timeout=10).json()
        syms = [s["symbol"] for s in ex["symbols"] if s["quoteAsset"]=="USDT" and s["status"]=="TRADING"]
        return syms[:TOP_LIMIT]
    except Exception as e:
        logger.exception("get_symbols_binance error: %s", e)
        return []

# ---------------- SIGNALS ----------------
def detect_signal(df: pd.DataFrame):
    if len(df) < 2: return "WATCH", [], df.iloc[-1], 0.0
    last = df.iloc[-1]; votes = []; conf = 0.2
    # EMA strategy
    ema20 = df["close"].rolling(20).mean().iloc[-1]
    if last["close"] > ema20: votes.append("bull"); conf += 0.1
    else: votes.append("bear"); conf += 0.05
    # simple pre-top detection
    pretop = False
    if len(df)>=10 and (last["close"]-df["close"].iloc[-10])/df["close"].iloc[-10]>0.1:
        pretop=True; votes.append("pretop"); conf += 0.1
    action = "LONG" if "bull" in votes else "SHORT"
    conf = min(1.0, conf)
    return action, votes, last, conf

# ---------------- PLOT ----------------
def plot_signal(df, symbol, action, votes):
    df_plot = df[['open','high','low','close','volume']].copy()
    buf = io.BytesIO()
    mpf.plot(df_plot, type='candle', volume=True, style='yahoo',
             title=f"{symbol} {action} {' '.join(votes)}",
             savefig=dict(fname=buf, dpi=100, bbox_inches='tight'))
    buf.seek(0)
    return buf

# ---------------- WEBSOCKET ----------------
def on_message(ws, msg):
    data = json.loads(msg)
    k = data.get("k"); s = data.get("s")
    if not k: return
    candle_closed = k["x"]
    with lock:
        df = symbol_dfs.get(s, pd.DataFrame(columns=["open","high","low","close","volume"]))
        df.loc[pd.to_datetime(k["t"], unit="ms")] = [float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k["v"])]
        df = df.tail(EMA_SCAN_LIMIT); symbol_dfs[s] = df
    # send signal
    action, votes, last, conf = detect_signal(df)
    prev = state["signals"].get(s, "")
    if action != "WATCH" and (action != prev or candle_closed):
        buf = plot_signal(df, s, action, votes)
        send_telegram(f"⚡ {s} {action} price={last['close']:.6f} conf={conf:.2f}", photo=buf)
        state["signals"][s] = action; state["last_update"]=str(datetime.now(timezone.utc))
        save_state(STATE_FILE, state)

def on_error(ws, err): logger.error("WebSocket error: %s", err)
def on_close(ws, cs, cm): logger.warning("WS closed, reconnect in 5s"); time.sleep(5); start_ws(list(symbol_dfs.keys()))
def on_open(ws): logger.info("WebSocket connected")

def start_ws(symbols):
    if not symbols: return
    streams = "/".join([f"{s.lower()}@kline_1m" for s in symbols])
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"
    ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
    ws.run_forever(ping_interval=20, ping_timeout=10)

# ---------------- FLASK ----------------
app = Flask(__name__)
@app.route("/")
def home(): return jsonify({"status":"ok", "time":str(datetime.now(timezone.utc))})

# ---------------- START BOT ----------------
def start_bot():
    symbols = get_symbols_binance()
    with lock:
        for s in symbols: symbol_dfs.setdefault(s, pd.DataFrame(columns=["open","high","low","close","volume"]))
    threading.Thread(target=start_ws, args=(symbols,), daemon=True).start()

if __name__=="__main__":
    # Flask для Render
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    # WebSocket бот
    start_bot()  # нескінченний цикл