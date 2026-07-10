"""
Signal Router — listener webhook -> LLM parse -> MT5 bridge.
Two parsing pathways:
  - TEXT: DeepSeek for text-only signals (buy/sell/close/manage)
  - IMAGE: Gemini vision for chart screenshots with coloured SL/TP lines
"""
import os, json, re, logging
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from typing import Optional
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("router")

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
WEBHOOK_TOKEN = os.environ["HERMES_WEBHOOK_TOKEN"]
BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://mt5:8000")
BRIDGE_TOKEN = os.environ["BRIDGE_TOKEN"]
ALERT_BOT_TOKEN = os.environ.get("ALERT_BOT_TOKEN", "")
ALERT_CHAT_ID = os.environ.get("ALERT_CHAT_ID", "")

TEXT_MODEL = os.environ.get("TEXT_MODEL", "deepseek/deepseek-v4-flash")
VISION_MODEL = os.environ.get("VISION_MODEL", "google/gemini-3.1-flash-lite-preview")

# ── Two distinct prompts ──────────────────────────────────────────

TEXT_SYSTEM_PROMPT = """You are a trade-signal parser. Input: a raw Telegram message from a signals channel.
Output: ONLY a single JSON object, no prose, no markdown fences.
Schema:
{"action":"BUY|SELL|CLOSE|MODIFY|IGNORE","symbol":"...","order_type":"market|limit|stop",
 "entry":number|null,"sl":number|null,"tp":[numbers],"manage_op":"close_all|close_partial|sl_to_be|modify_sl"|null,
 "partial_pct":number|null,"confidence":0.0-1.0}
Rules:
- Normalize symbols: GOLD/XAU -> XAUUSD, DOW -> US30, NAS -> NAS100. Uppercase.
- Commentary, recaps, ads, results screenshots, greetings -> action IGNORE.
- NEVER invent prices. If SL or TP is missing for BUY/SELL, set confidence <= 0.3.
- "close", "take profit now", "secure partials", "SL to breakeven", "move SL to X" -> CLOSE or MODIFY with manage_op.
- confidence reflects how certain you are the parse is a real, complete, actionable signal.
- A valid BUY/SELL signal MUST have both SL and TP. Without both, it is incomplete."""

IMAGE_SYSTEM_PROMPT = """You are a trade-signal parser for CHART IMAGES. Input: a screenshot of a trading chart with two adjacent shaded zones.
Output: ONLY a single JSON object, no prose, no markdown fences.
Schema:
{"action":"BUY|SELL|IGNORE","symbol":"XAUUSD","order_type":"limit","entry":number,"sl":number,"tp":[number],"confidence":0.0-1.0}

Rules:
- The chart shows XAUUSD (Gold). Always output symbol "XAUUSD" and order_type "limit".
- The chart has two adjacent shaded zones separated by a horizontal border line:
  * RED shaded zone = Stop Loss zone. The SL price is at the FAR edge of the red zone (the side opposite the entry border).
  * Grey shaded zone = Trade direction / profit zone. The trade moves in this direction.
- The ENTRY price is the horizontal border line between the RED zone and the Grey zone.
- Take Profit (TP): The far boundary line of the Grey zone. Output only ONE TP price.
- Determine direction:
  * If RED zone is ABOVE and Grey zone is BELOW the entry → action = SELL
  * If Grey zone is ABOVE and RED zone is BELOW the entry → action = BUY
|- Read the exact price labels on the Y-axis for entry, SL, and TP levels.
|- For SL and TP accuracy: the right-hand price panel shows boxed price indicators. A RED box = SL price at the far edge of the red zone. A GREY box = TP price at the far boundary of the grey zone. Read these boxed values from the right panel instead of guessing from zone boundaries.
|- If you cannot read ANY price level on the chart
- NEVER invent prices. Only report levels you can clearly read from chart labels.
- A valid signal MUST have a clearly readable entry, SL, and TP. If any is missing/unreadable, set confidence <= 0.3.
- confidence: 1.0 if you read entry, SL, and TP clearly. 0.5 if only two are clear. 0.0 if nothing readable."""

app = FastAPI(title="Signal Router")


class ListenerEvent(BaseModel):
    event: str
    channel_id: int
    channel_title: str = ""
    message_id: int
    reply_to_id: Optional[int] = None
    text: str = ""
    has_media: bool = False
    image_data: Optional[str] = None   # base64-encoded image from listener
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


# ── TEXT parser (DeepSeek) ────────────────────────────────────────

async def parse_text_signal(text: str) -> dict:
    payload = {
        "model": TEXT_MODEL,
        "messages": [
            {"role": "system", "content": TEXT_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
    }
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(OPENROUTER_API_URL, json=payload,
                         headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"})
        r.raise_for_status()
        return extract_json(r.json()["choices"][0]["message"]["content"])


# ── IMAGE parser (Gemini vision) ──────────────────────────────────

async def parse_image_signal(image_b64: str, caption: str = "") -> dict:
    """Parse a chart image trade signal via Gemini vision model."""
    user_text = caption.strip() if caption.strip() else "Read the chart levels and extract the trade signal."
    payload = {
        "model": VISION_MODEL,
        "messages": [
            {"role": "system", "content": IMAGE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                ],
            },
        ],
        "temperature": 0,
    }
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(OPENROUTER_API_URL, json=payload,
                         headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"})
        r.raise_for_status()
        return extract_json(r.json()["choices"][0]["message"]["content"])


# ── Endpoints ─────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"ok": True}


@app.post("/webhook")
async def webhook(ev: ListenerEvent, authorization: str = Header(None)):
    if authorization != f"Bearer {WEBHOOK_TOKEN}":
        raise HTTPException(401, "bad token")

    # Route to the right parser based on content type
    is_image = False
    if ev.image_data and len(ev.image_data) > 100:
        is_image = True
        log.info("Routing msg %s to VISION parser (%s b64 chars)", ev.message_id, len(ev.image_data))
        try:
            sig = await parse_image_signal(ev.image_data, ev.text)
        except Exception as e:
            await alert(f"Image parse failed for {ev.channel_id}:{ev.message_id}\n{e}")
            raise HTTPException(502, f"image parse failed: {e}")
    elif ev.text.strip():
        log.info("Routing msg %s to TEXT parser", ev.message_id)
        try:
            sig = await parse_text_signal(ev.text)
        except Exception as e:
            await alert(f"Text parse failed for {ev.channel_id}:{ev.message_id}\n{e}")
            raise HTTPException(502, f"parse failed: {e}")
    else:
        return {"status": "ignored", "reason": "empty message — no text or image"}

    # Process parsed result
    action = sig.get("action", "IGNORE").upper()
    conf = float(sig.get("confidence", 0))
    log.info("parsed %s:%s -> %s conf=%.2f", ev.channel_id, ev.message_id, action, conf)

    if action == "IGNORE":
        return {"status": "ignored", "signal": sig}

    headers = {"Authorization": f"Bearer {BRIDGE_TOKEN}"}
    async with httpx.AsyncClient(timeout=30) as c:
        if action in ("BUY", "SELL"):
            body = {
                "action": action,
                "symbol": sig.get("symbol", "XAUUSD"),
                "order_type": sig.get("order_type", "market"),
                "entry": sig.get("entry"),
                "sl": sig.get("sl"),
                "tp": sig.get("tp") or [],
                "signal_id": f"{ev.channel_title or ev.channel_id}:{ev.message_id}",
                "comment": ev.channel_title or "signal",
                "from_image": is_image,
            }
            if is_image and body["entry"] is not None:
                # Add 100-pip TP in trade direction as TP1 (closer target)
                hundred_pips = 10.0  # 100 pips × 0.10 for XAUUSD
                extra_tp = body["entry"] - hundred_pips if action == "SELL" else body["entry"] + hundred_pips
                body["tp"] = [round(extra_tp, 2)] + (body["tp"] or [])
                log.info("Added 100-pip TP at %.2f for image signal", extra_tp)
            if body["entry"] is not None and body["order_type"] == "market" and not is_image:
                body["order_type"] = "limit"
                log.info("entry=%.2f specified — converting market->limit", body["entry"])
            r = await c.post(f"{BRIDGE_URL}/trade", json=body, headers=headers)
        else:
            ref = ev.reply_to_id or ev.message_id
            body = {
                "signal_id": f"{ev.channel_id}:{ref}",
                "op": sig.get("manage_op") or "close_all",
                "value": sig.get("partial_pct"),
            }
            r = await c.post(f"{BRIDGE_URL}/manage", json=body, headers=headers)

    if r.status_code >= 400:
        await alert(f"Bridge rejected {ev.channel_id}:{ev.message_id}: {r.status_code} {r.text[:300]}")
    else:
        await alert(f"Executed {action} {sig.get('symbol', '')}: {r.text[:300]}")
    return {"status": "forwarded", "bridge_status": r.status_code, "bridge_body": r.json()}