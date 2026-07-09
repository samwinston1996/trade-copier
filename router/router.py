"""
Signal Router — listener webhook -> Hermes (Gemini parse) -> MT5 bridge.
Hermes is the brain; this service is dumb plumbing + safety gates.
"""
import os, json, re, logging
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from typing import Optional
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("router")

HERMES_API_URL = os.environ["HERMES_API_URL"]
HERMES_API_KEY = os.environ["HERMES_API_KEY"]
WEBHOOK_TOKEN = os.environ["HERMES_WEBHOOK_TOKEN"]
BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://mt5:8000")
BRIDGE_TOKEN = os.environ["BRIDGE_TOKEN"]
ALERT_BOT_TOKEN = os.environ.get("ALERT_BOT_TOKEN", "")
ALERT_CHAT_ID = os.environ.get("ALERT_CHAT_ID", "")
MIN_CONFIDENCE = 0.8

SYSTEM_PROMPT = """You are a trade-signal parser. Input: a raw Telegram message from a signals channel.
Output: ONLY a single JSON object, no prose, no markdown fences.
Schema:
{"action":"BUY|SELL|CLOSE|MODIFY|IGNORE","symbol":"...","order_type":"market|limit|stop",
 "entry":number|null,"sl":number|null,"tp":[numbers],"manage_op":"close_all|close_partial|sl_to_be|modify_sl"|null,
 "partial_pct":number|null,"confidence":0.0-1.0}
Rules:
- Normalize symbols: GOLD/XAU -> XAUUSD, DOW -> US30, NAS -> NAS100. Uppercase.
- Commentary, recaps, ads, results screenshots, greetings -> action IGNORE.
- NEVER invent prices. If SL is missing for BUY/SELL, set confidence <= 0.5.
- "close", "take profit now", "secure partials", "SL to breakeven", "move SL to X" -> CLOSE or MODIFY with manage_op.
- confidence reflects how certain you are the parse is a real, complete, actionable signal."""

app = FastAPI(title="Signal Router")

class ListenerEvent(BaseModel):
    event: str
    channel_id: int
    message_id: int
    reply_to_id: Optional[int] = None
    text: str
    has_media: bool = False
    date: str

async def alert(msg: str):
    if not ALERT_BOT_TOKEN:
        return
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            await c.post(f"https://api.telegram.org/bot{ALERT_BOT_TOKEN}/sendMessage",
                         json={"chat_id": ALERT_CHAT_ID, "text": msg[:4000]})
        except Exception as e:
            log.error("alert failed: %s", e)

def extract_json(s: str) -> dict:
    s = re.sub(r"^```(json)?|```$", "", s.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        raise ValueError(f"no JSON in model output: {s[:200]}")
    return json.loads(m.group(0))

async def parse_with_hermes(text: str) -> dict:
    payload = {"model": "gemini-1.5-flash",
               "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": text}],
               "temperature": 0}
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(HERMES_API_URL, json=payload,
                         headers={"Authorization": f"Bearer {HERMES_API_KEY}"})
        r.raise_for_status()
        return extract_json(r.json()["choices"][0]["message"]["content"])

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/webhook")
async def webhook(ev: ListenerEvent, authorization: str = Header(None)):
    if authorization != f"Bearer {WEBHOOK_TOKEN}":
        raise HTTPException(401, "bad token")
    if ev.has_media and not ev.text.strip():
        return {"status": "ignored", "reason": "media-only"}

    try:
        sig = await parse_with_hermes(ev.text)
    except Exception as e:
        await alert(f"⚠️ Parse failed for {ev.channel_id}:{ev.message_id}\n{e}")
        raise HTTPException(502, f"parse failed: {e}")

    action = sig.get("action", "IGNORE").upper()
    conf = float(sig.get("confidence", 0))
    log.info("parsed %s:%s -> %s conf=%.2f", ev.channel_id, ev.message_id, action, conf)

    if action == "IGNORE":
        return {"status": "ignored", "signal": sig}
    #if conf < MIN_CONFIDENCE:
     #   await alert(f"🟡 LOW CONFIDENCE ({conf:.2f}) — NOT trading.\n"
      #              f"Msg: {ev.text[:500]}\nParse: {json.dumps(sig)}")
       # return {"status": "low_confidence", "signal": sig}

    headers = {"Authorization": f"Bearer {BRIDGE_TOKEN}"}
    async with httpx.AsyncClient(timeout=30) as c:
        if action in ("BUY", "SELL"):
            body = {"action": action, "symbol": sig["symbol"],
                    "order_type": sig.get("order_type", "market"),
                    "entry": sig.get("entry"), "sl": sig["sl"],
                    "tp": sig.get("tp") or [],
                    "signal_id": f"{ev.channel_id}:{ev.message_id}"}
            r = await c.post(f"{BRIDGE_URL}/trade", json=body, headers=headers)
        else:  # CLOSE / MODIFY -> act on the original signal (reply_to_id)
            ref = ev.reply_to_id or ev.message_id
            body = {"signal_id": f"{ev.channel_id}:{ref}",
                    "op": sig.get("manage_op") or "close_all",
                    "value": sig.get("partial_pct")}
            r = await c.post(f"{BRIDGE_URL}/manage", json=body, headers=headers)

    if r.status_code >= 400:
        await alert(f"🔴 Bridge rejected {ev.channel_id}:{ev.message_id}: {r.status_code} {r.text[:300]}")
    else:
        await alert(f"✅ {action} {sig.get('symbol','')} executed: {r.text[:300]}")
    return {"status": "forwarded", "bridge_status": r.status_code, "bridge_body": r.json()}
