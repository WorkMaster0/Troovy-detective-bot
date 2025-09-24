# main.py — Dex Flip Bot + Pattern Detector + Flask
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
TOP_LIMIT = 50
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
                data=data, files=files, timeout=10
            )
        else:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": escaped_text, "parse_mode": "MarkdownV2"},
                timeout=10
            )
        if resp.status_code != 200:
            logger.warning("Telegram non-200: %s %s", resp.status_code, resp.text)
    except Exception as e:
        logger.exception("send_telegram error: %s", e)

# ---------------- MARKET DATA ----------------
symbol_dfs = {}
lock = threading.Lock()

def get_symbols_binance():
    try:
        ex = requests.get("https://api.binance.com/api/v3/exchangeInfo", timeout=10).json()
        if "symbols" not in ex:
            return ["BTCUSDT", "ETHUSDT", "BNBUSDT"]
        syms = [s["symbol"] for s in ex["symbols"]
                if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"]
        return syms[:TOP_LIMIT] if syms else ["BTCUSDT", "ETHUSDT", "BNBUSDT"]
    except Exception as e:
        logger.exception("get_symbols_binance error: %s", e)
        return ["BTCUSDT", "ETHUSDT", "BNBUSDT"]

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
            df = df.astype(float)
            return df
    except Exception as e:
        logger.error("load_history error for %s: %s", symbol, e)
    return pd.DataFrame(columns=["open","high","low","close","volume"])

# ---------------- SIGNALS ----------------
def detect_signal(df: pd.DataFrame, symbol=""):
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
        if last["close"] > prev["close"] * (1 + threshold):
            action, votes, conf = "LONG", ["up"], 0.7
        elif last["close"] < prev["close"] * (1 - threshold):
            action, votes, conf = "SHORT", ["down"], 0.7
    return action, votes, last, conf

def plot_signal(df, symbol, action, votes):
    df_plot = df[["open", "high", "low", "close", "volume"]].copy()
    buf = io.BytesIO()
    mpf.plot(
        df_plot, type="candle", volume=True, style="yahoo",
        title=f"{symbol} {action} {' '.join(votes)}",
        savefig=dict(fname=buf, dpi=100, bbox_inches="tight")
    )
    buf.seek(0)
    return buf

# ---------------- PATTERN DETECTOR ----------------
def detect_patterns(df):
    last = df.iloc[-1]
    closes, highs, lows, volumes = df["close"].values, df["high"].values, df["low"].values, df["volume"].values
    signals = []
    if len(df) < 30: return signals
    if (max(highs[-20:]) - min(lows[-20:]))/last["close"] < 0.02: signals.append("Triangle")
    if (max(highs[-30:]) - min(lows[-30:]))/last["close"] < 0.015: signals.append("Rectangle")
    if abs(highs[-5] - highs[-15]) / last["close"] < 0.01: signals.append("Double Top")
    if abs(lows[-5] - lows[-15]) / last["close"] < 0.01: signals.append("Double Bottom")
    if highs[-15] < highs[-10] and highs[-5] < highs[-10]: signals.append("Head & Shoulders")
    if abs(closes[-30] - closes[-1]) / closes[-30] > 0.05 and (max(highs[-10:]) - min(lows[-10:]))/last["close"] < 0.02:
        signals.append("Flag")
    vol_ma = pd.Series(volumes).rolling(20).mean().iloc[-1]
    if last["volume"] > 2 * vol_ma: signals.append("Volume Spike")
    return signals

def plot_chart(df, symbol, pattern_name):
    fig, axlist = mpf.plot(
        df.tail(80), type="candle", style="charles", volume=True,
        returnfig=True, figsize=(10,6), title=f"{symbol} - {pattern_name}"
    )
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    return buf

def run_pattern_scan():
    while True:
        with lock:
            for sym, df in list(symbol_dfs.items())[:10]:
                if len(df) < 100: continue
                try:
                    patterns = detect_patterns(df)
                    for p in patterns:
                        buf = plot_chart(df, sym, p)
                        send_telegram(f"📊 {sym} Pattern: *{p}* price={df['close'].iloc[-1]:.2f}", photo=buf)
                except Exception as e:
                    logger.error("Pattern scan error %s: %s", sym, e)
        time.sleep(300)

# ---------------- WEBSOCKET ----------------
def on_message(ws, msg):
    msg_json = json.loads(msg)
    data = msg_json.get("data", msg_json)
    k, s = data.get("k"), data.get("s")
    if not k: return
    candle_time = pd.to_datetime(k["t"], unit="ms")
    with lock:
        df = symbol_dfs.get(s, pd.DataFrame(columns=["open","high","low","close","volume"]))
        df.loc[candle_time] = [float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k["v"])]
        df = df.tail(EMA_SCAN_LIMIT)
        symbol_dfs[s] = df
    action, votes, last, conf = detect_signal(df, symbol=s)
    if action != "WATCH":
        buf = plot_signal(df, s, action, votes)
        send_telegram(f"⚡ {s} {action} price={last['close']:.6f} conf={conf:.2f}", photo=buf)
        state["signals"][s] = action
        state["last_update"] = str(datetime.now(timezone.utc))
        save_state(STATE_FILE, state)

def on_error(ws, err): logger.error("WS error: %s", err)
def on_close(ws, cs, cm): logger.warning("WS closed, reconnect in 5s"); time.sleep(5); start_ws(list(symbol_dfs.keys()))
def on_open(ws): logger.info("WS connected")

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
                    ws = websocket.WebSocketApp(u, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
                    ws.run_forever(ping_interval=10, ping_timeout=5, ping_payload="keepalive")
                except Exception as e:
                    logger.error("WS fatal: %s", e)
                logger.info("Reconnecting WS in 5s..."); time.sleep(5)
        threading.Thread(target=run_ws, daemon=True).start()

# ---------------- FLASK ----------------
app = Flask(__name__)
@app.route("/")
def home(): return jsonify({"status": "ok", "time": str(datetime.now(timezone.utc))})

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
    threading.Thread(target=run_pattern_scan, daemon=True).start()
    logger.info("Bot started ✅")
    send_telegram("🤖 Bot started with pattern detection!")

if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    start_bot()
    while True: time.sleep(1)