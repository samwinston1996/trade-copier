"""
MT5 Bridge — the ONLY component that places trades.
Endpoints:
  GET  /health   -> is MT5 connected to the broker?
  POST /trade    -> open a position (with risk gates + lot sizing)
  POST /manage   -> close/partial-close/move-SL on an existing position

Risk gates are deterministic Python. Hermes/the LLM cannot bypass them.
"""
import os
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Literal

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field
from mt5linux import MetaTrader5

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bridge")

BRIDGE_TOKEN = os.environ["BRIDGE_TOKEN"]
RISK_PERCENT = float(os.environ.get("RISK_PERCENT", 1.0))
MAX_DAILY_LOSS_PCT = float(os.environ.get("MAX_DAILY_LOSS_PERCENT", 4.0))
MAX_OPEN_TRADES = int(os.environ.get("MAX_OPEN_TRADES", 5))
MAGIC = 777001

SYMBOL_MAP = {"GOLD": "XAUUSD", "XAU": "XAUUSD", "DOW": "US30", "NAS": "NAS100"}

app = FastAPI(title="MT5 Bridge")
executed_signals: set[str] = set()

_mt5 = None


class TradeRequest(BaseModel):
    action: Literal["BUY", "SELL"]
    symbol: str
    order_type: Literal["market", "limit", "stop"] = "market"
    entry: Optional[float] = None
    sl: float
    tp: List[float] = Field(default_factory=list)
    risk_percent: Optional[float] = None
    comment: str = "tg-copier"
    signal_id: str


class ManageRequest(BaseModel):
    signal_id: str
    op: Literal["close_all", "close_partial", "sl_to_be", "modify_sl"]
    value: Optional[float] = None


def auth(authorization: Optional[str]):
    if authorization != f"Bearer {BRIDGE_TOKEN}":
        raise HTTPException(401, "bad token")


def get_mt5(retries: int = 30, delay: int = 2):
    """
    Lazy-connect to mt5linux server on localhost:8001.
    This avoids crashing at import time if MT5/Wine is still booting.
    """
    global _mt5
    if _mt5 is not None:
        return _mt5

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            client = MetaTrader5(host="localhost", port=8001)
            _mt5 = client
            log.info("Connected to mt5linux on localhost:8001 (attempt %s/%s)", attempt, retries)
            return _mt5
        except Exception as e:
            last_err = e
            log.warning("MT5 RPC not ready yet (attempt %s/%s): %s", attempt, retries, e)
            time.sleep(delay)

    raise HTTPException(503, f"MT5 RPC unavailable on localhost:8001 after retries: {last_err}")


def ensure_mt5():
    mt5 = get_mt5()
    if not mt5.initialize(
        login=int(os.environ["MT5_LOGIN"]),
        password=os.environ["MT5_PASSWORD"],
        server=os.environ["MT5_SERVER"],
    ):
        raise HTTPException(503, f"MT5 init failed: {mt5.last_error()}")
    return mt5


def daily_loss_exceeded() -> bool:
    mt5 = get_mt5()
    acct = mt5.account_info()
    now = datetime.now(timezone.utc)
    sod = now.replace(hour=0, minute=0, second=0, microsecond=0)
    sod_ts = int(sod.timestamp())
    now_ts = int(now.timestamp())
    deals = mt5.history_deals_get(sod_ts, now_ts) or []
    return sum(d.profit for d in deals) < -(acct.balance * MAX_DAILY_LOSS_PCT / 100)

def calc_lot(symbol: str, entry: float, sl: float, risk_pct: float) -> float:
    """Step-based lot sizing: balance/100 → lots, min 0.01."""
    mt5 = get_mt5()
    acct = mt5.account_info()
    info = mt5.symbol_info(symbol)
    step = max(int(acct.balance / 100), 0)
    lot = 0.01 + step * 0.01
    lot = max(info.volume_min, min(info.volume_max, round(lot / info.volume_step) * info.volume_step))
    return round(lot, 2)


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


@app.post("/trade")
def trade(req: TradeRequest, authorization: str = Header(None)):
    auth(authorization)
    mt5 = ensure_mt5()

    if req.signal_id in executed_signals:
        return {"status": "duplicate_ignored"}

    if not req.tp or all(t == 0 for t in req.tp):
        raise HTTPException(400, "TP is required — every trade needs a take profit")

    if req.sl is None or req.sl == 0:
        raise HTTPException(400, "SL is required — every trade needs a stop loss")

    if daily_loss_exceeded():
        raise HTTPException(423, "DAILY LOSS CAP HIT — trading disabled today")

    if len(mt5.positions_get() or []) >= MAX_OPEN_TRADES:
        raise HTTPException(429, "max open trades reached")

    symbol = SYMBOL_MAP.get(req.symbol.upper(), req.symbol.upper())
    if not mt5.symbol_select(symbol, True):
        raise HTTPException(400, f"unknown symbol {symbol}")

    tick = mt5.symbol_info_tick(symbol)
    price = tick.ask if req.action == "BUY" else tick.bid
    entry = req.entry if req.order_type != "market" else price

    if (req.action == "BUY" and req.sl >= entry) or (req.action == "SELL" and req.sl <= entry):
        raise HTTPException(400, "SL on wrong side of entry — bad parse or bad signal")

    if req.order_type == "market" and req.entry and abs(price - req.entry) / price > 0.005:
        raise HTTPException(400, "market moved >0.5% since signal — rejected (stale)")

    lot = calc_lot(symbol, entry, req.sl, req.risk_percent or RISK_PERCENT)

    tmap = {
        ("BUY", "market"): mt5.ORDER_TYPE_BUY,
        ("SELL", "market"): mt5.ORDER_TYPE_SELL,
        ("BUY", "limit"): mt5.ORDER_TYPE_BUY_LIMIT,
        ("SELL", "limit"): mt5.ORDER_TYPE_SELL_LIMIT,
        ("BUY", "stop"): mt5.ORDER_TYPE_BUY_STOP,
        ("SELL", "stop"): mt5.ORDER_TYPE_SELL_STOP,
    }

    is_market = req.order_type == "market"
    tps = req.tp or [0.0]
    lot_each = round(max(mt5.symbol_info(symbol).volume_min, lot / len(tps)), 2)

    results = []
    for tp in tps:
        order = {
            "action": mt5.TRADE_ACTION_DEAL if is_market else mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": lot_each,
            "type": tmap[(req.action, req.order_type)],
            "price": price if is_market else entry,
            "sl": req.sl,
            "tp": tp,
            "deviation": 20,
            "magic": MAGIC,
            "comment": f"{req.comment}|{req.signal_id}"[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_FOK,
        }

        res = mt5.order_send(order)
        if res is None:
            log.warning("order_send returned None for %s %s lot=%s tp=%s — retrying once", req.action, symbol, lot_each, tp)
            time.sleep(1.0)
            res = mt5.order_send(order)
        if res is None:
            log.error("order_send returned None again for %s %s — aborting", req.action, symbol)
            results.append({"retcode": -1, "order": 0, "tp": tp, "error": "MT5 RPC returned None"})
            continue
        if res.retcode == mt5.TRADE_RETCODE_REQUOTE:
            time.sleep(0.5)
            res = mt5.order_send(order)

        results.append({"retcode": res.retcode, "order": res.order, "tp": tp})
        log.info("%s %s lot=%s tp=%s -> retcode=%s", req.action, symbol, lot_each, tp, res.retcode)

    if all(r["retcode"] == mt5.TRADE_RETCODE_DONE for r in results):
        executed_signals.add(req.signal_id)
        return {"status": "executed", "symbol": symbol, "lot_each": lot_each, "orders": results}

    raise HTTPException(500, f"partial/failed execution: {results}")


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
            res = mt5.order_send(
                {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": p.symbol,
                    "volume": vol,
                    "position": p.ticket,
                    "type": mt5.ORDER_TYPE_SELL if p.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY,
                    "price": tick.bid if p.type == mt5.POSITION_TYPE_BUY else tick.ask,
                    "deviation": 20,
                    "magic": MAGIC,
                    "type_filling": mt5.ORDER_FILLING_FOK,
                }
            )
        else:
            new_sl = p.price_open if req.op == "sl_to_be" else req.value
            res = mt5.order_send(
                {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "position": p.ticket,
                    "symbol": p.symbol,
                    "sl": new_sl,
                    "tp": p.tp,
                }
            )

        out.append({"ticket": p.ticket, "retcode": res.retcode})

    return {"status": "done", "results": out}
