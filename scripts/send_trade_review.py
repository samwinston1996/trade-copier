#!/usr/bin/env python3
"""CEO Dashboard — Signal lifecycle with Telegram times + week trend."""
import smtplib, os, time, re, subprocess
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from collections import defaultdict
from mt5linux import MetaTrader5

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

# ── Get router signals ──
def get_router_signals():
    result = subprocess.run("docker logs router --tail 5000 2>&1", shell=True, capture_output=True, text=True, timeout=15)
    logs = result.stdout
    signals = []
    cmap = {"-1002045136062":"Gold Vision","-1001766321748":"star fx trade",
            "-1001916021658":"XAUUSD ANALYSIS TEAM","-1002110392082":"Ak Capital VIP",
            "-1001777167655":"GOLD_GBP_KILLER","-1001918089511":"Distinct Forex",
            "-1001519298737":"Trade.inn FOREX","-1001327204060":"GOLD PIPS HUNTER",
            "-1002639766136":"Gold Vision (Bold)","-1002280058493":"BotX"}
    for line in logs.split('\n'):
        m = re.search(r'(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d{3} INFO parsed (-?\d+):(\d+) -> (\w+) conf=([\d.]+)', line)
        if m:
            ts = datetime.strptime(f"{m.group(1)} {m.group(2)}","%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if ts.timestamp() < today_start_utc: continue
            signals.append({"ts":ts.timestamp(),"ch":cmap.get(m.group(3),m.group(3)),"action":m.group(5)})
        m2 = re.search(r'(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d{3} INFO TP3 hit for (-?\d+):(\d+)', line)
        if m2:
            ts = datetime.strptime(f"{m2.group(1)} {m2.group(2)}","%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if ts.timestamp() < today_start_utc: continue
            signals.append({"ts":ts.timestamp(),"ch":cmap.get(m2.group(3),m2.group(3)),"action":"TP3_HIT"})
    return signals

router_signals = get_router_signals()

# ── MT5 data ──
mt5 = MetaTrader5(host="localhost", port=8001)
mt5.initialize(login=int(os.environ["MT5_LOGIN"]), password=os.environ["MT5_PASSWORD"], server=os.environ["MT5_SERVER"])
deals = list(mt5.history_deals_get(int(today_start_utc), int(now)) or [])
orders = list(mt5.history_orders_get(int(today_start_utc), int(now)) or [])
acct = mt5.account_info()
balance = acct.balance if acct else 0
mt5.shutdown()

entries, exits = [], []
for d in deals:
    c = str(d.comment or "")
    if c.startswith("[tp") or c.startswith("[sl"):
        exits.append({"t":d.time,"tk":d.ticket,"typ":d.type,"sym":d.symbol,"vol":d.volume,"pr":d.price,"pl":d.profit,"com":c,"tp":c.startswith("[tp")})
    else:
        entries.append({"t":d.time,"tk":d.ticket,"typ":d.type,"sym":d.symbol,"vol":d.volume,"pr":d.price,"pl":d.profit,"com":c[:25]})

# Group entries into batches (same signal)
entries.sort(key=lambda x: x["t"])
batches, used = [], set()
for i, e in enumerate(entries):
    if i in used: continue
    batch = [e]; used.add(i)
    for j in range(i+1, len(entries)):
        if j in used: continue
        ej = entries[j]
        if ej["com"]==e["com"] and ej["typ"]==e["typ"] and abs(ej["t"]-e["t"])<30 and ej["sym"]==e["sym"]:
            batch.append(ej); used.add(j)
    if batch: batches.append(batch)

def find_rs(batch):
    if not batch: return None
    ch = batch[0]["com"][:25]
    bt = batch[0]["t"]
    best, bg = None, 999
    for rs in router_signals:
        if rs["action"] in ("TP3_HIT","IGNORE"): continue
        if rs["ch"].startswith(ch) or ch.startswith(rs["ch"]):
            g = abs(rs["ts"] - bt)
            if g < 300 and g < bg: bg, best = g, rs
    return best

# ── Signal lifecycle rows ──
signal_rows = ""
seen_batches = set()
ch_stats = defaultdict(lambda: {"n":0,"tp":0,"sl":0,"pnl":0.0})

for batch in batches:
    bid = id(batch)
    if bid in seen_batches: continue
    seen_batches.add(bid)
    
    ch = batch[0]["com"] or batch[0]["sym"]
    direction = "BUY" if batch[0]["typ"]==0 else "SELL"
    sym = batch[0]["sym"]
    vol = sum(e["vol"] for e in batch)
    entry_t = min(e["t"] for e in batch)
    entry_str = datetime.fromtimestamp(entry_t, tz=IST).strftime("%H:%M")
    
    # Telegram time from router
    rs = find_rs(batch)
    tg_time = rs["ts"] if rs else entry_t
    tg_str = datetime.fromtimestamp(tg_time, tz=IST).strftime("%H:%M")
    
    # Find matching exits
    mexits = []
    for ex in exits:
        for be in batch:
            if be["sym"]!=ex["sym"]: continue
            if (be["typ"]==0 and ex["typ"]==1) or (be["typ"]==1 and ex["typ"]==0):
                if abs(ex["t"]-be["t"])<86400 and abs(be["vol"]-ex["vol"])<0.02:
                    mexits.append(ex)
                    break
    
    tps = [x for x in mexits if x["tp"]]
    sls = [x for x in mexits if not x["tp"]]
    total_pl = sum(x["pl"] for x in mexits)
    
    # Status
    if tps and not sls:
        et = datetime.fromtimestamp(max(x["t"] for x in tps), tz=IST).strftime("%H:%M")
        status = f"✅ TP Hit ({len(tps)}×) @ {et}"
    elif sls and not tps:
        et = datetime.fromtimestamp(max(x["t"] for x in sls), tz=IST).strftime("%H:%M")
        status = f"❌ SL Hit ({len(sls)}×) @ {et}"
    else:
        # Check TP3 or replacement
        is_tp3 = False
        for rs2 in router_signals:
            if rs2["action"]=="TP3_HIT" and (rs2["ch"].startswith(ch) or ch.startswith(rs2["ch"])):
                if entry_t < rs2["ts"] < entry_t + 86400:
                    is_tp3 = True
                    break
        if is_tp3:
            status = "🧹 TP3 Cleaned"
        else:
            is_replaced = False
            for ob in batches:
                if ob is batch: continue
                if ob[0]["com"]==ch and min(e["t"] for e in ob) > entry_t and abs(min(e["t"] for e in ob)-entry_t) < 3600:
                    is_replaced = True; break
            status = "🔄 Replaced by new signal" if is_replaced else "⏳ Open"
    
    # Exit time
    exit_t = max([x["t"] for x in mexits]) if mexits else None
    exit_str = datetime.fromtimestamp(exit_t, tz=IST).strftime("%H:%M") if exit_t else "—"
    pc = "green" if total_pl >= 0 else "red"
    
    ch_stats[ch]["n"] += 1
    ch_stats[ch]["tp"] += len(tps)
    ch_stats[ch]["sl"] += len(sls)
    ch_stats[ch]["pnl"] += total_pl
    
    signal_rows += f"""
    <tr>
        <td nowrap>{tg_str}</td>
        <td>{ch}</td>
        <td>{direction}</td>
        <td>{sym}</td>
        <td>{vol:.2f}</td>
        <td nowrap>{entry_str}</td>
        <td nowrap>{exit_str}</td>
        <td>{status}</td>
        <td style="color:{pc};font-weight:bold">${total_pl:+.2f}</td>
    </tr>"""

# ── Missed orders ──
for o in orders:
    if o.type in (3,4,5,6) and getattr(o,'state',0) in (2,4):
        ot = ["","","","BUY_LIMIT","SELL_LIMIT","BUY_STOP","SELL_STOP"][o.type] if o.type<7 else f"T{o.type}"
        t = getattr(o,'time_setup',0) or getattr(o,'time',0)
        ts_str = datetime.fromtimestamp(t, tz=IST).strftime("%H:%M") if t else "?"
        pr = getattr(o,'price_setup',0) or getattr(o,'price',0)
        com = str(o.comment or "")[:20]
        signal_rows += f"""<tr style="opacity:0.6"><td nowrap>{ts_str}</td><td>{com}</td><td>{ot.split('_')[0]}</td><td>{o.symbol}</td><td>—</td><td>—</td><td>—</td><td>🚫 Expired @ {pr}</td><td>$0.00</td></tr>"""

# ── Week trend ──
def get_day_pnl(day_start):
    try:
        m = MetaTrader5(host="localhost", port=8001)
        m.initialize(login=int(os.environ["MT5_LOGIN"]), password=os.environ["MT5_PASSWORD"], server=os.environ["MT5_SERVER"])
        dd = list(m.history_deals_get(int(day_start), int(day_start+86400)) or [])
        m.shutdown()
        return {str(d.comment or "")[:20]: d.profit for d in dd if str(d.comment or "").startswith("[tp") or str(d.comment or "").startswith("[sl")}
    except: return {}

wtd = defaultdict(lambda: defaultdict(float))
for do in range(7):
    ds = today_start_utc - (do * 86400)
    dpnl = get_day_pnl(ds)
    for ch_name, pl_ in dpnl.items():
        wtd[ch_name][f"d{-do}"] += pl_

day_labels = []
for do in range(6,-1,-1):
    d = datetime.fromtimestamp(today_start_utc - (do*86400), tz=IST)
    day_labels.append(d.strftime("%a"))
trend_rows = ""
for ch in sorted(ch_stats, key=lambda c: ch_stats[c]["pnl"], reverse=True):
    td = [wtd[ch].get(f"d{-d}",0) for d in range(6,-1,-1)]
    td_s = "".join(f'<td style="color:{"green" if t>=0 else "red"};font-size:11px">${t:+.0f}</td>' for t in td)
    tt = sum(td)
    tcol = "green" if tt >= 0 else "red"
    trend_rows += f"<tr><td>{ch}</td>{td_s}<td style='font-weight:bold;color:{tcol}'>${tt:+.2f}</td></tr>"

# ── Totals ──
today_pnl = sum(e["pl"] for e in exits)
today_tp = sum(1 for e in exits if e["tp"])
today_sl = sum(1 for e in exits if not e["tp"])
win_rate = (today_tp/(today_tp+today_sl)*100) if (today_tp+today_sl)>0 else 0
date_str = datetime.now(tz=IST).strftime("%b %d, %Y")
oc = "green" if today_pnl>=0 else "red"

ch_rows = ""
for ch in sorted(ch_stats, key=lambda c: ch_stats[c]["pnl"], reverse=True):
    s = ch_stats[ch]; tot = s["tp"]+s["sl"]; wr = (s["tp"]/tot*100) if tot>0 else 0
    col = "green" if s["pnl"]>=0 else "red"
    ch_rows += f"<tr><td>{ch}</td><td>{s['n']}</td><td>{s['tp']}</td><td>{s['sl']}</td><td>{wr:.0f}%</td><td style='color:{col};font-weight:bold'>${s['pnl']:+.2f}</td></tr>"

# ── HTML ──
html = f"""<!DOCTYPE html>
<html><head><style>
body {{ font-family: Arial, sans-serif; padding: 20px; background: #f5f5f5; max-width: 1100px; margin: auto; }}
h2 {{ color: #1a1a2e; }} .date {{ color: #666; font-size: 13px; }}
.section {{ background: white; border-radius: 8px; padding: 15px; margin: 15px 0; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
.section h3 {{ margin-top: 0; color: #1a1a2e; border-bottom: 2px solid #eee; padding-bottom: 8px; font-size: 15px; }}
.sc {{ display: flex; gap: 12px; flex-wrap: wrap; }}
.si {{ flex: 1; min-width: 100px; text-align: center; padding: 8px; background: #f8f9ff; border-radius: 6px; }}
.si .l {{ font-size: 11px; color: #888; }} .si .v {{ font-size: 18px; font-weight: bold; }}
table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
th {{ background: #1a1a2e; color: white; padding: 6px; text-align: left; font-size: 11px; white-space: nowrap; }}
td {{ padding: 5px 6px; border-bottom: 1px solid #eee; }}
tr:hover {{ background: #f8f9ff; }}
.footer {{ color: #888; font-size: 11px; margin-top: 20px; text-align: center; }}
</style></head><body>

<h2>📊 Hermes Trade Copier — CEO Dashboard</h2>
<p class="date">{date_str} | IST</p>

<div class="section">
<div class="sc">
    <div class="si"><div class="l">Balance</div><div class="v">${balance:.2f}</div></div>
    <div class="si"><div class="l">Today P&L</div><div class="v" style="color:{oc}">${today_pnl:+.2f}</div></div>
    <div class="si"><div class="l">Win Rate</div><div class="v">{win_rate:.0f}%</div></div>
    <div class="si"><div class="l">TPs / SLs</div><div class="v"><span style="color:#2e7d32">{today_tp}</span> / <span style="color:#c62828">{today_sl}</span></div></div>
    <div class="si"><div class="l">Signals</div><div class="v">{len(batches)}</div></div>
</div>
</div>

<div class="section">
<h3>📈 Per-Channel P&amp;L</h3>
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
<tr><th>Telegram</th><th>Channel</th><th>Dir</th><th>Sym</th><th>Lot</th><th>MT5 Entry</th><th>TP/SL Time</th><th>Status</th><th>P&amp;L</th></tr>
{signal_rows}
</table>
</div>

<div class="footer">Hermes Trade Copier — {date_str}</div>
</body></html>"""

msg = MIMEText(html, "html")
msg["Subject"] = f"Hermes Trade Copier — {date_str} | ${today_pnl:+.2f}"
msg["From"] = EMAIL; msg["To"] = RECIPIENT

try:
    srv = smtplib.SMTP("smtp.gmail.com", 587)
    srv.starttls(); srv.login(EMAIL, PASSWORD)
    srv.sendmail(EMAIL, [RECIPIENT], msg.as_string()); srv.quit()
    print(f"✅ Dashboard sent! P&L ${today_pnl:+.2f}")
except Exception as e:
    print(f"❌ Failed: {e}"); exit(1)