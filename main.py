# main.py — Dex Flip Bot + Advanced Analysis (EMA, RSI, MACD, Patterns) + Flask

import os, json, logging, threading, time, io
from datetime import datetime, timezone

import pandas as pd
import numpy as np
import requests
import websocket
import matplotlib.pyplot as plt
import mplfinance as mpf
from flask import Flask, jsonify
from scipy.signal import find_peaks

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
PORT = int(os.getenv("PORT", 10000))
TOP_LIMIT = 30
PATTERN_LIMIT = 10
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

# ---------------- INDICATORS ----------------
def add_indicators(df):
    df["EMA9"] = df["close"].ewm(span=9).mean()
    df["EMA21"] = df["close"].ewm(span=21).mean()
    delta = df["close"].diff()
    gain, loss = delta.clip(lower=0), -delta.clip(upper=0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss
    df["RSI"] = 100 - (100 / (1 + rs))
    short = df["close"].ewm(span=12).mean()
    long = df["close"].ewm(span=26).mean()
    df["MACD"] = short - long
    df["Signal"] = df["MACD"].ewm(span=9).mean()
    return df

# ---------------- FLIP SIGNALS ----------------
def detect_signal(df, symbol=""):
    if len(df) < 30: return "WATCH", [], {}, 0.0
    df = add_indicators(df.copy())
    last = df.iloc[-1]
    signals, conf = [], 0.5
    if last["EMA9"] > last["EMA21"]: signals.append("EMA↑")
    if last["EMA9"] < last["EMA21"]: signals.append("EMA↓")
    if last["RSI"] < 35: signals.append("RSI Oversold")
    if last["RSI"] > 65: signals.append("RSI Overbought")
    if last["MACD"] > last["Signal"]: signals.append("MACD Bull")
    if last["MACD"] < last["Signal"]: signals.append("MACD Bear")
    if "EMA↑" in signals and "MACD Bull" in signals and last["RSI"] < 65:
        return "LONG", signals, last, 0.9
    elif "EMA↓" in signals and "MACD Bear" in signals and last["RSI"] > 35:
        return "SHORT", signals, last, 0.9
    return "WATCH", signals, last, conf

def plot_signal(df, symbol, action, votes):
    df_plot = add_indicators(df.tail(100).copy())
    ap = [
        mpf.make_addplot(df_plot["EMA9"], color="blue"),
        mpf.make_addplot(df_plot["EMA21"], color="orange"),
    ]
    buf = io.BytesIO()
    mpf.plot(df_plot, type="candle", volume=True, style="yahoo",
             addplot=ap, title=f"{symbol} {action} {' '.join(votes)}",
             savefig=dict(fname=buf, dpi=120, bbox_inches="tight"))
    buf.seek(0)
    plt.close("all")
    return buf

# ---------------- PATTERN DETECTION ----------------
def detect_patterns(df):
    if len(df) < 50: return []
    highs, lows, closes = df["high"].values, df["low"].values, df["close"].values
    signals = []
    peaks, _ = find_peaks(highs, distance=5)
    troughs, _ = find_peaks(-lows, distance=5)
    if len(peaks) >= 2 and abs(highs[peaks[-1]] - highs[peaks[-2]])/closes[-1] < 0.01:
        signals.append("Double Top")
    if len(troughs) >= 2 and abs(lows[troughs[-1]] - lows[troughs[-2]])/closes[-1] < 0.01:
        signals.append("Double Bottom")
    if len(peaks) >= 3 and highs[peaks[-2]] > highs[peaks[-3]] and highs[peaks[-2]] > highs[peaks[-1]]:
        signals.append("Head & Shoulders")
    if (max(highs[-20:]) - min(lows[-20:]))/closes[-1] < 0.02:
        signals.append("Triangle")
    return signals

from scipy.signal import find_peaks

def plot_pattern(df, symbol, pattern_name):
    last80 = df.tail(80)
    fig, axlist = mpf.plot(
        last80, type="candle", style="charles", volume=True,
        returnfig=True, figsize=(10,6),
        title=f"{symbol} - {pattern_name} (15m)"
    )
    ax = axlist[0]

    highs = last80["high"].values
    lows = last80["low"].values
    closes = last80["close"].values
    idx = np.arange(len(last80))

    # ---- Пошук піків і впадин ----
    peaks, _ = find_peaks(highs, distance=3)
    bottoms, _ = find_peaks(-lows, distance=3)

    if pattern_name.startswith("Triangle"):
        if len(peaks) >= 2 and len(bottoms) >= 2:
            # Верхня трендова
            x1, x2 = peaks[-2], peaks[-1]
            y1, y2 = highs[x1], highs[x2]
            ax.plot(last80.index[[x1, x2]], [y1, y2], "r--", label="Upper Trend")

            # Нижня трендова
            b1, b2 = bottoms[-2], bottoms[-1]
            ly1, ly2 = lows[b1], lows[b2]
            ax.plot(last80.index[[b1, b2]], [ly1, ly2], "g--", label="Lower Trend")

    elif pattern_name.startswith("Rectangle"):
        high_zone = max(highs[-30:])
        low_zone = min(lows[-30:])
        ax.hlines([high_zone, low_zone], xmin=last80.index[0], xmax=last80.index[-1], 
                  colors=["r","g"], linestyles="--")

    elif pattern_name.startswith("Double Top"):
        if len(peaks) >= 2:
            level = np.mean(highs[peaks[-2:]])
            ax.hlines(level, xmin=last80.index[0], xmax=last80.index[-1], 
                      colors="r", linestyles="--")

    elif pattern_name.startswith("Double Bottom"):
        if len(bottoms) >= 2:
            level = np.mean(lows[bottoms[-2:]])
            ax.hlines(level, xmin=last80.index[0], xmax=last80.index[-1], 
                      colors="g", linestyles="--")

    elif pattern_name.startswith("Head & Shoulders"):
        if len(peaks) >= 3 and len(bottoms) >= 2:
            neckline = np.mean([lows[bottoms[-1]], lows[bottoms[-2]]])
            ax.hlines(neckline, xmin=last80.index[0], xmax=last80.index[-1], 
                      colors="orange", linestyles="--")

    elif pattern_name.startswith("Flag"):
        if len(peaks) >= 2 and len(bottoms) >= 2:
            # верхня межа каналу
            ax.plot(last80.index[[peaks[-2], peaks[-1]]], highs[peaks[-2:]], "b--")
            # нижня межа каналу
            ax.plot(last80.index[[bottoms[-2], bottoms[-1]]], lows[bottoms[-2:]], "b--")

    ax.legend()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    return buf

def pattern_scan_loop():
    while True:
        with lock:
            for sym in list(symbol_dfs.keys())[:PATTERN_LIMIT]:
                df = load_history(sym, limit=300, interval="15m")
                if df.empty: continue
                signals = detect_patterns(df)
                for sig in signals:
                    caption = f"📊 {sym} [15m] Pattern: {sig} | Last Price: {df['close'].iloc[-1]:.2f}"
                    buf = plot_pattern(df, sym, sig)
                    send_telegram(caption, photo=buf)
        time.sleep(600)

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
    send_telegram("🤖 Bot started with Advanced Flip + Pattern detection ✅")

if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    start_bot()
    while True: time.sleep(1)