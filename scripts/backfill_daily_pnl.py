#!/usr/bin/env python3
"""Backfill daily_pnl.db from MT5 deal history (Jul 30 → now)."""
import sqlite3, time, os
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from mt5linux import MetaTrader5

DB_PATH = "/tmp/daily_pnl.db"
IST = timezone(timedelta(hours=5, minutes=30))
RANGE_START = int(datetime(2026, 7, 30, tzinfo=timezone.utc).timestamp())
RANGE_END = int(time.time())

mt5 = MetaTrader5("localhost", port=8001)
mt5.initialize()
deals = list(mt5.history_deals_get(RANGE_START, RANGE_END) or [])
mt5.shutdown()

print(f"Deals from Jul 30: {len(deals)}")

# Group by position_id
positions = defaultdict(lambda: {"entry": None, "exit": None, "pnl": 0.0, "channel": None, "entry_time": 0})
for d in deals:
    pid = getattr(d, "position_id", 0)
    if not pid:
        continue
    deal_entry = getattr(d, "entry", 0)  # 0=DEAL_ENTRY_IN, 1=DEAL_ENTRY_OUT
    c = str(d.comment or "")
    reason = getattr(d, "reason", 3)
    
    if deal_entry == 1:  # DEAL_ENTRY_OUT — exit deal
        is_tp = c.startswith("[tp") or reason == 5
        if positions[pid]["exit"] is None or d.time > positions[pid]["exit"]["t"]:
            positions[pid]["exit"] = {"t": d.time, "pl": d.profit, "tp": is_tp}
        positions[pid]["pnl"] += d.profit
    else:  # DEAL_ENTRY_IN — entry deal
        if positions[pid]["entry"] is None:
            positions[pid]["entry"] = {"t": d.time, "com": c[:25]}
            positions[pid]["entry_time"] = d.time
            positions[pid]["channel"] = c[:25]
        positions[pid]["pnl"] += d.profit

# Filter: only positions with an entry
positions = {pid: p for pid, p in positions.items() if p["entry"]}
print(f"Unique positions: {len(positions)}")

# Aggregate daily per-channel
daily = defaultdict(lambda: defaultdict(lambda: {"pnl": 0.0, "signals": 0}))
for pid, p in positions.items():
    p_exit = p["exit"]
    if p_exit and abs(p["entry"]["t"] - p_exit.get("t", 0)) < 5:
        continue  # immediate close — not a real trade
    day = datetime.fromtimestamp(p["entry_time"], tz=IST).strftime("%Y-%m-%d")
    ch = p["channel"] or "Unknown"
    daily[day][ch]["pnl"] += p["pnl"]
    daily[day][ch]["signals"] += 1

# Upsert into DB
conn = sqlite3.connect(DB_PATH)
conn.execute("""CREATE TABLE IF NOT EXISTS daily_pnl (
    date TEXT, channel TEXT, pnl REAL, tp INTEGER, sl INTEGER, signals INTEGER,
    PRIMARY KEY (date, channel))""")

total_rows = 0
for day in sorted(daily):
    for ch in sorted(daily[day], key=lambda c: daily[day][c]["pnl"], reverse=True):
        s = daily[day][ch]
        conn.execute("""INSERT INTO daily_pnl (date,channel,pnl,tp,sl,signals)
            VALUES (?,?,?,?,?,?) ON CONFLICT(date,channel) DO UPDATE SET
            pnl=excluded.pnl, tp=excluded.tp, sl=excluded.sl, signals=excluded.signals""",
            (day, ch, round(s["pnl"], 2), 0, 0, s["signals"]))
        total_rows += 1

conn.commit()
days = conn.execute("SELECT COUNT(DISTINCT date) FROM daily_pnl").fetchone()[0]
rows = conn.execute("SELECT COUNT(*) FROM daily_pnl").fetchone()[0]
conn.close()

print(f"✅ Backfilled {rows} rows across {days} days")
print(f"New rows added: {total_rows}")