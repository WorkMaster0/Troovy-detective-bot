#!/usr/bin/env python3
"""
Futures spread watcher (ccxt.pro) — моніторить USDT perpetual (swap) пари
на множині бірж через websockets і відправляє сигнали в Telegram, якщо
знайдено валідний спред (але ігнорує спреди >100%).
"""

import os
import asyncio
import time
import json
import logging
from datetime import datetime, timezone
from collections import defaultdict

import requests
import pandas as pd

# зовнішні: ccxt, ccxt.pro
try:
    import ccxt
except Exception as e:
    raise RuntimeError("ccxt required. pip install ccxt") from e

try:
    import ccxt.pro as ccxtpro
except Exception:
    ccxtpro = None  # ми перевіримо пізніше і дамо зрозуміле повідомлення

# --- LOGGING ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("futures-spread-bot")

# --- CONFIG (env) ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
# біржі, які будемо намагатись моніторити (ccxt ids)
EXCHANGES_TO_TRY = os.getenv("EXCHANGES", "gate,mexc,lbank,hotbit").split(",")  # можна змінити через env
MIN_SPREAD_PCT = float(os.getenv("MIN_SPREAD_PCT", "0.5"))  # мінімум 0.5% spread для сигналу
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "100.0"))  # ігнорувати >100%
RECONNECT_DELAY = int(os.getenv("RECONNECT_DELAY", "5"))  # backoff
WS_TIMEOUT = int(os.getenv("WS_TIMEOUT", "30"))  # таймаут між тиками, не критично
PRICE_SAFETY_MIN = 1e-8  # мінімальна ціна, нижче якої вважаємо аномалією

# Telegram helper
MARKDOWNV2_ESCAPE = r"_*[]()~`>#+-=|{}.!"
import re
def escape_md_v2(text: str) -> str:
    return re.sub(f"([{re.escape(MARKDOWNV2_ESCAPE)}])", r"\\\1", str(text))

def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.debug("Telegram not configured -> skip send")
        return
    try:
        payload = {"chat_id": CHAT_ID, "text": escape_md_v2(text), "parse_mode": "MarkdownV2"}
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=10)
        if not r.ok:
            logger.warning("Telegram send failed: %s %s", r.status_code, r.text)
    except Exception as ex:
        logger.exception("send_telegram error: %s", ex)

# --- utility: human timestamp
def now_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

# --- Step 1: initial market discovery via ccxt (sync) ---
def discover_futures_markets(exchange_ids):
    """
    Викликаємо ccxt REST fetch_markets для кожної біржі і збираємо futures/swap
    пари з USDT (підозрюємо, що їх маркування міститиме 'USDT').
    Повертаємо dict: exchange_id -> set(symbols)
    """
    logger.info("Discovering markets via REST for exchanges: %s", exchange_ids)
    exchange_symbols = {}
    for ex_id in exchange_ids:
        try:
            logger.info("Init REST client for %s", ex_id)
            ex = getattr(ccxt, ex_id)()
            # fetch markets
            markets = ex.load_markets(True)
            symbols = set()
            for mkt in markets.values():
                sym = mkt.get('symbol')
                if not sym:
                    continue
                # Filter: must contain USDT and be a contract / swap / future market
                mtype = mkt.get('type', '') or ''
                info = mkt.get('info') or {}
                is_contract = False
                # heuristics
                if 'swap' in mtype or 'future' in mtype or mkt.get('contract') or info.get('contractType') or info.get('contract') == True:
                    is_contract = True
                # Some exchanges have 'linear' flag or 'spot' flag -> skip spot
                if 'USDT' in sym and is_contract:
                    symbols.add(sym)
            logger.info("  %s -> %d futures symbols", ex_id, len(symbols))
            exchange_symbols[ex_id] = symbols
        except Exception as e:
            logger.exception("Failed to discover markets for %s: %s", ex_id, e)
            exchange_symbols[ex_id] = set()
    return exchange_symbols

# --- Step 2: compute cross-exchange common symbols list ---
def find_common_symbols(exchange_symbols):
    """
    Знайдемо всі символи, які присутні принаймні на двох біржах,
    оскільки для арбітражу потрібно порівняння.
    Повертаємо sorted list.
    """
    cnt = {}
    for ex, syms in exchange_symbols.items():
        for s in syms:
            cnt[s] = cnt.get(s, 0) + 1
    common = [s for s, c in cnt.items() if c >= 2]
    logger.info("Found %d symbols that exist on >=2 exchanges", len(common))
    return common

# --- Step 3: create async ccxt.pro clients (one per exchange) ---
async def create_pro_client(exchange_id):
    """
    Створюємо ccxt.pro клієнт для exchange_id.
    Якщо ccxt.pro не встановлено або exchange не підтримується — повертаємо None.
    """
    if ccxtpro is None:
        logger.error("ccxt.pro not available. Install it to use websockets.")
        return None
    try:
        logger.info("Creating ws client for %s", exchange_id)
        ex_class = getattr(ccxtpro, exchange_id, None)
        if ex_class is None:
            logger.warning("ccxt.pro has no exchange class for id '%s'", exchange_id)
            return None
        ex = ex_class({
            # деякі біржі вимагають 'enableRateLimit': True для стабільності
            "enableRateLimit": True,
            "timeout": 20000,
        })
        # спробуємо load_markets асинхронно
        await ex.load_markets()
        logger.info("Initialized exchange client: %s", exchange_id)
        return ex
    except Exception as e:
        logger.exception("Failed to init exchange client %s: %s", exchange_id, e)
        return None

# --- helper: normalize symbol names between exchanges ---
def normalize_symbol(symbol):
    # ccxt symbols are often unified, leave as-is
    return symbol.replace(":", "/")  # some have ':' suffix for inverse contracts

# --- Core: watch loops & comparison ---
async def watch_symbol_across_exchanges(symbol, clients_map, last_prices_cache):
    """
    Для певного символу підписуємось (через clients_map) на watch_ticker.
    Але щоб не створювати окремий корутин на кожну пару+exchange доволі тяжко —
    логіка: кожен exchange клієнт має свій корутин, що слухає watch_ticker по багатьох парах.
    Тому ця функція не використовується напряму. (залишаю для розширення)
    """
    pass  # main logic реалізовано в per-exchange watcher нижче

async def exchange_ticker_watcher(exchange_id, ex_client, symbols_on_exchange, shared_book):
    """
    На одному exchange слухаємо watch_ticker для усіх symbols_on_exchange (список).
    Прихід кожного тікера зберігаємо в shared_book: shared_book[symbol][exchange_id] = {'bid':..., 'ask':..., 'ts':...}
    Потім для цього symbol виконуємо пошук найкращих bid/ask across other exchanges і рахуємо spread.
    """
    logger.info("Starting watcher for exchange %s (symbols: %d)", exchange_id, len(symbols_on_exchange))
    # chunk subscribes to avoid overloading ws — будемо підписуватись по черзі
    symbols = list(symbols_on_exchange)
    for sym in symbols:
        # async subscribe with retry
        mapped = sym
        tries = 0
        while True:
            try:
                # watch_ticker returns a dict like { 'symbol':..., 'bid':..., 'ask':... }
                ticker = await ex_client.watch_ticker(mapped, params={})
                bid = ticker.get('bid') or ticker.get('bestBid') or 0.0
                ask = ticker.get('ask') or ticker.get('bestAsk') or 0.0
                ts = ticker.get('timestamp') or ticker.get('datetime') or datetime.now(timezone.utc).timestamp()
                # safe-cast
                try:
                    bid = float(bid) if bid is not None else 0.0
                    ask = float(ask) if ask is not None else 0.0
                except Exception:
                    bid = 0.0; ask = 0.0

                # store
                shared_book.setdefault(sym, {})[exchange_id] = {"bid": bid, "ask": ask, "ts": time.time()}

                # compare with other exchanges present for this symbol
                await analyze_spreads_for_symbol(sym, shared_book)

                # small sleep to yield control
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                logger.info("watcher cancelled for %s @ %s", sym, exchange_id)
                return
            except Exception as e:
                # often websocket will close; attempt backoff
                tries += 1
                logger.debug("watch_ticker exception for %s on %s (try %d): %s", sym, exchange_id, tries, e)
                await asyncio.sleep(min(10 + tries, 60))
                # try to reload markets or reconnect if needed
                try:
                    await ex_client.load_markets()
                except Exception:
                    pass
                continue

async def analyze_spreads_for_symbol(symbol, shared_book):
    """
    Для символу беремо всі біржі з даними, знаходимо найменший ask (купити)
    і найбільший bid (продати). Рахуємо spread.
    Якщо spread_pct в межах (MIN_SPREAD_PCT, MAX_SPREAD_PCT) — надсилаємо повідомлення.
    Також перевіряємо, що ціни не аномально малі.
    Використовуємо simple dedup: зберігаємо останній відправлений spread для символу і пар (buy_ex/sell_ex)
    і не спамимо повторно за короткий інтервал.
    """
    data = shared_book.get(symbol, {})
    if not data or len(data) < 2:
        return
    # build list of (exchange, bid, ask)
    rows = []
    for ex, v in data.items():
        bid = v.get("bid", 0.0) or 0.0
        ask = v.get("ask", 0.0) or 0.0
        rows.append((ex, bid, ask))
    # find best ask (min) and best bid (max)
    best_ask_ex, best_ask = None, float('inf')
    best_bid_ex, best_bid = None, 0.0
    for ex, bid, ask in rows:
        if ask and ask > PRICE_SAFETY_MIN and ask < best_ask:
            best_ask = ask; best_ask_ex = ex
        if bid and bid > PRICE_SAFETY_MIN and bid > best_bid:
            best_bid = bid; best_bid_ex = ex
    if best_ask_ex is None or best_bid_ex is None:
        return
    # ignore if same exchange
    if best_ask_ex == best_bid_ex:
        return
    # compute spread pct = (best_bid / best_ask - 1) * 100
    try:
        spread_ratio = best_bid / best_ask if best_ask > 0 else 0.0
        spread_pct = (spread_ratio - 1.0) * 100.0
    except Exception:
        return
    # sanity filters
    if spread_pct <= MIN_SPREAD_PCT:
        return
    if spread_pct > MAX_SPREAD_PCT:
        # ignore crazy numbers
        logger.debug("Ignoring %s spread %.2f%% (>%s%%) between %s/%s", symbol, spread_pct, MAX_SPREAD_PCT, best_bid_ex, best_ask_ex)
        return
    # Dedup: do not resend same pair+symbol too frequently
    dedup_key = f"{symbol}|{best_ask_ex}|{best_bid_ex}"
    now_ts = time.time()
    last_sent = analyze_spreads_for_symbol._last_sent.get(dedup_key, 0)
    # resend only if > 120s since last alert for same arrangement (configurable)
    if now_ts - last_sent < 120:
        return
    analyze_spreads_for_symbol._last_sent[dedup_key] = now_ts

    # Build human-friendly message
    msg = (
        f"🔔 *Spread Alert via WS*\n"
        f"Symbol: `{symbol}`\n"
        f"Buy (ask): `{best_ask:.8f}` @ *{best_ask_ex}*\n"
        f"Sell (bid): `{best_bid:.8f}` @ *{best_bid_ex}*\n"
        f"Spread: *{spread_pct:.2f}%* ({best_bid - best_ask:.8f})\n"
        f"Time: {now_utc_str()}\n"
    )
    logger.info("Spread found %s: ask %s@%s bid %s@%s => %.2f%%", symbol, best_ask, best_ask_ex, best_bid, best_bid_ex, spread_pct)
    send_telegram(msg)

# attach state
analyze_spreads_for_symbol._last_sent = {}

# --- Main runner ---
async def run_ws_monitor():
    # 1) discover REST markets for all exchanges
    exchange_ids = [e.strip() for e in EXCHANGES_TO_TRY if e.strip()]
    rest_markets = discover_futures_markets(exchange_ids)
    # 2) find common symbols (>=2 exch) — we'll monitor only them (to be efficient)
    common_symbols = find_common_symbols(rest_markets)
    if not common_symbols:
        logger.warning("No common futures symbols found across exchanges. Exiting.")
        return

    # 3) prepare mapping exchange -> symbols present there (intersection)
    exch_to_symbols = {}
    for ex in exchange_ids:
        # keep only symbols that are in common_symbols
        syms = rest_markets.get(ex, set())
        use = sorted([s for s in syms if s in common_symbols])
        exch_to_symbols[ex] = use
        logger.info("%s -> subscribing to %d common symbols", ex, len(use))

    # 4) create ccxt.pro clients async
    pro_clients = {}
    for ex in exchange_ids:
        client = await create_pro_client(ex)
        if client is None:
            logger.warning("Skipping exchange %s (no ws client)", ex)
            continue
        pro_clients[ex] = client

    if not pro_clients:
        logger.error("No websocket clients available (ccxt.pro missing or all failed). Exiting.")
        return

    # 5) spin watchers per exchange
    shared_book = {}  # symbol -> exchange -> {bid,ask,ts}
    watchers = []
    for ex_id, client in pro_clients.items():
        symbols = exch_to_symbols.get(ex_id, [])
        if not symbols:
            continue
        # create an independent task
        task = asyncio.create_task(exchange_ticker_watcher(ex_id, client, symbols, shared_book))
        watchers.append(task)

    # 6) join tasks (they run forever); handle cancellation
    try:
        await asyncio.gather(*watchers)
    except asyncio.CancelledError:
        logger.info("Run cancelled, closing clients")
    except Exception as e:
        logger.exception("run_ws_monitor top-level error: %s", e)
    finally:
        # close clients
        for cli in pro_clients.values():
            try:
                await cli.close()
            except Exception:
                pass

# --- entrypoint ---
def main():
    logger.info("Starting futures-spread-bot (ccxt.pro event-based). Exchanges: %s", EXCHANGES_TO_TRY)
    if ccxtpro is None:
        logger.error("ccxt.pro is not installed. Install ccxt.pro to use websocket monitoring.")
        return

    # run asyncio loop
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(run_ws_monitor())
    finally:
        # ensure loop closed gracefully
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()

if __name__ == "__main__":
    main()