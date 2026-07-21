"""
MT5 Bridge — the ONLY component that places trades.
v3:
  - Atomic executed_signals lock (prevents duplicate race condition)
  - /close_channel endpoint (close positions + cancel pending per channel)
  - Robust rpyc iteration with EOFError retry (iterate_safe)
  - Per-channel + global trade limits
"""
import os
import time
import logging
import unicodedata
import threading
from datetime import datetime, timezone
from typing import Optional, List, Literal

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field
from mt5linux import MetaTrader5

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bridge")

BRIDGE_TOKEN = os.environ["BRIDGE_TOKEN"]
RISK_PERCENT = float(os.environ.get("RISK_PERCENT", 1.0))
MAX_DAILY_LOSS_PCT = float(os.environ.get("MAX_DAILY_LOSS_PERCENT", 4.0))
MAX_GLOBAL_TRADES = int(os.environ.get("MAX_GLOBAL_TRADES", 25))
MAX_PER_CHANNEL = int(os.environ.get("MAX_PER_CHANNEL_TRADES", 5))
MAGIC = 777001

SYMBOL_MAP = {"GOLD": "XAUUSD", "XAU": "XAUUSD", "DOW": "US30", "NAS": "NAS100"}
PIP_SIZES = {"XAUUSD": 0.10, "US30": 1.0, "NAS100": 1.0}


def sanitize_comment(comment: str) -> str:
    """Normalize Unicode bold/italic chars to plain ASCII for MT5 compatibility."""
    if not comment:
        return comment
    return unicodedata.normalize("NFKC", comment).strip()


app = FastAPI(title="MT5 Bridge")

executed_signals: set[str] = set()
executed_lock = threading.Lock()


class TradeRequest(BaseModel):
    action: Literal["BUY", "SELL"]
    symbol: str
    order_type: Literal["market", "limit", "stop"] = "market"
    entry: Optional[float] = None
    sl: Optional[float] = None
    tp: List[float] = Field(default_factory=list)
    risk_percent: Optional[float] = None
    comment: str = "tg-copier"
    signal_id: str
    from_image: bool = False


class ManageRequest(BaseModel):
    signal_id: str
    op: Literal["close_all", "close_partial", "sl_to_be", "modify_sl"]
    value: Optional[float] = None


class CloseChannelRequest(BaseModel):
    comment: Optional[str] = None
    symbol: Optional[str] = None
    exclude_tickets: list[int] = Field(default_factory=list)


def auth(authorization: str = Header(None)):
    if authorization != f"Bearer {BRIDGE_TOKEN}":
        raise HTTPException(401, "bad token")


def get_mt5(retries: int = 30, delay: int = 2):
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            client = MetaTrader5(host="localhost", port=8001)
            log.info("Connected to mt5linux (attempt %s/%s)", attempt, retries)
            return client
        except Exception as e:
            last_err = e
            log.warning("MT5 RPC not ready yet (attempt %s/%s): %s", attempt, retries, e)
            time.sleep(delay)
    raise HTTPException(503, f"MT5 RPC unavailable: {last_err}")


def ensure_mt5():
    mt5 = get_mt5()
    if not mt5.initialize(
        login=int(os.environ["MT5_LOGIN"]),
        password=os.environ["MT5_PASSWORD"],
        server=os.environ["MT5_SERVER"],
    ):
        raise HTTPException(503, f"MT5 init failed: {mt5.last_error()}")
    return mt5


# ── Safe rpyc iterator (handles EOFError on stale proxy objects) ─
def iterate_safe(mt5_collection, max_retries=3):
    """Safely iterate rpyc proxy objects, retrying on EOFError."""
    result = []
    for attempt in range(1, max_retries + 1):
        try:
            items = list(mt5_collection) if mt5_collection else []
        except (EOFError, OSError, AttributeError) as e:
            log.warning("MT5 collection error: %s — retry %d/%d", e, attempt, max_retries)
            time.sleep(1)
            continue
        result = []
        for item in items:
            try:
                _ = item.ticket  # force RPC attribute access
                result.append(item)
            except (EOFError, AttributeError) as e:
                log.warning("MT5 item error: %s — retry %d/%d", e, attempt, max_retries)
                time.sleep(1)
                break
        else:
            return result
    log.warning("Exhausted retries — returning %d items", len(result))
    return result


def daily_loss_exceeded(mt5_client=None) -> bool:
    mt5 = mt5_client if mt5_client else get_mt5()
    acct = mt5.account_info()
    now = datetime.now(timezone.utc)
    sod = now.replace(hour=0, minute=0, second=0, microsecond=0)
    sod_ts = int(sod.timestamp())
    now_ts = int(now.timestamp())
    deals = mt5.history_deals_get(sod_ts, now_ts) or []
    return sum(d.profit for d in deals) < -(acct.balance * MAX_DAILY_LOSS_PCT / 100)


def calc_lot(symbol: str, entry: float, sl: float, risk_pct: float, mt5_client=None) -> float:
    mt5 = mt5_client if mt5_client else get_mt5()
    acct = mt5.account_info()
    info = mt5.symbol_info(symbol)
    step = max(int(acct.balance / 100), 0)
    lot = 0.01 + step * 0.01
    lot = max(info.volume_min, min(info.volume_max, round(lot / info.volume_step) * info.volume_step))
    return round(lot, 2)


# ── Endpoints ───────────────────────────────────────────────────

@app.get("/health")
def health():
    try:
        mt5 = get_mt5(retries=2, delay=1)
        ok = mt5.initialize(
            login=int(os.environ["MT5_LOGIN"]),
            password=os.environ["MT5_PASSWORD"],
            server=os.environ["MT5_SERVER"],
        )
        ti = mt5.terminal_info()
        acct = mt5.account_info() if ok else None
        return {
            "mt5_connected": bool(ti and ti.connected),
            "balance": acct.balance if acct else None,
        }
    except Exception as e:
        return {"mt5_connected": False, "error": str(e)}


@app.post("/close_channel")
def close_channel(req: CloseChannelRequest, authorization: str = Header(None)):
    """Close ALL open positions + cancel ALL pending orders matching a channel comment."""
    auth(authorization)

    if not req.comment:
        raise HTTPException(400, "comment is required — refusing to close all positions")

    mt5 = ensure_mt5()
    comment = sanitize_comment(req.comment)
    closed = []
    cancelled = []

    # ── 1. Close matching positions (retry to warm rpyc cache) ──
    positions = []
    for attempt in range(3):
        positions = iterate_safe(mt5.positions_get())
        if positions:
            break
        log.warning("positions_get() returned empty (attempt %d/3)", attempt + 1)
        time.sleep(1)

    for p in positions:
        if comment not in sanitize_comment(p.comment or ""):
            continue
        if p.ticket in req.exclude_tickets:
            log.info("Skipping position %s (excluded)", p.ticket)
            continue
        tick = mt5.symbol_info_tick(p.symbol)
        close_type = mt5.ORDER_TYPE_SELL if p.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        close_price = tick.bid if p.type == mt5.POSITION_TYPE_BUY else tick.ask
        res = mt5.order_send({
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": p.symbol,
            "volume": p.volume,
            "position": p.ticket,
            "type": close_type,
            "price": close_price,
            "deviation": 20,
            "magic": MAGIC,
            "type_filling": mt5.ORDER_FILLING_FOK,
        })
        rc = res.retcode if res else -1
        closed.append({"ticket": p.ticket, "symbol": p.symbol, "retcode": rc})
        if rc == mt5.TRADE_RETCODE_DONE:
            log.info("Closed position %s %s -> retcode=%s", p.ticket, p.symbol, rc)
        else:
            log.warning("Failed to close position %s %s -> retcode=%s", p.ticket, p.symbol, rc)

    # ── 2. Cancel matching pending orders (direct MT5 call, no subprocess) ──
    for o in iterate_safe(mt5.orders_get()):
        if comment not in sanitize_comment(o.comment or ""):
            continue
        if o.ticket in req.exclude_tickets:
            log.info("Skipping order %s (excluded)", o.ticket)
            continue
        res = mt5.order_send({
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": o.ticket,
            "symbol": o.symbol,
            "magic": MAGIC,
        })
        rc = res.retcode if res else -1
        cancelled.append({"ticket": o.ticket, "symbol": o.symbol, "retcode": rc})
        if rc == mt5.TRADE_RETCODE_DONE:
            log.info("Cancelled order %s %s -> retcode=%s", o.ticket, o.symbol, rc)
        else:
            log.warning("Failed to cancel order %s %s -> retcode=%s", o.ticket, o.symbol, rc)

    return {"status": "done", "closed": closed, "cancelled": cancelled}


@app.post("/cancel_pending")
def cancel_pending(req: CloseChannelRequest, authorization: str = Header(None)):
    """Legacy endpoint — delegates to close_channel."""
    return close_channel(req, authorization)


@app.post("/trade")
def trade(req: TradeRequest, authorization: str = Header(None)):
    auth(authorization)
    mt5 = ensure_mt5()

    # Atomic duplicate detection (burn on check, un-burn on failure)
    with executed_lock:
        if req.signal_id in executed_signals:
            return {"status": "duplicate_ignored"}
        executed_signals.add(req.signal_id)

    try:
        # --- Validation ---
        if not req.tp or all(t == 0 for t in req.tp):
            raise HTTPException(400, "TP is required — every trade needs a take profit")
        if req.sl is None or req.sl == 0:
            raise HTTPException(400, "SL is required — every trade needs a stop loss")

        # Global trade cap (safety net)
        if len(iterate_safe(mt5.positions_get())) >= MAX_GLOBAL_TRADES:
            raise HTTPException(429, "max global trades reached")

        # Per-channel trade cap
        comment = sanitize_comment(req.comment)
        channel_positions = [
            p for p in iterate_safe(mt5.positions_get())
            if comment in sanitize_comment(p.comment or "")
        ]
        if len(channel_positions) >= MAX_PER_CHANNEL:
            raise HTTPException(429, f"max {MAX_PER_CHANNEL} trades reached for channel '{req.comment}'")

        if daily_loss_exceeded(mt5_client=mt5):
            raise HTTPException(423, "DAILY LOSS CAP HIT — trading disabled today")

        symbol = SYMBOL_MAP.get(req.symbol.upper(), req.symbol.upper())
        if not mt5.symbol_select(symbol, True):
            raise HTTPException(400, f"unknown symbol {symbol}")

        # --- 20-pip rule (bridge decides) ---
        tick = mt5.symbol_info_tick(symbol)
        price = tick.ask if req.action == "BUY" else tick.bid
        entry = req.entry if req.order_type != "market" else price
        pip_size = PIP_SIZES.get(symbol, mt5.symbol_info(symbol).point * 10)
        twenty_pips = 20 * pip_size
        final_type = req.order_type

        if req.entry is not None:
            if abs(req.entry - price) >= twenty_pips:
                entry = req.entry
                if (req.action == "BUY" and req.entry < price) or (req.action == "SELL" and req.entry > price):
                    final_type = "limit"
                    log.info("entry=%.2f is %.1f pips from market=%.2f — LIMIT at %.2f",
                             req.entry, abs(req.entry - price) / pip_size, price, entry)
                else:
                    final_type = "stop"
                    log.info("entry=%.2f is %.1f pips from market=%.2f — STOP at %.2f",
                             req.entry, abs(req.entry - price) / pip_size, price, entry)
            else:
                if req.from_image:
                    entry = req.entry
                    if (req.action == "BUY" and req.entry < price) or (req.action == "SELL" and req.entry > price):
                        final_type = "limit"
                        log.info("entry=%.2f is %.1f pips from market=%.2f — IMAGE LIMIT at %.2f",
                                 req.entry, abs(req.entry - price) / pip_size, price, entry)
                    else:
                        final_type = "stop"
                        log.info("entry=%.2f is %.1f pips from market=%.2f — IMAGE STOP at %.2f",
                                 req.entry, abs(req.entry - price) / pip_size, price, entry)
                else:
                    final_type = "market"
                    entry = price
                    log.info("entry=%.2f is <20 pips from market=%.2f — MARKET at %.2f",
                             req.entry, price, price)

        # --- SL direction check ---
        if (req.action == "BUY" and req.sl >= entry) or (req.action == "SELL" and req.sl <= entry):
            raise HTTPException(400, "SL on wrong side of entry — bad parse or bad signal")

        # --- Stale signal check ---
        if req.order_type == "market" and req.entry and abs(price - req.entry) / price > 0.005:
            raise HTTPException(400, "market moved >0.5% since signal — rejected (stale)")

        # --- Lot sizing ---
        lot = calc_lot(symbol, entry, req.sl, req.risk_percent or RISK_PERCENT, mt5_client=mt5)

        # --- Order execution ---
        tmap = {
            ("BUY", "market"): mt5.ORDER_TYPE_BUY,
            ("SELL", "market"): mt5.ORDER_TYPE_SELL,
            ("BUY", "limit"): mt5.ORDER_TYPE_BUY_LIMIT,
            ("SELL", "limit"): mt5.ORDER_TYPE_SELL_LIMIT,
            ("BUY", "stop"): mt5.ORDER_TYPE_BUY_STOP,
            ("SELL", "stop"): mt5.ORDER_TYPE_SELL_STOP,
        }

        is_market = final_type == "market"
        tps = req.tp or [0.0]
        lot_each = round(max(mt5.symbol_info(symbol).volume_min, lot / len(tps)), 2)

        results = []
        for tp in tps:
            order = {
                "action": mt5.TRADE_ACTION_DEAL if is_market else mt5.TRADE_ACTION_PENDING,
                "symbol": symbol,
                "volume": lot_each,
                "type": tmap[(req.action, final_type)],
                "price": price if is_market else entry,
                "sl": req.sl,
                "tp": tp,
                "deviation": 20,
                "magic": MAGIC,
                "comment": sanitize_comment(req.comment)[:31],
                "type_time": mt5.ORDER_TIME_GTC,
            }
            if is_market:
                order["type_filling"] = mt5.ORDER_FILLING_FOK

            log.info("Sending order to MT5: action=%s type=%s symbol=%s vol=%s price=%s sl=%s tp=%s",
                     order.get("action"), order.get("type"), order.get("symbol"),
                     order.get("volume"), order.get("price"), order.get("sl"), order.get("tp"))

            import subprocess, json as _json
            order_json = _json.dumps(order)
            sp = subprocess.run(
                ["python3", "/app/order_send.py", order_json,
                 os.environ["MT5_LOGIN"], os.environ["MT5_PASSWORD"], os.environ["MT5_SERVER"]],
                capture_output=True, text=True, timeout=30)
            out = sp.stdout.strip()
            log.info("order_send subprocess: %s", out)

            if out.startswith("OK:"):
                parts = out.split(":")
                res = type("obj", (), {"retcode": int(parts[1]), "order": int(parts[2])})()
            else:
                log.error("order_send subprocess failed: %s", sp.stderr[:300])
                res = None

            if res is None:
                log.error("order_send returned None — order=%s", order)
                log.warning("order_send returned None for %s %s lot=%s tp=%s — reconnecting MT5",
                            req.action, symbol, lot_each, tp)
                mt5 = ensure_mt5()
                time.sleep(1.0)
                log.info("Sending order to MT5: action=%s type=%s symbol=%s vol=%s price=%s sl=%s tp=%s (fallback after subprocess None)",
                         order.get("action"), order.get("type"), order.get("symbol"),
                         order.get("volume"), order.get("price"), order.get("sl"), order.get("tp"))
                res = mt5.order_send(order)

            if res is None:
                log.error("order_send returned None again after reconnect for %s %s — aborting", req.action, symbol)
                results.append({"retcode": -1, "order": 0, "tp": tp, "error": "MT5 RPC returned None"})
                continue

            if res.retcode == mt5.TRADE_RETCODE_REQUOTE:
                time.sleep(0.5)
                res = mt5.order_send(order)

            results.append({"retcode": res.retcode, "order": res.order, "tp": tp})
            log.info("%s %s lot=%s tp=%s -> retcode=%s", req.action, symbol, lot_each, tp, res.retcode)

        # --- Result ---
        if all(r["retcode"] == mt5.TRADE_RETCODE_DONE for r in results):
            return {"status": "executed", "symbol": symbol, "lot_each": lot_each, "orders": results}

        raise HTTPException(500, f"partial/failed execution: {results}")

    except Exception:
        # On failure, free the signal_id so it can be retried
        with executed_lock:
            executed_signals.discard(req.signal_id)
        raise


@app.post("/manage")
def manage(req: ManageRequest, authorization: str = Header(None)):
    auth(authorization)
    mt5 = ensure_mt5()
    matched = [p for p in (mt5.positions_get() or []) if req.signal_id in p.comment]
    if not matched:
        raise HTTPException(404, "no open position for that signal_id")
    out = []
    for p in matched:
        if req.op in ("close_all", "close_partial"):
            vol = p.volume if req.op == "close_all" else round(p.volume * (req.value or 50) / 100, 2)
            tick = mt5.symbol_info_tick(p.symbol)
            res = mt5.order_send({
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": p.symbol,
                "volume": vol,
                "position": p.ticket,
                "type": mt5.ORDER_TYPE_SELL if p.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY,
                "price": tick.bid if p.type == mt5.POSITION_TYPE_BUY else tick.ask,
                "deviation": 20,
                "magic": MAGIC,
                "type_filling": mt5.ORDER_FILLING_FOK,
            })
        else:
            new_sl = p.price_open if req.op == "sl_to_be" else req.value
            res = mt5.order_send({
                "action": mt5.TRADE_ACTION_SLTP,
                "position": p.ticket,
                "symbol": p.symbol,
                "sl": new_sl,
                "tp": p.tp,
            })
        out.append({"ticket": p.ticket, "retcode": res.retcode})
    return {"status": "done", "results": out}