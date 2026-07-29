#!/usr/bin/env python3
"""CEO Dashboard — Full trade lifecycle with Telegram signal times."""
import smtplib, os, time, re, json, subprocess
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from collections import defaultdict
from mt5linux import MetaTrader5

# ── Config ──
EMAIL = os.environ.get("EMAIL_ADDRESS") or (
    lambda: [l.split("=",1)[1].strip() for l in open("/opt/data/.env") if l.startswith("EMAIL_ADDRESS=")][0]
)()
PASSWORD = os.environ.get("EMAIL_APP_PASSWORD") or (
    lambda: [l.split("=",1)[1].strip() for l in open("/opt/data/.env") if l.startswith("EMAIL_APP_PASSWORD=")][0]
)()
RECIPIENT = "samwinston96@gmail.com"
if not EMAIL or not PASSWORD:
    print("Credentials not found"); exit(1)

IST = timezone(timedelta(hours=5, minutes=30))
now = time.time()
today_start_utc = now - (now % 86400)

# ── Get router signal times ──
def get_router_signals():
    result = subprocess.run("docker logs router --tail 5000 2>&1", shell=True, capture_output=True, text=True, timeout=15)
    logs = result.stdout
    signals = []
    channel_map = {
        "-1002045136062": "Gold Vision", "-1001766321748": "star fx trade",
        "-1001916021658": "XAUUSD ANALYSIS TEAM", "-1002110392082": "Ak Capital VIP",
        "-1001777167655": "GOLD_GBP_KILLER", "-1001918089511": "Distinct Forex",
        "-1001519298737": "Trade.inn FOREX", "-1001327204060": "GOLD PIPS HUNTER",
        "-1002639766136": "Gold Vision (Bold)", "-1002280058493": "BotX",
    }
    for line in logs.split('\n'):
        m = re.search(r'(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d{3} INFO parsed (-?\d+):(\d+) -> (\w+) conf=([\d.]+)', line)
        if m:
            ts = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if ts.timestamp() < today_start_utc: continue
            signals.append({
                "ts": ts.timestamp(), "channel": channel_map.get(m.group(3), m.group(3)),
                "action": m.group(5), "conf": float(m.group(6)),
                "channel_id": m.group(3)
            })
        m2 = re.search(r'(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d{3} INFO TP3 hit for (-?\d+):(\d+)', line)
        if m2:
            ts = datetime.strptime(f"{m2.group(1)} {m2.group(2)}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if ts.timestamp() < today_start_utc: continue
            signals.append({
                "ts": ts.timestamp(), "channel": channel_map.get(m2.group(3), m2.group(3)),
                "action": "TP3_HIT", "channel_id": m2.group(3)
            })
    return signals

router_signals = get_router_signals()

# ── MT5 Deals ──
mt5 = MetaTrader5(host="localhost", port=8001)
mt5.initialize(login=int(os.environ["MT5_LOGIN"]), password=os.environ["MT5_PASSWORD"], server=os.environ["MT5_SERVER"])
deals = list(mt5.history_deals_get(int(today_start_utc), int(now)) or [])
orders = list(mt5.history_orders_get(int(today_start_utc), int(now)) or [])
acct = mt5.account_info()
balance = acct.balance if acct else 0
mt5.shutdown()

# Classify MT5 deals
entries = []
exits = []
for d in deals:
    c = str(d.comment or "")
    if c.startswith("[tp") or c.startswith("[sl"):
        exits.append({"t": d.time, "tk": d.ticket, "typ": d.type, "sym": d.symbol,
                       "vol": d.volume, "pr": d.price, "pl": d.profit, "com": c, "tp": c.startswith("[tp")})
    else:
        entries.append({"t": d.time, "tk": d.ticket, "typ": d.type, "sym": d.symbol,
                        "vol": d.volume, "pr": d.price, "pl": d.profit, "com": c[:25]})

# ── Group entries into signal batches ──
entries.sort(key=lambda x: x["t"])
batches = []
used = set()
for i, e in enumerate(entries):
    if i in used: continue
    batch = [e]; used.add(i)
    for j in range(i+1, len(entries)):
        if j in used: continue
        ej = entries[j]
        if (ej["com"] == e["com"] and ej["typ"] == e["typ"] and abs(ej["t"] - e["t"]) < 30 and ej["sym"] == e["sym"]):
            batch.append(ej); used.add(j)
    if batch: batches.append(batch)

# ── Match router signals to MT5 batches ──
def find_router_signal(batch):
    """Find the router signal that triggered this MT5 batch."""
    if not batch: return None
    ch = batch[0]["com"][:25]
    bt = batch[0]["t"]
    # Find closest router signal by time (+/- 5 min) matching channel
    best = None; best_gap = 999
    for rs in router_signals:
        if rs["action"] == "TP3_HIT": continue
        if rs["channel"].startswith(ch) or ch.startswith(rs["channel"]):
            gap = abs(rs["ts"] - bt)
            if gap < 300 and gap < best_gap:  # within 5 min
                best_gap = gap; best = rs
    return best

# ── Build signal lifecycle rows ──
signal_rows = ""
seen = set()
tp3_channels = set()
for batch in batches:
    bid = id(batch)
    if bid in seen: continue
    seen.add(bid)
    
    ch = batch[0]["com"] or batch[0]["sym"]
    direction = "BUY" if batch[0]["typ"] == 0 else "SELL"
    symbol = batch[0]["sym"]
    total_vol = sum(e["vol"] for e in batch)
    mt5_time = min(e["t"] for e in batch)
    
    # Find router signal time
    rs = find_router_signal(batch)
    telegram_time = rs["ts"] if rs else mt5_time
    telegram_time_str = datetime.fromtimestamp(telegram_time, tz=IST).strftime("%H:%M")
    mt5_time_str = datetime.fromtimestamp(mt5_time, tz=IST).strftime("%H:%M")
    
    # Calculate latency
    latency_sec = mt5_time - telegram_time
    if latency_sec < 10:
        latency_str = "Instant ✅"
    elif latency_sec < 60:
        latency_str = f"{int(latency_sec)}s"
    elif latency_sec < 3600:
        latency_str = f"{int(latency_sec/60)}m {int(latency_sec%60)}s ⚠️"
    else:
        latency_str = f"{int(latency_sec/3600)}h {int((latency_sec%3600)/60)}m ❌"
    
    # Find matching exits
    matched_exits = []
    for ex in exits:
        for be in batch:
            if be["sym"] != ex["sym"]: continue
            if (be["typ"] == 0 and ex["typ"] == 1) or (be["typ"] == 1 and ex["typ"] == 0):
                if abs(ex["t"] - be["t"]) < 86400 and abs(be["vol"] - ex["vol"]) < 0.02:
                    matched_exits.append(ex)
                    break
    
    tps = [x for x in matched_exits if x["tp"]]
    sls = [x for x in matched_exits if not x["tp"]]
    total_profit = sum(x["pl"] for x in matched_exits)
    
    # Check if replaced by later signal
    is_replaced = False
    for other in batches:
        if other is batch: continue
        if (other[0]["com"] == ch and min(e["t"] for e in other) > mt5_time
            and abs(min(e["t"] for e in other) - mt5_time) < 3600):
            is_replaced = True
            break
    
    # Check if TP3 hit
    is_tp3 = False
    for rs2 in router_signals:
        if rs2["action"] == "TP3_HIT" and (rs2["channel"].startswith(ch) or ch.startswith(rs2["channel"])):
            if mt5_time < rs2["ts"] < mt5_time + 86400:
                is_tp3 = True
                break
    
    # Status
    if tps and not sls:
        exit_t = datetime.fromtimestamp(max(x["t"] for x in tps), tz=IST).strftime("%H:%M")
        status = f"✅ TP @ {exit_t}"
    elif sls and not tps:
        exit_t = datetime.fromtimestamp(max(x["t"] for x in sls), tz=IST).strftime("%H:%M")
        status = f"❌ SL @ {exit_t}"
    elif is_tp3:
        status = "🧹 TP3 Cleaned"
    elif is_replaced:
        status = "🔄 Replaced by new signal"
    else:
        status = "⏳ Open"
    
    # P&L display
    pl_color = "green" if total_profit >= 0 else "red"
    
    signal_rows += f"""
    <tr>
        <td nowrap>{telegram_time_str}</td>
        <td>{ch}</td>
        <td>{direction}</td>
        <td>{symbol}</td>
        <td>{total_vol:.2f}</td>
        <td nowrap>{mt5_time_str}</td>
        <td>{latency_str}</td>
        <td nowrap>{status}</td>
        <td style="color:{pl_color};font-weight:bold">${total_profit:+.2f}</td>
    </tr>"""

# ── Missed orders (merged into signal table) ──
for o in orders:
    if o.type in (3,4,5,6) and getattr(o, 'state', 0) in (2,4):
        ot = ["","","","BUY_LIMIT","SELL_LIMIT","BUY_STOP","SELL_STOP"][o.type] if o.type < 7 else f"T{o.type}"
        t = getattr(o, 'time_setup', 0) or getattr(o, 'time', 0)
        ts_str = datetime.fromtimestamp(t, tz=IST).strftime("%H:%M") if t else "?"
        pr = getattr(o, 'price_setup', 0) or getattr(o, 'price', 0)
        com = str(o.comment or "")[:20]
        signal_rows += f"""
    <tr style="opacity:0.7">
        <td nowrap>{ts_str}</td>
        <td>{com}</td>
        <td>{ot.split('_')[0]}</td>
        <td>{o.symbol}</td>
        <td>—</td>
        <td>—</td>
        <td>—</td>
        <td>🚫 Expired @ {pr}</td>
        <td>$0.00</td>
    </tr>"""

signal_rows = signal_rows or '<tr><td colspan="9" style="text-align:center;color:#888">No trades today</td></tr>'

# ── Per-channel stats ──
ch_stats = defaultdict(lambda: {"n": 0, "tp": 0, "sl": 0, "pnl": 0.0})
for batch in batches:
    ch = batch[0]["com"][:25] or "?"
    ch_stats[ch]["n"] += 1
    for ex in exits:
        for be in batch:
            if be["sym"] != ex["sym"]: continue
            if (be["typ"] == 0 and ex["typ"] == 1) or (be["typ"] == 1 and ex["typ"] == 0):
                if abs(ex["t"] - be["t"]) < 86400 and abs(be["vol"] - ex["vol"]) < 0.02:
                    ch_stats[ch]["pnl"] += ex["pl"]
                    ch_stats[ch]["tp" if ex["tp"] else "sl"] += 1
                    break

# ── Week-to-date channel trend ──
# Get up to 7 days of data
wtd = defaultdict(lambda: defaultdict(float))
for day_offset in range(7):
    day_start = today_start_utc - (day_offset * 86400)
    day_end = day_start + 86400
    day_deals = []
    try:
        mt5_2 = MetaTrader5(host="localhost", port=8001)
        mt5_2.initialize(login=int(os.environ["MT5_LOGIN"]), password=os.environ["MT5_PASSWORD"], server=os.environ["MT5_SERVER"])
        dd = list(mt5_2.history_deals_get(int(day_start), int(day_end)) or [])
        mt5_2.shutdown()
        day_deals = dd
    except: pass
    
    for d in day_deals:
        c = str(d.comment or "")
        if c.startswith("[tp") or c.startswith("[sl"):
            # Find channel by matching to entry from same period
            ch_name = "?"
            for d2 in day_deals:
                if str(d2.comment or "") and not str(d2.comment or "").startswith("[tp") and not str(d2.comment or "").startswith("[sl"):
                    ch_name = str(d2.comment or "")[:20]
                    break
            wtd[ch_name][f"day{-day_offset}"] += d.profit

# Week trend row
day_labels = []
for do in range(6, -1, -1):
    d = datetime.fromtimestamp(today_start_utc - (do * 86400), tz=IST)
    day_labels.append(d.strftime("%a"))
trend_rows = ""
active_channels = sorted(ch_stats.keys(), key=lambda c: ch_stats[c]["pnl"], reverse=True)
for ch in active_channels:
    td = [wtd[ch].get(f"day{-d}", 0) for d in range(6, -1, -1)]
    c = "green" if sum(td) >= 0 else "red"
    td_str = "".join(f'<td style="color:{"green" if t>=0 else "red"};font-size:11px">${t:+.0f}</td>' for t in td)
    trend_rows += f"<tr><td>{ch}</td>{td_str}<td style='color:{c};font-weight:bold'>${sum(td):+.2f}</td></tr>"

# ── Totals ──
today_pnl = sum(e["pl"] for e in exits)
today_tp = sum(1 for e in exits if e["tp"])
today_sl = sum(1 for e in exits if not e["tp"])
win_rate = (today_tp / (today_tp + today_sl) * 100) if (today_tp + today_sl) > 0 else 0
date_str = datetime.now(tz=IST).strftime("%b %d, %Y")

# Channel summary rows
ch_rows = ""
for ch in sorted(ch_stats, key=lambda c: ch_stats[c]["pnl"], reverse=True):
    s = ch_stats[ch]
    tot = s["tp"] + s["sl"]
    wr = (s["tp"] / tot * 100) if tot > 0 else 0
    col = "green" if s["pnl"] >= 0 else "red"
    ch_rows += f"<tr><td>{ch}</td><td>{s['n']}</td><td>{s['tp']}</td><td>{s['sl']}</td><td>{wr:.0f}%</td><td style='color:{col};font-weight:bold'>${s['pnl']:+.2f}</td></tr>"

# ── HTML ──
oc = "green" if today_pnl >= 0 else "red"
html = f"""<!DOCTYPE html>
<html><head><style>
body {{ font-family: Arial, sans-serif; padding: 20px; background: #f5f5f5; max-width: 1100px; margin: auto; }}
h2 {{ color: #1a1a2e; margin-bottom: 5px; }}
.date {{ color: #666; font-size: 13px; }}
.section {{ background: white; border-radius: 8px; padding: 15px; margin: 15px 0; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
.section h3 {{ margin-top: 0; color: #1a1a2e; border-bottom: 2px solid #eee; padding-bottom: 8px; font-size: 15px; }}
.sc {{ display: flex; gap: 12px; flex-wrap: wrap; }}
.si {{ flex: 1; min-width: 100px; text-align: center; padding: 8px; background: #f8f9ff; border-radius: 6px; }}
.si .l {{ font-size: 11px; color: #888; }}
.si .v {{ font-size: 18px; font-weight: bold; }}
table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
th {{ background: #1a1a2e; color: white; padding: 6px; text-align: left; font-size: 11px; white-space: nowrap; }}
td {{ padding: 5px 6px; border-bottom: 1px solid #eee; }}
tr:hover {{ background: #f8f9ff; }}
.green {{ color: #2e7d32; }} .red {{ color: #c62828; }}
.footer {{ color: #888; font-size: 11px; margin-top: 20px; text-align: center; }}
tr.missed {{ opacity: 0.6; }}
</style></head><body>

<h2>📊 Hermes Trade Copier — CEO Dashboard</h2>
<p class="date">{date_str} | All times IST (UTC+5:30)</p>

<div class="section">
<div class="sc">
    <div class="si"><div class="l">Balance</div><div class="v">${balance:.2f}</div></div>
    <div class="si"><div class="l">Today P&L</div><div class="v" style="color:{oc}">${today_pnl:+.2f}</div></div>
    <div class="si"><div class="l">Win Rate</div><div class="v">{win_rate:.0f}%</div></div>
    <div class="si"><div class="l">TPs / SLs</div><div class="v"><span class="green">{today_tp}</span> / <span class="red">{today_sl}</span></div></div>
    <div class="si"><div class="l">Signals</div><div class="v">{len(batches)}</div></div>
</div>
</div>

<div class="section">
<h3>📈 Per-Channel P&L</h3>
<table><tr><th>Channel</th><th>Sig</th><th>TP</th><th>SL</th><th>Win%</th><th>Net $</th></tr>
{ch_rows}
<tr style="background:#f0f0f0;font-weight:bold"><td>TOTAL</td><td>{len(batches)}</td><td>{today_tp}</td><td>{today_sl}</td><td>{win_rate:.0f}%</td><td style="color:{oc}">${today_pnl:+.2f}</td></tr>
</table>
</div>

<div class="section">
<h3>📊 Week-to-Date Channel Trend</h3>
<table><tr><th>Channel</th>{"".join(f'<th>{dl}</th>' for dl in day_labels)}<th>Total</th></tr>
{trend_rows}
</table>
</div>

<div class="section">
<h3>📋 Signal Lifecycle</h3>
<table>
<tr><th>Telegram</th><th>Channel</th><th>Dir</th><th>Sym</th><th>Lot</th><th>MT5 Entry</th><th>Latency</th><th>Status</th><th>P&L</th></tr>
{signal_rows}
</table>
</div>

<div class="footer">Hermes Trade Copier — Trade.inn FOREX copier | {date_str}</div>
</body></html>"""

msg = MIMEText(html, "html")
msg["Subject"] = f"Hermes Trade Copier — {date_str} | ${today_pnl:+.2f}"
msg["From"] = EMAIL
msg["To"] = RECIPIENT

try:
    server = smtplib.SMTP("smtp.gmail.com", 587)
    server.starttls()
    server.login(EMAIL, PASSWORD)
    server.sendmail(EMAIL, [RECIPIENT], msg.as_string())
    server.quit()
    print(f"✅ Dashboard sent! P&L ${today_pnl:+.2f}")
except Exception as e:
    print(f"❌ Failed: {e}")
    exit(1)