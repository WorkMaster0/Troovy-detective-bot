#!/usr/bin/env python3
import os
import asyncio
import requests
import time
from datetime import datetime
from typing import Dict, List, Optional
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("top-monitor")

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
MEXC_REST = "https://www.mexc.com/api/v3/ticker/24hr"
POLL_INTERVAL = 5  # seconds
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search/?q={q}"
GMGN_API = "https://gmgn.ai/defi/quotation/v1/tokens/search?keyword={q}"
TOP_N = 10

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ---------------- UTIL ----------------
def tg_send(text: str) -> Optional[int]:
    """Send message, return message_id"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return None
    try:
        r = requests.post(
            TELEGRAM_API + "/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
        r.raise_for_status()
        mid = r.json().get("result", {}).get("message_id")
        return mid
    except Exception as e:
        logger.warning("tg_send error: %s", e)
        return None

def tg_edit(message_id: int, text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not message_id:
        return
    try:
        requests.post(
            TELEGRAM_API + "/editMessageText",
            json={"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        logger.warning("tg_edit error: %s", e)

def fetch_from_gmgn(symbol: str) -> Optional[float]:
    try:
        url = GMGN_API.format(q=symbol.upper())
        r = requests.get(url, timeout=7)
        r.raise_for_status()
        data = r.json()
        items = data.get("data") or []
        for it in items:
            price = it.get("price_usd") or it.get("priceUsd") or it.get("price")
            if price:
                return float(price)
    except:
        pass
    return None

def fetch_from_dexscreener(symbol: str) -> Optional[float]:
    try:
        url = DEXSCREENER_SEARCH.format(q=symbol.upper())
        r = requests.get(url, timeout=7)
        r.raise_for_status()
        data = r.json()
        pairs = data.get("pairs") or []
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
    except:
        pass
    return None

def fetch_dex_price(symbol: str) -> Optional[float]:
    price = fetch_from_gmgn(symbol)
    if price is not None:
        return price
    return fetch_from_dexscreener(symbol)

def build_table(pairs: List[Dict]) -> str:
    lines = ["📡 *Top 10 MEXC Movers (1h)*", f"_Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}_", ""]
    lines.append("`SYMBOL    CEX(USD)     DEX(USD)    Δ%`")
    lines.append("`----------------------------------------`")
    for p in pairs:
        symbol = p["symbol"]
        cex = p.get("cex_price")
        dex = p.get("dex_price")
        pct = p.get("pct_change")
        cex_str = f"{cex:.6f}" if cex else "—"
        dex_str = f"{dex:.6f}" if dex else "—"
        pct_str = f"{pct:+6.2f}%" if pct is not None else "—"
        lines.append(f"`{symbol:<8}` {cex_str:>10}  {dex_str:>10}  {pct_str:>6}")
    return "\n".join(lines)

# ---------------- MEXC ----------------
def fetch_mexc_top() -> List[Dict]:
    """Fetch all USDT pairs, compute 1h % change, return top N"""
    try:
        r = requests.get(MEXC_REST, timeout=10)
        r.raise_for_status()
        data = r.json()
        usdt_pairs = [p for p in data if p["symbol"].endswith("USDT")]
        for p in usdt_pairs:
            p["pct_change"] = float(p.get("priceChangePercent1h") or 0.0)
            p["cex_price"] = float(p.get("lastPrice") or 0.0)
        usdt_pairs.sort(key=lambda x: abs(x["pct_change"]), reverse=True)
        return usdt_pairs[:TOP_N]
    except Exception as e:
        logger.warning("fetch_mexc_top error: %s", e)
        return []

# ---------------- MAIN LOOP ----------------
async def main_loop():
    message_id = None
    while True:
        top_pairs = fetch_mexc_top()
        # fetch DEX prices
        for p in top_pairs:
            sym = p["symbol"].replace("USDT", "")
            p["dex_price"] = fetch_dex_price(sym)
        txt = build_table(top_pairs)
        if not message_id:
            message_id = tg_send(txt)
        else:
            tg_edit(message_id, txt)
        await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    logger.info("🚀 Starting top monitor bot...")
    asyncio.run(main_loop())