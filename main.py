# main.py — Dex Flip Bot + Pattern Detection + Flask (Render-optimized)
import os, json, logging, threading, time, io
from datetime import datetime, timezone

import pandas as pd
import numpy as np
import requests
import websocket
import mplfinance as mpf
import matplotlib.pyplot as plt
from flask import Flask, jsonify

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
PORT = int(os.getenv("PORT", 10000))
TOP_LIMIT = 30        # скільки символів моніторимо через WS
PATTERN_LIMIT = 10    # скільки символів скануємо на патерни
EMA_SCAN_LIMIT = 500
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
def escape_markdown_v2(text):
    escape_chars = r'\_*[]()~`>#+-=|{}.!'
    for c in escape_chars:
        text = text.replace(c, f'\\{c}')
    return text

def send_telegram(text, photo=None):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.info("[TELEGRAM MOCK] %s", text)
        return
    try:
        escaped_text = escape_markdown_v2(text)
        if photo:
            files = {"photo": ("signal.png", photo, "image/png")}
            data = {"chat_id": CHAT_ID, "caption": escaped_text, "parse_mode": "MarkdownV2"}
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                data=data, files=files, timeout=20
            )
            photo.close()
        else:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": escaped_text, "parse_mode": "MarkdownV2"},
                timeout=10
            )

        if resp.status_code != 200:
            logger.warning("Telegram returned %s: %s", resp.status_code, resp.text)
    except Exception as e:
        logger.exception("send_telegram error: %s", e)

# ---------------- MARKET DATA ----------------
symbol_dfs = {}
lock = threading.Lock()

def get_symbols_binance():
    try:
        ex = requests.get("https://api.binance.com/api/v3/exchangeInfo", timeout=10).json()
        if "symbols" not in ex:
            return ["BTCUSDT","ETHUSDT","BNBUSDT"]
        syms = [s["symbol"] for s in ex["symbols"]
                if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"]
        return syms[:TOP_LIMIT]
    except:
        return ["BTCUSDT","ETHUSDT","BNBUSDT"]

def load_history(symbol, limit=EMA_SCAN_LIMIT, interval="1m"):
    try:
        url = f"https://api.binance.com/api/v3/klines"
        resp = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10)
        data = resp.json()
        if isinstance(data, list):
            df = pd.DataFrame(data, columns=[
                "time","open","high","low","close","volume","c","q","n","taker_base","taker_quote","ignore"
            ])
            df = df[["time","open","high","low","close","volume"]]
            df["time"] = pd.to_datetime(df["time"], unit="ms")
            df.set_index("time", inplace=True)
            return df.astype(float)
    except Exception as e:
        logger.error("load_history error for %s: %s", symbol, e)
    return pd.DataFrame(columns=["open","high","low","close","volume"])

# ---------------- FLIP SIGNALS ----------------
def detect_signal(df, symbol=""):
    if len(df) < 3:
        return "WATCH", [], df.iloc[-1] if len(df) else {}, 0.0
    last, prev, prev2 = df.iloc[-1], df.iloc[-2], df.iloc[-3]
    conf, action, votes = 0.5, "WATCH", []
    if last["close"] > prev["close"] > prev2["close"]:
        action, votes, conf = "LONG", ["3up"], 0.9
    elif last["close"] < prev["close"] < prev2["close"]:
        action, votes, conf = "SHORT", ["3down"], 0.9
    else:
        threshold = 0.005
        if last["close"] > prev["close"] * (1+threshold):
            action, votes, conf = "LONG", ["up"], 0.7
        elif last["close"] < prev["close"] * (1-threshold):
            action, votes, conf = "SHORT", ["down"], 0.7
    return action, votes, last, conf

def plot_signal(df, symbol, action, votes):
    df_plot = df.tail(80)[["open","high","low","close","volume"]]
    buf = io.BytesIO()
    mpf.plot(
        df_plot, type="candle", volume=True, style="yahoo",
        title=f"{symbol} {action} {' '.join(votes)}",
        savefig=dict(fname=buf, dpi=100, bbox_inches="tight")
    )
    buf.seek(0)
    plt.close("all")
    return buf

# ---------------- PATTERN DETECTION ----------------
def detect_patterns(df):
    if len(df) < 30: return []
    last = df.iloc[-1]
    highs, lows, closes, vols = df["high"].values, df["low"].values, df["close"].values, df["volume"].values
    signals = []
    if (max(highs[-20:]) - min(lows[-20:]))/last["close"] < 0.02:
        signals.append("Triangle")
    if (max(highs[-30:]) - min(lows[-30:]))/last["close"] < 0.015:
        signals.append("Rectangle")
    if abs(highs[-5] - highs[-15]) / last["close"] < 0.01:
        signals.append("Double Top")
    if abs(lows[-5] - lows[-15]) / last["close"] < 0.01:
        signals.append("Double Bottom")
    if highs[-15] < highs[-10] and highs[-5] < highs[-10]:
        signals.append("Head & Shoulders")
    if abs(closes[-30] - closes[-1])/closes[-30] > 0.05 and (max(highs[-10:]) - min(lows[-10:]))/last["close"] < 0.02:
        signals.append("Flag")
    vol_ma = pd.Series(vols).rolling(20).mean().iloc[-1]
    if last["volume"] > 2*vol_ma:
        signals.append("Volume Spike")
    return signals

def plot_pattern(df, symbol, pattern_name):
    last80 = df.tail(80)
    fig, axlist = mpf.plot(
        last80, type="candle", style="charles", volume=True,
        returnfig=True, figsize=(10,6),
        title=f"{symbol} - {pattern_name}"
    )
    ax = axlist[0]
    highs, lows, closes = last80["high"].values, last80["low"].values, last80["close"].values
    if pattern_name == "Triangle":
        ax.plot(last80.index, np.linspace(max(highs), min(highs[-10:]), len(last80)), "r--")
        ax.plot(last80.index, np.linspace(min(lows), max(lows[-10:]), len(last80)), "g--")
    elif pattern_name == "Rectangle":
        ax.hlines([max(highs[-30:]), min(lows[-30:])], xmin=last80.index[0], xmax=last80.index[-1], colors=["r","g"], linestyles="--")
    elif pattern_name == "Double Top":
        ax.hlines(max(highs[-10:]), xmin=last80.index[0], xmax=last80.index[-1], colors="r", linestyles="--")
    elif pattern_name == "Double Bottom":
        ax.hlines(min(lows[-10:]), xmin=last80.index[0], xmax=last80.index[-1], colors="g", linestyles="--")
    elif pattern_name == "Head & Shoulders":
        neckline = (lows[-20] + lows[-10]) / 2
        ax.hlines(neckline, xmin=last80.index[0], xmax=last80.index[-1], colors="orange", linestyles="--")
    elif pattern_name == "Flag":
        mid = np.mean(closes[-20:])
        ax.plot(last80.index, np.linspace(mid*0.98, mid*1.02, len(last80)), "b--")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    return buf

def pattern_scan_loop():
    while True:
        with lock:
            for sym in list(symbol_dfs.keys())[:PATTERN_LIMIT]:
                # ✅ Завантажуємо історію 15m
                df = load_history(sym, limit=200, interval="15m")
                if df.empty: 
                    continue
                signals = detect_patterns(df)
                for sig in signals:
                    caption = f"📊 {sym} [15m] Pattern: {sig} | Last Price: {df['close'].iloc[-1]:.2f}"
                    buf = plot_pattern(df, sym, f"{sig} (15m)")
                    send_telegram(caption, photo=buf)
        time.sleep(600)  # раз на 10 хв

# ---------------- WEBSOCKET ----------------
def on_message(ws, msg):
    msg_json = json.loads(msg)
    data = msg_json["data"] if "data" in msg_json else msg_json
    k, s = data.get("k"), data.get("s")
    if not k: return
    candle_time = pd.to_datetime(k["t"], unit="ms")
    with lock:
        df = symbol_dfs.get(s, pd.DataFrame(columns=["open","high","low","close","volume"]))
        df.loc[candle_time] = [float(k["o"]),float(k["h"]),float(k["l"]),float(k["c"]),float(k["v"])]
        df = df.tail(EMA_SCAN_LIMIT)
        symbol_dfs[s] = df
    action, votes, last, conf = detect_signal(df, symbol=s)
    if action != "WATCH":
        buf = plot_signal(df, s, action, votes)
        send_telegram(f"⚡ {s} {action} price={last['close']:.6f} conf={conf:.2f}", photo=buf)
        state["signals"][s] = action
        state["last_update"] = str(datetime.now(timezone.utc))
        save_state(STATE_FILE, state)

def on_error(ws, err): logger.error("WebSocket error: %s", err)
def on_close(ws, cs, cm):
    logger.warning("WS closed, reconnect...")
    time.sleep(5); start_ws(list(symbol_dfs.keys()))
def on_open(ws): logger.info("WebSocket connected")
def on_pong(ws, msg): logger.info("PONG received")

def start_ws(symbols):
    if not symbols: return
    chunk_size = 30
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i+chunk_size]
        streams = "/".join([f"{s.lower()}@kline_1m" for s in chunk])
        url = f"wss://stream.binance.com:9443/stream?streams={streams}"
        def run_ws(u=url):
            while True:
                try:
                    ws = websocket.WebSocketApp(
                        u,on_open=on_open,on_message=on_message,
                        on_error=on_error,on_close=on_close,on_pong=on_pong
                    )
                    ws.run_forever(ping_interval=10,ping_timeout=5,ping_payload="keepalive")
                except Exception as e:
                    logger.error("WS fatal error: %s", e)
                time.sleep(5)
        threading.Thread(target=run_ws, daemon=True).start()

# ---------------- FLASK ----------------
app = Flask(__name__)
@app.route("/")
def home(): return jsonify({"status":"ok","time":str(datetime.now(timezone.utc))})

# ---------------- START BOT ----------------
def start_bot():
    symbols = get_symbols_binance()
    logger.info("Symbols loaded: %s", symbols)
    with lock:
        for s in symbols:
            df = load_history(s)
            symbol_dfs[s] = df
            logger.info("History loaded: %s rows=%s", s, len(df))
    start_ws(symbols)
    threading.Thread(target=pattern_scan_loop, daemon=True).start()
    send_telegram("🤖 Bot started with Flip + Pattern detection ✅")

if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    start_bot()
    while True: time.sleep(1)