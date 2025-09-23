import os, json, logging, threading, time, io
import pandas as pd
import requests, websocket
from flask import Flask, jsonify
import mplfinance as mpf
from scipy.signal import find_peaks
import numpy as np
from datetime import datetime, timezone

# --- CONFIG ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
PORT = int(os.getenv("PORT", 10000))
STATE_FILE = "state.json"
TOP_LIMIT = 100
EMA_SCAN_LIMIT = 500

# --- LOGGING ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("dex-flip-bot")

# --- STATE ---
def load_json_safe(path, default):
    try:
        if os.path.exists(path):
            return json.load(open(path, "r"))
    except: pass
    return default

def save_json_safe(path, data):
    with open(path, "w") as f: json.dump(data, f, indent=2)

state = load_json_safe(STATE_FILE, {"signals": {}, "last_update": None})

# --- TELEGRAM ---
def send_telegram(text, photo=None):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        if photo:
            files = {'photo': ('signal.png', photo, 'image/png')}
            data = {'chat_id': CHAT_ID, 'caption': text, 'parse_mode': 'MarkdownV2'}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto", data=data, files=files)
        else:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": text})
    except Exception as e: logger.exception(e)

# --- MARKET DATA ---
symbol_dfs = {}
lock = threading.Lock()

def get_symbols_binance(): 
    try:
        ex = requests.get("https://api.binance.com/api/v3/exchangeInfo").json()
        syms = [s["symbol"] for s in ex["symbols"] if s["quoteAsset"]=="USDT" and s["status"]=="TRADING"]
        return syms[:TOP_LIMIT]
    except: return []

# --- FEATURES & SIGNALS ---
def detect_signal(df):
    last = df.iloc[-1]; votes=[]; conf=0.2
    # EMA example
    if last["close"]>df["close"].rolling(20).mean().iloc[-1]: votes.append("bull"); conf+=0.1
    else: votes.append("bear"); conf+=0.05
    action="LONG" if "bull" in votes else "SHORT"
    return action, votes, last, conf

# --- PLOT ---
def plot_signal(df,symbol,action,votes):
    df_plot=df[['open','high','low','close','volume']]
    buf=io.BytesIO()
    mpf.plot(df_plot, type='candle', savefig=dict(fname=buf,dpi=100,bbox_inches='tight'))
    buf.seek(0)
    return buf

# --- WEBSOCKET ---
def on_message(ws,msg):
    data=json.loads(msg)
    k = data.get("k"); s = data.get("s")
    if not k: return
    candle_closed = k["x"]
    with lock:
        df = symbol_dfs.get(s, pd.DataFrame(columns=["open","high","low","close","volume"]))
        df.loc[pd.to_datetime(k["t"],unit="ms")] = [float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k["v"])]
        df = df.tail(EMA_SCAN_LIMIT); symbol_dfs[s]=df
    if candle_closed:
        action, votes, last, conf = detect_signal(df)
        prev = state["signals"].get(s,"")
        if action != prev:
            buf = plot_signal(df,s,action,votes)
            send_telegram(f"⚡ {s} {action} price={last['close']} conf={conf}", photo=buf)
            state["signals"][s]=action; save_json_safe(STATE_FILE,state)

def start_ws(symbols):
    if not symbols: return
    streams="/".join([f"{s.lower()}@kline_1m" for s in symbols])
    url=f"wss://stream.binance.com:9443/stream?streams={streams}"
    ws=websocket.WebSocketApp(url, on_message=on_message)
    ws.run_forever(ping_interval=20, ping_timeout=10)

# --- FLASK ---
app=Flask(__name__)
@app.route("/")
def home(): return jsonify({"status":"ok"})
def run_flask(): app.run(host="0.0.0.0", port=PORT)

# --- START BOT ---
def start_bot():
    symbols=get_symbols_binance()
    with lock: [symbol_dfs.setdefault(s,pd.DataFrame(columns=["open","high","low","close","volume"])) for s in symbols]
    threading.Thread(target=start_ws,args=(symbols,),daemon=True).start()

if __name__=="__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    start_bot()
    while True: time.sleep(1)