"""
Signal Router — listener webhook -> LLM parse -> MT5 bridge.
v3:
  - Regex pre-filter for known signal formats (Distinct Forex, BotX) — zero LLM cost
  - Per-channel state machine for reconciliation
  - /close_channel instead of /cancel_pending (closes positions + cancels orders)
  - Delete event handler for reverted signals
  - TP type coercion (float -> list) to prevent Pydantic 422
"""
import os, json, re, logging, time
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from typing import Optional
import httpx

from channel_state import channel_state

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
IMAGE_CHANNEL_IDS = [int(c.strip()) for c in os.environ.get("IMAGE_CHANNEL_IDS", "").split(",") if c.strip()]

# ── Prompts ─────────────────────────────────────────────────────

TEXT_SYSTEM_PROMPT = """You are a trade-signal parser. Input: a raw Telegram message from a signals channel.
Output: ONLY a single JSON object, no prose, no markdown fences.
Schema:
{"action":"BUY|SELL|CLOSE|MODIFY|IGNORE","symbol":"...","order_type":"market|limit|stop",
 "entry":number|null,"sl":number|null,"tp":[numbers],"manage_op":"close_all|close_partial|sl_to_be|modify_sl"|null,
 "partial_pct":number|null,"confidence":0.0-1.0}
Rules:
- Normalize symbols: GOLD/XAU -> XAUUSD, DOW -> US30, NAS -> NAS100. Uppercase.
- Commentary, recaps, ads, results screenshots, greetings, "Good morning", "XAU/USD ANALYSIS", market views -> action IGNORE.
- NEVER invent prices. If SL or TP is missing for BUY/SELL, set confidence <= 0.3.
- "close", "take profit now", "secure partials", "SL to breakeven", "move SL to X" -> CLOSE or MODIFY with manage_op.
- Common signal format: "Entry: X", "Stop Loss: X" (or "SL: X"), "TP1: X" (or "Target: X", "TP: X"). Parse these correctly.
- For structured text signals with labeled fields (Entry:, Stop Loss:, TP:), extract the exact numbers after each label.
- confidence reflects how certain you are the parse is a real, complete, actionable signal.
- A valid BUY/SELL signal MUST have both SL and TP. Without both, it is incomplete."""

IMAGE_SYSTEM_PROMPT = """You are a trade-signal parser for CHART IMAGES. Input: a screenshot of a trading chart with two adjacent shaded zones.
Output: ONLY a single JSON object, no prose, no markdown fences.
Schema:
{"action":"BUY|SELL|IGNORE","symbol":"XAUUSD","order_type":"limit","entry":number,"sl":number,"tp":[number],"confidence":0.0-1.0}

Rules:
- The chart shows XAUUSD (Gold). Always output symbol "XAUUSD" and order_type "limit".
- Genuine trade signals contain bullish/bearish directional keywords in the image or caption (e.g. "BULLISH SETUP", "BEARISH SETUP", "VIEW BULLISH", "VIEW BEARISH", "BULLISH", "BEARISH", "BUY SETUP", "SELL SETUP", or any text clearly indicating a trade signal direction). If no directional keyword is present, this is likely a results screenshot or market commentary — set confidence <= 0.3 and action IGNORE.
- The chart has two adjacent shaded zones separated by a horizontal border line:
  * RED shaded zone = Stop Loss zone. The SL price is at the FAR edge of the red zone (the side opposite the entry border).
  * Grey shaded zone = Trade direction / profit zone. The trade moves in this direction.
- The ENTRY price is the horizontal border line between the RED zone and the Grey zone.
- Take Profit (TP): The far boundary line of the Grey zone. Output only ONE TP price.
- Determine direction:
  * If RED zone is ABOVE and Grey zone is BELOW the entry -> action = SELL
  * If Grey zone is ABOVE and RED zone is BELOW the entry -> action = BUY
- Read the exact price labels on the Y-axis for entry, SL, and TP levels.
- For accuracy: the right panel shows boxed price indicators.
  - SL price = the RED box in the right panel, horizontally aligned with the far edge of the RED zone
  - ENTRY price = the GREY box in the right panel, horizontally aligned with the border line between the RED zone and the Grey zone
  - TP price = the GREY box in the right panel, horizontally aligned with the far boundary of the Grey zone
- There may be multiple boxed prices on the right panel. Only use the ones matching the alignment rules above. Ignore all other boxes.
- Do not guess prices from the right panel labels. The boxes aligned with the zone boundaries are the true SL, ENTRY, and TP prices.
- NEVER invent prices. Only report levels you can clearly read from chart labels.
- A valid signal MUST have a clearly readable entry, SL, and TP. If any is missing/unreadable, set confidence <= 0.3.
- A valid trading signal should have SL at least 50 pips (5.0 points) away from entry. If the SL is very close to entry (<50 pips), it is likely a results screenshot or filled trade — set confidence <= 0.3 and action IGNORE.
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
    image_data: Optional[str] = None
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


# ── Regex pre-filter for known text formats ─────────────────────

def parse_with_regex(text: str) -> dict | None:
    """Try to parse known text signal formats with regex.
    Returns None if no match — caller should fall back to LLM.
    """
    # Guard: result/recap messages contain hit/profit keywords — let LLM handle
    if re.search(r'(HIT|✅|❌|😄|💯|PROFIT)', text):
        return None

    # — Distinct Forex format: #XAUUSD BUY 4000 \n\n TP 4004 \n TP 4008 ... \n\n SL 3990 —
    m = re.search(r'#?XAUUSD\s+(BUY|SELL)\s+(\d+\.?\d*)', text)
    if m:
        action = m.group(1)
        entry = float(m.group(2))
        tps = [float(x) for x in re.findall(r'TP\s+(\d+\.?\d*)', text)]
        sl_m = re.search(r'SL\s+(\d+\.?\d*)', text)
        sl = float(sl_m.group(1)) if sl_m else None
        if sl and tps:
            return {
                "action": action, "symbol": "XAUUSD",
                "order_type": "limit", "entry": entry,
                "sl": sl, "tp": tps, "confidence": 1.0,
            }
        return {"action": action, "confidence": 0.3}

    # — BotX format: BUY XAUUSD \n ENTRY: 4120 \n SL: 4110 \n TP: 4137 —
    m = re.search(r'(BUY|SELL)\s+XAUUSD', text)
    if m:
        action = m.group(1)
        entry_m = re.search(r'ENTRY:\s*(\d+\.?\d*)', text)
        sl_m = re.search(r'SL:\s*(\d+\.?\d*)', text)
        tps = [float(x) for x in re.findall(r'TP\d?:?\s*(\d+\.?\d*)', text)]
        entry = float(entry_m.group(1)) if entry_m else None
        sl = float(sl_m.group(1)) if sl_m else None
        if entry and sl and tps:
            return {
                "action": action, "symbol": "XAUUSD",
                "order_type": "limit", "entry": entry,
                "sl": sl, "tp": tps, "confidence": 1.0,
            }
        return {"action": action, "confidence": 0.3}

    return None


# ── LLM parsers ────────────────────────────────────────────────

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


async def parse_image_signal(image_b64: str, caption: str = "") -> dict:
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


# ── Endpoints ───────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"ok": True}


@app.post("/webhook")
async def webhook(ev: ListenerEvent, authorization: str = Header(None)):
    if authorization != f"Bearer {WEBHOOK_TOKEN}":
        raise HTTPException(401, "bad token")

    # ===== 1. DELETE EVENT — revert channel state, clean up MT5 =====
    if ev.event == "delete":
        log.info("Delete event: channel=%s msg=%s", ev.channel_id, ev.message_id)
        removed = channel_state.remove_signal_id(str(ev.channel_id), ev.message_id)
        if removed:
            title = channel_state.get_channel_title(str(ev.channel_id))
            comment = title or str(ev.channel_id)
            headers = {"Authorization": f"Bearer {BRIDGE_TOKEN}"}
            async with httpx.AsyncClient(timeout=15) as c:
                await c.post(
                    f"{BRIDGE_URL}/close_channel",
                    json={"comment": comment},
                    headers=headers,
                )
            log.info("Cleaned up MT5 for channel %s after signal deletion", ev.channel_id)
        return {"status": "delete_processed"}

    # ===== 2. ROUTE TO PARSER =====
    is_image = False
    has_image = ev.image_data and len(ev.image_data) > 100
    is_image_channel = ev.channel_id in IMAGE_CHANNEL_IDS

    if has_image and is_image_channel:
        is_image = True
        log.info("Routing msg %s to VISION parser (image channel, %s b64 chars)",
                 ev.message_id, len(ev.image_data))
        try:
            sig = await parse_image_signal(ev.image_data, ev.text)
        except Exception as e:
            await alert(f"Image parse failed for {ev.channel_id}:{ev.message_id}\n{e}")
            raise HTTPException(502, f"image parse failed: {e}")

    elif ev.text.strip():
        if has_image and not is_image_channel:
            log.info("Channel %s has image but not in IMAGE_CHANNEL_IDS — routing to TEXT parser",
                     ev.channel_id)
        log.info("Routing msg %s to TEXT parser", ev.message_id)

        # Try regex pre-filter first (zero LLM cost)
        sig = parse_with_regex(ev.text)
        if sig:
            log.info("Regex parsed %s:%s -> %s conf=%.2f",
                     ev.channel_id, ev.message_id, sig.get("action", "?"), sig.get("confidence", 0))
        else:
            try:
                sig = await parse_text_signal(ev.text)
            except Exception as e:
                await alert(f"Text parse failed for {ev.channel_id}:{ev.message_id}\n{e}")
                raise HTTPException(502, f"parse failed: {e}")

    else:
        return {"status": "ignored", "reason": "empty message — no text or image"}

    # ===== 3. PROCESS PARSED RESULT =====
    action = sig.get("action", "IGNORE").upper()
    conf = float(sig.get("confidence", 0))
    log.info("parsed %s:%s -> %s conf=%.2f", ev.channel_id, ev.message_id, action, conf)

    if action == "IGNORE":
        return {"status": "ignored", "signal": sig}

    if action in ("BUY", "SELL"):
        if sig.get("sl") is None:
            log.warning("Rejected %s:%s — SL missing, conf=%.2f", ev.channel_id, ev.message_id, conf)
            return {"status": "ignored", "reason": "SL missing from signal"}
        if conf < 0.5:
            log.warning("Rejected %s:%s — confidence %.2f below 0.5", ev.channel_id, ev.message_id, conf)
            return {"status": "ignored", "reason": f"confidence {conf:.2f} below 0.5"}

    if is_image and action in ("BUY", "SELL"):
        symbol = sig.get("symbol", "XAUUSD")
        pip_sizes = {"XAUUSD": 0.10, "US30": 1.0, "NAS100": 1.0}
        pip_size = pip_sizes.get(symbol, 0.10)
        sl_distance = abs(sig["entry"] - sig["sl"]) / pip_size
        if sl_distance < 50:
            log.warning("Rejected image signal %s:%s — SL only %.0f pips from entry (min 50)",
                        ev.channel_id, ev.message_id, sl_distance)
            await alert(f"Trade.inn signal rejected: SL too tight ({sl_distance:.0f} pips, min 50)")
            return {"status": "ignored", "reason": f"SL too tight: {sl_distance:.0f} pips (min 50)"}

    # ===== 4. FORWARD TO BRIDGE =====
    headers = {"Authorization": f"Bearer {BRIDGE_TOKEN}"}
    async with httpx.AsyncClient(timeout=30) as c:
        if action in ("BUY", "SELL"):
            # --- a) Close existing positions + cancel pending orders for this channel ---
            close_body = {"comment": ev.channel_title or "signal"}
            try:
                close_resp = await c.post(f"{BRIDGE_URL}/close_channel", json=close_body,
                                          headers=headers, timeout=15)
                if close_resp.status_code < 400:
                    result = close_resp.json()
                    closed = result.get("closed", [])
                    cancelled = result.get("cancelled", [])
                    if closed or cancelled:
                        log.info("Closed %d positions + cancelled %d orders for '%s' before new signal",
                                 len(closed), len(cancelled), ev.channel_title)
            except Exception as e:
                log.warning("close_channel call failed (non-fatal): %s", e)

            # --- b) Build trade body with TP coercion ---
            raw_tp = sig.get("tp")
            if isinstance(raw_tp, (int, float)):
                raw_tp = [raw_tp]

            body = {
                "action": action,
                "symbol": sig.get("symbol", "XAUUSD"),
                "order_type": sig.get("order_type", "market"),
                "entry": sig.get("entry"),
                "sl": sig.get("sl"),
                "tp": raw_tp or [],
                "signal_id": f"{ev.channel_title or ev.channel_id}:{ev.message_id}",
                "comment": ev.channel_title or "signal",
                "from_image": is_image,
            }

            if is_image and body["entry"] is not None:
                hundred_pips = 10.0
                extra_tp = body["entry"] - hundred_pips if action == "SELL" else body["entry"] + hundred_pips
                body["tp"] = [round(extra_tp, 2)] + (body["tp"] or [])
                log.info("Added 100-pip TP at %.2f for image signal", extra_tp)

            if body["entry"] is not None and body["order_type"] == "market" and not is_image:
                body["order_type"] = "limit"
                log.info("entry=%.2f specified — converting market->limit", body["entry"])

            # --- c) Send trade ---
            r = await c.post(f"{BRIDGE_URL}/trade", json=body, headers=headers)

        else:
            ref = ev.reply_to_id or ev.message_id
            body = {
                "signal_id": f"{ev.channel_id}:{ref}",
                "op": sig.get("manage_op") or "close_all",
                "value": sig.get("partial_pct"),
            }
            r = await c.post(f"{BRIDGE_URL}/manage", json=body, headers=headers)

    # ===== 5. ALERT + STATE UPDATE =====
    if r.status_code >= 400:
        await alert(f"Bridge rejected {ev.channel_id}:{ev.message_id}: {r.status_code} {r.text[:300]}")
    else:
        await alert(f"Executed {action} {sig.get('symbol', '')}: {r.text[:300]}")
        if action in ("BUY", "SELL"):
            channel_state.update(str(ev.channel_id), {
                "msg_id": ev.message_id,
                "channel_title": ev.channel_title or str(ev.channel_id),
                "action": action,
                "entry": sig.get("entry"),
                "sl": sig.get("sl"),
                "tp": sig.get("tp", []),
                "order_type": sig.get("order_type", "market"),
                "from_image": is_image,
                "timestamp": ev.date,
            })

    return {"status": "forwarded", "bridge_status": r.status_code, "bridge_body": r.json()}