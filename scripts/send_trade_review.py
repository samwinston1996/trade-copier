#!/usr/bin/env python3
"""CEO Dashboard — Signal lifecycle via position_id matching + daily P&L history."""
import smtplib, os, time, re, subprocess, sqlite3
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
if not EMAIL or not PASSWORD: print("Creds missing"); exit(1)

IST = timezone(timedelta(hours=5, minutes=30))
now_ts = time.time()

# ── Skip weekends (UTC — cron is UTC-based) ──
today_weekday = datetime.fromtimestamp(now_ts, tz=timezone.utc).weekday()
if today_weekday >= 5:  # 5=Sat, 6=Sun
    print("Weekend — skipping email")
    exit(0)

today_start_utc = now_ts - (now_ts % 86400)
today_date = datetime.fromtimestamp(today_start_utc, tz=IST).strftime("%Y-%m-%d")

# ── Router signals ──
def get_router_signals():
    r = subprocess.run("docker logs router --tail 5000 2>&1", shell=True, capture_output=True, text=True, timeout=15)
    sigs = []
    cm = {"-1002045136062":"BotX channel","-1001766321748":"Trade.inn FOREX",
          "-1001916021658":"star fx trade","-1002110392082":"XAUUSD ANALYSIS TEAM",
          "-1001777167655":"GOLD_GBP_KILLER","-1001918089511":"Paul GOLD Dominator",
          "-1001519298737":"Ak Capital VIP","-1001327204060":"GOLD PIPS HUNTER",
          "-1002639766136":"Gold Vision","-1002280058493":"XAUUSD BEST TRADER"}
    for line in r.stdout.split('\n'):
        m = re.search(r'(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d{3} INFO parsed (-?\d+):(\d+) -> (\w+) conf=([\d.]+)', line)
        if m:
            ts = datetime.strptime(f"{m.group(1)} {m.group(2)}","%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if ts.timestamp() < today_start_utc: continue
            sigs.append({"ts":ts.timestamp(),"ch":cm.get(m.group(3),m.group(3)),"action":m.group(5)})
        m2 = re.search(r'(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d{3} INFO TP3 hit for (-?\d+):(\d+)', line)
        if m2:
            ts = datetime.strptime(f"{m2.group(1)} {m2.group(2)}","%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if ts.timestamp() < today_start_utc: continue
            sigs.append({"ts":ts.timestamp(),"ch":cm.get(m2.group(3),m2.group(3)),"action":"TP3_HIT"})
    return sigs

router_signals = get_router_signals()

# ── MT5 data ──
mt5 = MetaTrader5(host="localhost", port=8001)
mt5.initialize(login=int(os.environ["MT5_LOGIN"]), password=os.environ["MT5_PASSWORD"], server=os.environ["MT5_SERVER"])
deals = list(mt5.history_deals_get(int(today_start_utc), int(now_ts)) or [])
orders = list(mt5.history_orders_get(int(today_start_utc), int(now_ts)) or [])
acct = mt5.account_info()
balance = acct.balance if acct else 0
mt5.shutdown()

# ── Group deals by position_id ──
positions = defaultdict(lambda: {"id": None, "entry": None, "exit": None, "profit": 0.0})
entries_list = []

for d in deals:
    pid = getattr(d, 'position_id', 0)
    if not pid: continue
    c = str(d.comment or "")
    is_exit = c.startswith("[tp") or c.startswith("[sl")
    
    if is_exit:
        if positions[pid]["exit"] is None or d.time > positions[pid]["exit"]["t"]:
            positions[pid]["exit"] = {"t": d.time, "sym": d.symbol, "vol": d.volume, "pl": d.profit, "tp": c.startswith("[tp")}
        positions[pid]["profit"] += d.profit
    else:
        if positions[pid]["entry"] is None:
            positions[pid]["entry"] = {"t": d.time, "sym": d.symbol, "vol": d.volume, "pl": d.profit, "com": c[:25], "typ": d.type}
            positions[pid]["id"] = pid
        positions[pid]["profit"] += d.profit

# Filter: only positions with an entry (real positions)
positions = {pid: p for pid, p in positions.items() if p["entry"] and p["id"]}

# ── Group positions into signal batches ──
all_pos = sorted(positions.values(), key=lambda p: p["entry"]["t"])
batches = []
used = set()
for i, p in enumerate(all_pos):
    if i in used: continue
    e = p["entry"]
    batch = [p]; used.add(i)
    for j in range(i+1, len(all_pos)):
        if j in used: continue
        ej = all_pos[j]["entry"]
        if ej["com"]==e["com"] and ej["typ"]==e["typ"] and abs(ej["t"]-e["t"])<30 and ej["sym"]==e["sym"]:
            batch.append(all_pos[j]); used.add(j)
    if batch: batches.append(batch)

# ── Telegram time lookup ──
def find_tg(ch, entry_t):
    best, bg = None, 999
    for rs in router_signals:
        if rs["action"] in ("TP3_HIT","IGNORE"): continue
        if rs["ch"].startswith(ch) or ch.startswith(rs["ch"]):
            g = abs(rs["ts"] - entry_t)
            if g < 300 and g < bg: bg, best = g, rs
    return best

# ── Build signal lifecycle ──
signal_rows = ""
ch_stats = defaultdict(lambda: {"n":0,"tp":0,"sl":0,"pnl":0.0})
exit_positions_used = set()

for batch in batches:
    ch = batch[0]["entry"]["com"] or batch[0]["entry"]["sym"]
    direction = "BUY" if batch[0]["entry"]["typ"]==0 else "SELL"
    sym = batch[0]["entry"]["sym"]
    vol = sum(p["entry"]["vol"] for p in batch)
    entry_t = min(p["entry"]["t"] for p in batch)
    entry_str = datetime.fromtimestamp(entry_t, tz=IST).strftime("%H:%M")
    
    # Telegram time
    rs = find_tg(ch, entry_t)
    tg_t = rs["ts"] if rs else entry_t
    tg_str = datetime.fromtimestamp(tg_t, tz=IST).strftime("%H:%M")
    
    # Collect exits from all positions in this batch
    mexits = []
    total_pl = 0.0
    for p in batch:
        if p["exit"]:
            mexits.append(p["exit"])
            total_pl += p["exit"]["pl"]
    
    tps = [x for x in mexits if x["tp"]]
    sls = [x for x in mexits if not x["tp"]]
    
    # Status
    if tps and sls:
        et = datetime.fromtimestamp(max(x["t"] for x in tps+sls), tz=IST).strftime("%H:%M")
        status = f"✅ TP {len(tps)}× / ❌ SL {len(sls)}× @ {et}"
    elif tps:
        et = datetime.fromtimestamp(max(x["t"] for x in tps), tz=IST).strftime("%H:%M")
        status = f"✅ TP Hit ({len(tps)}×) @ {et}"
    elif sls:
        et = datetime.fromtimestamp(max(x["t"] for x in sls), tz=IST).strftime("%H:%M")
        status = f"❌ SL Hit ({len(sls)}×) @ {et}"
    else:
        is_tp3 = any(rs2["action"]=="TP3_HIT" and (rs2["ch"].startswith(ch) or ch.startswith(rs2["ch"])) and entry_t < rs2["ts"] < entry_t+86400 for rs2 in router_signals)
        is_replaced = any(ob is not batch and ob[0]["entry"]["com"]==ch and abs(ob[0]["entry"]["t"]-entry_t) < 3600 for ob in batches)
        status = "🧹 TP3 Cleaned" if is_tp3 else ("🔄 Replaced" if is_replaced else "⏳ Open")
    
    exit_t = datetime.fromtimestamp(max(x["t"] for x in mexits), tz=IST).strftime("%H:%M") if mexits else "—"
    pc = "green" if total_pl >= 0 else "red"
    
    ch_stats[ch]["n"] += 1
    ch_stats[ch]["tp"] += len(tps)
    ch_stats[ch]["sl"] += len(sls)
    ch_stats[ch]["pnl"] += total_pl
    
    signal_rows += f"<tr><td nowrap>{tg_str}</td><td>{ch}</td><td>{direction}</td><td>{sym}</td><td>{vol:.2f}</td><td nowrap>{entry_str}</td><td nowrap>{exit_t}</td><td>{status}</td><td style='color:{pc};font-weight:bold'>${total_pl:+.2f}</td></tr>"

# ── Missed orders ──
for o in orders:
    if o.type in (3,4,5,6) and getattr(o,'state',0) in (2,4):
        ot = ["","","","BUY_LIMIT","SELL_LIMIT","BUY_STOP","SELL_STOP"][o.type] if o.type<7 else f"T{o.type}"
        tt = getattr(o,'time_setup',0) or getattr(o,'time',0)
        ts = datetime.fromtimestamp(tt, tz=IST).strftime("%H:%M") if tt else "?"
        pr = getattr(o,'price_setup',0) or getattr(o,'price',0)
        co = str(o.comment or "")[:20]
        signal_rows += f"<tr style='opacity:0.6'><td nowrap>{ts}</td><td>{co}</td><td>{ot.split('_')[0]}</td><td>{o.symbol}</td><td>—</td><td>—</td><td>—</td><td>🚫 Expired @ {pr}</td><td>$0.00</td></tr>"

# ════════════════════════════════════════════════════════════
# ── Persist today's per-channel P&L to SQLite ──
# ════════════════════════════════════════════════════════════

DB_PATH = "/tmp/daily_pnl.db"
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
c = conn.cursor()
c.execute("""
    CREATE TABLE IF NOT EXISTS daily_pnl (
        date TEXT NOT NULL,
        channel TEXT NOT NULL,
        pnl REAL NOT NULL DEFAULT 0.0,
        tp INTEGER NOT NULL DEFAULT 0,
        sl INTEGER NOT NULL DEFAULT 0,
        signals INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (date, channel)
    )
""")

for ch, stats in ch_stats.items():
    c.execute("""
        INSERT INTO daily_pnl (date, channel, pnl, tp, sl, signals)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(date, channel) DO UPDATE SET
            pnl = excluded.pnl,
            tp = excluded.tp,
            sl = excluded.sl,
            signals = excluded.signals
    """, (today_date, ch, stats["pnl"], stats["tp"], stats["sl"], stats["n"]))

conn.commit()

# ── Query all historical data for the daily performance table ──
c.execute("SELECT date, channel, pnl FROM daily_pnl ORDER BY date ASC, channel ASC")
db_rows = c.fetchall()
conn.close()

# Build the daily performance table (days as rows, channels as columns)
day_data = {}       # date -> {channel -> pnl}
channel_totals = defaultdict(float)
date_labels = []

for r in db_rows:
    d = r["date"]
    ch = r["channel"]
    pnl = r["pnl"]
    if d not in day_data:
        day_data[d] = {}
        date_labels.append(d)
    day_data[d][ch] = pnl
    channel_totals[ch] += pnl

# Sort channels by total P&L descending
all_channels = sorted(channel_totals.keys(), key=lambda c: channel_totals[c], reverse=True)

# Render daily performance HTML
daily_table_rows = ""
for d in date_labels:
    cells = ""
    day_pnl_total = 0.0
    for ch in all_channels:
        v = day_data[d].get(ch)
        if v is not None:
            c = "green" if v >= 0 else "red"
            cells += f'<td style="color:{c}">${v:+.2f}</td>'
            day_pnl_total += v
        else:
            cells += "<td style='color:#ccc'>—</td>"
    dt_str = datetime.strptime(d, "%Y-%m-%d").strftime("%a %b %-d")
    dc = "green" if day_pnl_total >= 0 else "red"
    cells += f'<td style="font-weight:bold;color:{dc}">${day_pnl_total:+.2f}</td>'
    daily_table_rows += f"<tr><td nowrap>{dt_str}</td>{cells}</tr>"

# Total row
total_cells = ""
grand_total = 0.0
for ch in all_channels:
    v = channel_totals[ch]
    c = "green" if v >= 0 else "red"
    total_cells += f'<td style="font-weight:bold;color:{c}">${v:+.2f}</td>'
    grand_total += v
gtc = "green" if grand_total >= 0 else "red"
total_cells += f'<td style="font-weight:bold;color:{gtc}">${grand_total:+.2f}</td>'

channel_headers = "".join(f"<th>{ch}</th>" for ch in all_channels)

daily_section = f"""<div class="section"><h3>📊 Daily Performance</h3><table>
<tr><th>Date</th>{channel_headers}<th>Total</th></tr>
{daily_table_rows}
<tr style="background:#f0f0f0;font-weight:bold"><td>TOTAL</td>{total_cells}</tr>
</table></div>"""

# ════════════════════════════════════════════════════════════
# ── Build HTML ──
# ════════════════════════════════════════════════════════════

today_pnl = sum(p["exit"]["pl"] for p in positions.values() if p["exit"])
today_tp = sum(1 for p in positions.values() if p["exit"] and p["exit"]["tp"])
today_sl = sum(1 for p in positions.values() if p["exit"] and not p["exit"]["tp"])
wr = (today_tp/(today_tp+today_sl)*100) if today_tp+today_sl>0 else 0
date_str = datetime.now(tz=IST).strftime("%b %d, %Y")
oc = "green" if today_pnl>=0 else "red"

ch_rows = ""
for ch in sorted(ch_stats, key=lambda c: ch_stats[c]["pnl"], reverse=True):
    s = ch_stats[ch]; t = s["tp"]+s["sl"]; w = (s["tp"]/t*100) if t>0 else 0
    c = "green" if s["pnl"]>=0 else "red"
    ch_rows += f"<tr><td>{ch}</td><td>{s['n']}</td><td>{s['tp']}</td><td>{s['sl']}</td><td>{w:.0f}%</td><td style='color:{c};font-weight:bold'>${s['pnl']:+.2f}</td></tr>"

html = f"""<!DOCTYPE html>
<html><head><style>
body {{ font-family: Arial; padding:20px; background:#f5f5f5; max-width:1100px; margin:auto; }}
h2 {{ color:#1a1a2e; }} .date {{ color:#666; font-size:13px; }}
.section {{ background:white; border-radius:8px; padding:15px; margin:15px 0; box-shadow:0 1px 3px rgba(0,0,0,0.1); }}
.section h3 {{ margin-top:0; color:#1a1a2e; border-bottom:2px solid #eee; padding-bottom:8px; font-size:15px; }}
.sc {{ display:flex; gap:12px; flex-wrap:wrap; }}
.si {{ flex:1; min-width:100px; text-align:center; padding:8px; background:#f8f9ff; border-radius:6px; }}
.si .l {{ font-size:11px; color:#888; }} .si .v {{ font-size:18px; font-weight:bold; }}
table {{ border-collapse:collapse; width:100%; font-size:12px; }}
th {{ background:#1a1a2e; color:white; padding:6px; text-align:left; font-size:11px; white-space:nowrap; }}
td {{ padding:5px 6px; border-bottom:1px solid #eee; }}
tr:hover {{ background:#f8f9ff; }}
.footer {{ color:#888; font-size:11px; margin-top:20px; text-align:center; }}
</style></head><body>
<h2>📊 Hermes Trade Copier — CEO Dashboard</h2>
<p class="date">{date_str} | IST</p>
<div class="section"><div class="sc">
<div class="si"><div class="l">Balance</div><div class="v">${balance:.2f}</div></div>
<div class="si"><div class="l">Today P&L</div><div class="v" style="color:{oc}">${today_pnl:+.2f}</div></div>
<div class="si"><div class="l">Win Rate</div><div class="v">{wr:.0f}%</div></div>
<div class="si"><div class="l">TPs / SLs</div><div class="v"><span style="color:#2e7d32">{today_tp}</span> / <span style="color:#c62828">{today_sl}</span></div></div>
<div class="si"><div class="l">Signals</div><div class="v">{len(batches)}</div></div>
</div></div>
<div class="section"><h3>📈 Per-Channel P&amp;L</h3><table><tr><th>Channel</th><th>Sig</th><th>TP</th><th>SL</th><th>Win%</th><th>Net $</th></tr>
{ch_rows}
<tr style="background:#f0f0f0;font-weight:bold"><td>TOTAL</td><td>{len(batches)}</td><td>{today_tp}</td><td>{today_sl}</td><td>{wr:.0f}%</td><td style="color:{oc}">${today_pnl:+.2f}</td></tr>
</table></div>
{daily_section}
<div class="section"><h3>📋 Signal Lifecycle</h3><table>
<tr><th>Telegram</th><th>Channel</th><th>Dir</th><th>Sym</th><th>Lot</th><th>MT5 Entry</th><th>TP/SL Time</th><th>Status</th><th>P&amp;L</th></tr>
{signal_rows}
</table></div>
<div class="footer">Hermes Trade Copier — {date_str}</div>
</body></html>"""

msg = MIMEText(html, "html")
msg["Subject"] = f"Hermes Trade Copier — {date_str} | ${today_pnl:+.2f}"
msg["From"] = EMAIL; msg["To"] = RECIPIENT
srv = smtplib.SMTP("smtp.gmail.com", 587)
srv.starttls(); srv.login(EMAIL, PASSWORD)
srv.sendmail(EMAIL, [RECIPIENT], msg.as_string()); srv.quit()
print(f"✅ Sent! P&L ${today_pnl:+.2f}")