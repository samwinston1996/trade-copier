#!/usr/bin/env python3
"""CEO Dashboard — Signal lifecycle with Telegram times."""
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
if not EMAIL or not PASSWORD: print("Creds missing"); exit(1)

IST = timezone(timedelta(hours=5, minutes=30))
now_ts = time.time()
today_start_utc = now_ts - (now_ts % 86400)

# ── Router signals ──
def get_router_signals():
    r = subprocess.run("docker logs router --tail 5000 2>&1", shell=True, capture_output=True, text=True, timeout=15)
    sigs = []
    cm = {"-1002045136062":"Gold Vision","-1001766321748":"star fx trade",
          "-1001916021658":"XAUUSD ANALYSIS TEAM","-1002110392082":"Ak Capital VIP",
          "-1001777167655":"GOLD_GBP_KILLER","-1001918089511":"Distinct Forex",
          "-1001519298737":"Trade.inn FOREX","-1001327204060":"GOLD PIPS HUNTER",
          "-1002639766136":"Gold Vision (Bold)","-1002280058493":"BotX"}
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

# ── Deal classifier ──
entries, exits = [], []
for d in deals:
    c = str(d.comment or "")
    is_tp = c.startswith("[tp")
    is_sl = c.startswith("[sl")
    if is_tp or is_sl:
        exits.append({"t":d.time,"sym":d.symbol,"vol":d.volume,"pl":d.profit,"com":c,"tp":is_tp,"typ":d.type})
    else:
        entries.append({"t":d.time,"sym":d.symbol,"vol":d.volume,"pl":d.profit,"com":c[:25],"typ":d.type})

# ── Group entries into signal batches ──
entries.sort(key=lambda x: x["t"])
batches, used = [], set()
for i, e in enumerate(entries):
    if i in used: continue
    b = [e]; used.add(i)
    for j in range(i+1, len(entries)):
        if j in used: continue
        ej = entries[j]
        if ej["com"]==e["com"] and ej["typ"]==e["typ"] and abs(ej["t"]-e["t"])<30 and ej["sym"]==e["sym"]:
            b.append(ej); used.add(j)
    if b: batches.append(b)

# ── Group exits into batches too ──
exits.sort(key=lambda x: x["t"])
exit_batches = []
used_ex = set()
for i, e in enumerate(exits):
    if i in used_ex: continue
    b = [e]; used_ex.add(i)
    for j in range(i+1, len(exits)):
        if j in used_ex: continue
        ej = exits[j]
        if ej["sym"]==e["sym"] and ej["tp"]==e["tp"] and abs(ej["t"]-e["t"])<30 and abs(ej["vol"]-e["vol"])<0.02:
            b.append(ej); used_ex.add(j)
    if b: exit_batches.append(b)

# ── Match exit batches to entry batches ──
def is_near(a, b, eps=0.05):
    return abs(a - b) <= eps

# ── Build signal lifecycle ──
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
    vol = sum(x["vol"] for x in batch)
    entry_t = min(x["t"] for x in batch)
    entry_str = datetime.fromtimestamp(entry_t, tz=IST).strftime("%H:%M")
    
    # Telegram time
    rs = None
    for candidate in router_signals:
        if candidate["action"] in ("TP3_HIT","IGNORE"): continue
        if candidate["ch"].startswith(ch) or ch.startswith(candidate["ch"]):
            g = abs(candidate["ts"] - entry_t)
            if g < 300: rs = candidate
    tg_t = rs["ts"] if rs else entry_t
    tg_str = datetime.fromtimestamp(tg_t, tz=IST).strftime("%H:%M")
    
    # Find matching exit batch
    matched_eb = None
    tot_bv = sum(x["vol"] for x in batch)
    for exb in exit_batches:
        exv = sum(x["vol"] for x in exb)
        if exb[0]["sym"] != sym: continue
        if abs(exv - tot_bv) > 0.05: continue
        tg = abs(exb[0]["t"] - entry_t)
        if 0 < tg < 86400:
            matched_eb = exb
            break
    
    tps = [x for x in (matched_eb or []) if x["tp"]]
    sls = [x for x in (matched_eb or []) if not x["tp"]]
    total_pl = sum(x["pl"] for x in (matched_eb or []))
    
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
        # Check TP3 or replaced
        is_tp3 = any(rs2["action"]=="TP3_HIT" and (rs2["ch"].startswith(ch) or ch.startswith(rs2["ch"])) and entry_t < rs2["ts"] < entry_t+86400 for rs2 in router_signals)
        is_replaced = any(ob is not batch and ob[0]["com"]==ch and abs(min(x["t"] for x in ob)-entry_t) < 3600 for ob in batches)
        status = "🧹 TP3 Cleaned" if is_tp3 else ("🔄 Replaced" if is_replaced else "⏳ Open")
    
    exit_t = datetime.fromtimestamp(max(x["t"] for x in matched_eb), tz=IST).strftime("%H:%M") if matched_eb else "—"
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

# ── Week trend ──
def get_day_pnl(day_start):
    try:
        m = MetaTrader5(host="localhost", port=8001)
        m.initialize(login=int(os.environ["MT5_LOGIN"]), password=os.environ["MT5_PASSWORD"], server=os.environ["MT5_SERVER"])
        dd = list(m.history_deals_get(int(day_start), int(day_start+86400)) or [])
        m.shutdown()
        entries_d, exits_d = [], []
        for x in dd:
            c = str(x.comment or "")
            if c.startswith("[tp") or c.startswith("[sl"): exits_d.append(x)
            else: entries_d.append(x)
        # Group entry by channel
        channel_profits = defaultdict(float)
        for ex in exits_d:
            for en in entries_d:
                if en.symbol != ex.symbol: continue
                if (en.type == 0 and ex.type == 1) or (en.type == 1 and ex.type == 0):
                    if abs(en.volume - ex.volume) < 0.02 and abs(ex.time - en.time) < 86400:
                        channel_profits[str(en.comment or "")[:20]] += ex.profit
                        break
        return dict(channel_profits)
    except:
        return {}

wtd = defaultdict(lambda: defaultdict(float))
for do in range(7):
    ds = today_start_utc - (do * 86400)
    day_data = get_day_pnl(ds)
    for ch_name, pl in day_data.items():
        wtd[ch_name][f"d{-do}"] += pl

day_labels = [datetime.fromtimestamp(today_start_utc-(do*86400), tz=IST).strftime("%a") for do in range(6,-1,-1)]
trend_rows = ""
for ch in sorted(ch_stats, key=lambda c: ch_stats[c]["pnl"], reverse=True):
    vals = [wtd[ch].get(f"d{-d}",0) for d in range(6,-1,-1)]
    vs = "".join(f'<td style="color:{"green" if v>=0 else "red"};font-size:11px">${v:+.0f}</td>' for v in vals)
    tt = sum(vals); tc = "green" if tt>=0 else "red"
    trend_rows += f"<tr><td>{ch}</td>{vs}<td style='font-weight:bold;color:{tc}'>${tt:+.2f}</td></tr>"

# ── Totals ──
today_pnl = sum(e["pl"] for e in exits)
today_tp = sum(1 for e in exits if e["tp"])
today_sl = sum(1 for e in exits if not e["tp"])
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
<div class="section"><h3>📊 Week-to-Date</h3><table><tr><th>Channel</th>{"".join(f'<th>{dl}</th>' for dl in day_labels)}<th>Total</th></tr>{trend_rows}</table></div>
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