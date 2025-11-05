#!/usr/bin/env python3
# arbitrage_monitor_chart.py
# Demo Arbitrage Monitor with Chart.js
# Works with Python 3.13
#
# Requirements:
#   pip install flask ccxt

import ccxt
import threading
import time
from datetime import datetime
from flask import Flask, render_template_string, jsonify, request

# ---------------- CONFIG ----------------
POLL_INTERVAL = 11.0       # seconds between polling exchanges
HISTORY_MAX_POINTS = 360   # keep last N points per symbol (e.g., ~1 hour with 11s interval)
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "DOGE/USDT"]
HTTP_PORT = int(__import__("os").environ.get("PORT", "5000"))

# ---------------- EXCHANGES ----------------
# using public API (no API keys)
exchanges = {
    "binance": ccxt.binance({"enableRateLimit": True}),
    "bybit": ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "future"}}),
    "mexc": ccxt.mexc({"enableRateLimit": True}),
}

# ---------------- SHARED STATE ----------------
# latest rows (list of dicts)
latest_rows = []
latest_time = None

# history: symbol -> list of {"ts": epoch, "spread": float, "min":float, "max":float}
history = {sym: [] for sym in SYMBOLS}

# lock to protect shared state
state_lock = threading.Lock()

# ---------------- HELPERS ----------------
def now_iso():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def is_valid_price(p):
    try:
        return p is not None and p == p and p > 0 and p < 1e18
    except Exception:
        return False

def fetch_price_safe(exchange, symbol):
    try:
        # some exchanges expect different symbol formatting for futures; try both
        ticker = exchange.fetch_ticker(symbol)
        price = ticker.get("last") or ticker.get("close")
        if is_valid_price(price):
            return float(price)
    except Exception:
        # try alternative symbol e.g., remove slash
        try:
            alt = symbol.replace("/", "")
            ticker = exchange.fetch_ticker(alt)
            price = ticker.get("last") or ticker.get("close")
            if is_valid_price(price):
                return float(price)
        except Exception:
            return None
    return None

# ---------------- MONITOR THREAD ----------------
def monitor_loop():
    global latest_rows, latest_time, history
    print("Monitor thread started, polling every", POLL_INTERVAL, "s")
    while True:
        t0 = time.time()
        gathered = {}
        # fetch tickers from each exchange
        for ex_name, ex in exchanges.items():
            gathered[ex_name] = {}
            for sym in SYMBOLS:
                try:
                    p = fetch_price_safe(ex, sym)
                except Exception as e:
                    p = None
                if is_valid_price(p):
                    gathered[ex_name][sym] = p
                # else leave absent

        rows = []
        ts = int(time.time())
        for sym in SYMBOLS:
            prices = []
            values = {}
            for ex_name in exchanges.keys():
                p = gathered.get(ex_name, {}).get(sym)
                values[ex_name] = p
                if is_valid_price(p):
                    prices.append(p)
            if len(prices) < 2:
                # still include row with available prices but spread=None
                spread = None
                min_p = min(prices) if prices else None
                max_p = max(prices) if prices else None
            else:
                min_p = min(prices)
                max_p = max(prices)
                spread = (max_p - min_p) / min_p * 100.0

                # store history point
                with state_lock:
                    hist = history.get(sym)
                    if hist is None:
                        history[sym] = []
                        hist = history[sym]
                    hist.append({"ts": ts, "spread": round(spread, 6), "min": round(min_p, 8), "max": round(max_p,8)})
                    # trim
                    if len(hist) > HISTORY_MAX_POINTS:
                        del hist[:len(hist) - HISTORY_MAX_POINTS]

            rows.append({
                "symbol": sym,
                "spread": round(spread, 6) if spread is not None else None,
                "min": round(min_p, 8) if min_p is not None else None,
                "max": round(max_p, 8) if max_p is not None else None,
                "prices": values
            })

        with state_lock:
            latest_rows = rows
            latest_time = now_iso()

        # print lightweight log so Render keeps process alive
        print(f"[{latest_time}] polled: rows={len(rows)} (next in {POLL_INTERVAL}s)")

        # sleep remainder
        dt = time.time() - t0
        to_sleep = POLL_INTERVAL - dt
        if to_sleep > 0:
            time.sleep(to_sleep)

# ---------------- FLASK APP ----------------
app = Flask(__name__)

INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Arbitrage Monitor + Chart</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { font-family: Inter, Arial, sans-serif; margin: 18px; background:#0f1724; color:#e6eef8; }
    table { border-collapse: collapse; width: 100%; margin-bottom: 12px;}
    th, td { padding: 8px 10px; border-bottom: 1px solid #223047; text-align: left; }
    th { color:#9fb4d9; }
    tr:hover { background: #122033; cursor: pointer; }
    .small { font-size: 0.9em; color:#9fb4d9; }
    .container { max-width:1200px; margin:0 auto; }
    #chartContainer { background:#071022; padding:10px; border-radius:8px; }
  </style>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body>
  <div class="container">
    <h2>Arbitrage Monitor — Binance / Bybit / MEXC</h2>
    <div class="small">Last update: <span id="lastUpdate">—</span> UTC — Poll every {{poll}}s</div>

    <table id="table">
      <thead>
        <tr><th>Symbol</th><th>Spread %</th><th>Min</th><th>Max</th><th>Binance</th><th>Bybit</th><th>MEXC</th></tr>
      </thead>
      <tbody id="tbody"></tbody>
    </table>

    <div id="chartContainer">
      <canvas id="spreadChart" height="120"></canvas>
    </div>
    <div class="small" style="margin-top:8px;">Click a row to view history for that symbol. Chart auto-updates.</div>
  </div>

<script>
const POLL = {{poll}};
let selectedSymbol = "{{first_symbol}}";
let chart = null;
let chartData = { labels: [], datasets: [{ label: 'Spread %', data: [], fill: true, tension: 0.2 }] };

function buildTable(rows) {
  const tbody = document.getElementById("tbody");
  tbody.innerHTML = "";
  rows.forEach(r => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><strong>${r.symbol}</strong></td>
      <td>${r.spread !== null ? r.spread.toFixed(4) + '%' : '—'}</td>
      <td>${r.min !== null ? r.min.toFixed(6) : '—'}</td>
      <td>${r.max !== null ? r.max.toFixed(6) : '—'}</td>
      <td>${r.prices.binance !== undefined && r.prices.binance !== null ? r.prices.binance.toFixed(6) : '—'}</td>
      <td>${r.prices.bybit  !== undefined && r.prices.bybit  !== null ? r.prices.bybit.toFixed(6)  : '—'}</td>
      <td>${r.prices.mexc   !== undefined && r.prices.mexc   !== null ? r.prices.mexc.toFixed(6)   : '—'}</td>
    `;
    tr.onclick = () => { selectSymbol(r.symbol); };
    tbody.appendChild(tr);
  });
}

function selectSymbol(sym) {
  selectedSymbol = sym;
  loadHistoryAndUpdateChart();
}

async function fetchLatest() {
  try {
    const res = await fetch("/api/latest");
    const j = await res.json();
    document.getElementById("lastUpdate").innerText = j.time || "—";
    buildTable(j.rows || []);
  } catch (e) {
    console.error("fetchLatest:", e);
  }
}

async function fetchHistory(sym) {
  try {
    const res = await fetch("/api/history?symbol=" + encodeURIComponent(sym));
    const j = await res.json();
    return j.history || [];
  } catch (e) {
    console.error("fetchHistory:", e);
    return [];
  }
}

async function loadHistoryAndUpdateChart() {
  if (!selectedSymbol) return;
  const hist = await fetchHistory(selectedSymbol);
  const labels = hist.map(pt => new Date(pt.ts * 1000).toLocaleTimeString());
  const data = hist.map(pt => pt.spread);
  if (!chart) {
    const ctx = document.getElementById('spreadChart').getContext('2d');
    chart = new Chart(ctx, {
      type: 'line',
      data: { labels: labels, datasets: [{ label: selectedSymbol + " — Spread %", data: data, borderWidth:1, backgroundColor: 'rgba(66,165,245,0.1)', borderColor: 'rgba(66,165,245,1)', pointRadius: 0 }] },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          y: { ticks: { color: '#cfe9ff' } },
          x: { ticks: { color: '#cfe9ff' } }
        },
        plugins: { legend: { labels: { color: '#cfe9ff' } } }
      }
    });
  } else {
    chart.data.labels = labels;
    chart.data.datasets[0].label = selectedSymbol + " — Spread %";
    chart.data.datasets[0].data = data;
    chart.update();
  }
}

// main polling loop
async function mainLoop() {
  await fetchLatest();
  await loadHistoryAndUpdateChart();
}

// periodic update
setInterval(async () => {
  await fetchLatest();
  await loadHistoryAndUpdateChart();
}, POLL * 1000);

// initial
window.addEventListener('load', () => {
  mainLoop();
});
</script>
</body>
</html>
"""

@app.route("/")
def index():
    first_symbol = SYMBOLS[0] if SYMBOLS else ""
    return render_template_string(INDEX_HTML, poll=int(POLL_INTERVAL), first_symbol=first_symbol)

@app.route("/api/latest")
def api_latest():
    with state_lock:
        return jsonify({"time": latest_time, "rows": latest_rows})

@app.route("/api/history")
def api_history():
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol or symbol not in history:
        return jsonify({"symbol": symbol, "history": []})
    with state_lock:
        hist = history.get(symbol, [])[-HISTORY_MAX_POINTS:]
        # return as list of points {ts, spread, min, max}
        return jsonify({"symbol": symbol, "history": hist})

# ---------------- BOOT ----------------

def start_background_monitor():
    """Ensure background thread starts even under Gunicorn."""
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()
    print("[INIT] Background monitor thread started.")

# start monitor on import (Gunicorn will call this file as module)
start_background_monitor()

# For local dev run
if __name__ == "__main__":
    print(f"Starting Flask on 0.0.0.0:{HTTP_PORT}")
    app.run(host="0.0.0.0", port=HTTP_PORT, threaded=True)