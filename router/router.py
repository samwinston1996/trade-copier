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

# Channel IDs for special handling
AK_CAPITAL_ID = -1001519298737
STAR_FX_ID = -1001916021658
XAUUSD_ANALYSIS_ID = -1002110392082

# ── Prompts ─────────────────────────────────────────────────────

TEXT_SYSTEM_PROMPT = """You are a trade-signal parser. Input: a raw Telegram message from a signals channel.
Output: ONLY a single JSON object, no prose, no markdown fences.
Schema:
{"action":"BUY|SELL|CLOSE|MODIFY|IGNORE","symbol":"...","order_type":"market|limit|stop",
 "entry":number|null,"sl":number|null,"tp":[numbers],"manage_op":"close_all|close_partial|close_and_be|sl_to_be|modify_sl"|null,
 "partial_pct":number|null,"confidence":0.0-1.0}
Rules:
- Normalize symbols: GOLD/XAU -> XAUUSD, DOW -> US30, NAS -> NAS100. Uppercase.
- Commentary, recaps, ads, results screenshots, greetings, "Good morning", "XAU/USD ANALYSIS", market views -> action IGNORE.
- NEVER invent prices. If SL or TP is missing for BUY/SELL, set confidence <= 0.3.
- "close", "take profit now", "secure partials", "SL to breakeven", "move SL to X" -> CLOSE or MODIFY with manage_op.
- BREAKEVEN DETECTION: Messages like "set breakeven", "use breakeven", "move SL to breakeven", "secure your profit & move SL to BE", "secure X% profits by closing partial", "book profit", "move to breakeven" → action=MODIFY, manage_op="close_and_be", partial_pct=50. This books 50% profit and moves remaining SL to entry.
- Common signal format: "Entry: X", "Stop Loss: X" (or "SL: X"), "TP1: X" (or "Target: X", "TP: X"). Parse these correctly.
- Handle emoji-prefixed fields: "💎TP¹ 4092" → TP=4092, "#⃣SL 4073" → SL=4073, "💻XAUUSD BUY 4088" → BUY entry=4088
- For structured text signals with labeled fields (Entry:, Stop Loss:, TP:), extract the exact numbers after each label.
- confidence reflects how certain you are the parse is a real, complete, actionable signal.
|- A valid BUY/SELL signal MUST have both SL and TP. Without both, it is incomplete.
|- "Sell Now @ X - Y" or "Buy Now @ X - Y" → action=SELL/BUY. entry = MIDPOINT of the range. Never return None for entry.
|- "Gold Sell Now @ X - Y" or "Gold Buy Now @ X - Y" → as above, symbol = XAUUSD.
|- "TP1: X" (numeric) → valid first TP. "TP2: later" or any non-numeric TP → ignore that line, treat as single-TP signal.
|- A signal with a valid numeric SL and at least one valid numeric TP is complete and actionable."""

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
    request_id: str = ""


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

def parse_with_regex(text: str, channel_id: int = 0) -> dict | None:
    """Try to parse known text signal formats with regex.
    Returns None if no match — caller should fall back to LLM.
    """
    # Guard: result/recap messages contain hit/profit keywords — let LLM handle
    # NOTE: ✅ is NOT blocked — StarFx and XAUUSD use it as a bullet in signal messages
    if re.search(r'(HIT|❌|😄|💯|PROFIT)', text):
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
        return None  # partial match — let LLM fallback handle

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
        return None  # partial match — let LLM fallback handle

    # — Paul GOLD Dominator: "Gold Sell Now @ 4377 - 4385 / SL: 4387 / TP1: 4367 / TP2: 4357" —
    if channel_id == -1001918089511:
        m = re.search(r'(?:Gold\s+)?(?:Sell|Buy)\s+Now\s*@\s*(\d+\.?\d*)\s*[-–]\s*(\d+\.?\d*)', text, re.I)
        if m:
            lo, hi = float(m.group(1)), float(m.group(2))
            entry = round((lo + hi) / 2, 2)  # midpoint — deterministic
            sl_m = re.search(r'SL:?\s*(\d+\.?\d*)', text)
            tps = [float(x) for x in re.findall(r'TP\d?:?\s*(\d+\.?\d*)', text)]
            if sl_m and tps:
                action = "SELL" if re.search(r'Sell', text, re.I) else "BUY"
                return {
                    "action": action, "symbol": "XAUUSD",
                    "order_type": "limit", "entry": entry,
                    "sl": float(sl_m.group(1)), "tp": tps,
                    "confidence": 1.0,
                }
            return None  # partial → LLM fallback

        # — Paul GOLD Breakeven: "set breakeven to minimize risk"
        m = re.search(r'set breakeven to minimize risk', text, re.I)
        if m:
            return {
                "action": "MODIFY", "symbol": "XAUUSD",
                "manage_op": "close_and_be",
                "partial_pct": 50,
                "confidence": 1.0,
            }

    # — AK Capital Pending TP (Phase 1): "xauusd sell 4482-4484 | sl 4492" or "Xauusd sell 4670-72-73 | sl 4681"
    #   Entry as range, SL present, TP follows in a separate message.
    if channel_id == AK_CAPITAL_ID:
        m = re.search(r'xauusd\s+(buy|sell)(?:\s+now)?\s+([\d\-\s]+)\s*(?:\||\n)\s*sl\s+(\d+)', text, re.I)
        if m:
            action = m.group(1).upper()
            prices_str = m.group(2).strip()
            sl = float(m.group(3))
            # Parse price range — handles "4482-4484", "4670-72-73", "4583-4584-4586"
            parts = [p for p in re.split(r'[\-\s]+', prices_str) if p]
            full_prices = []
            prefix = None
            for p in parts:
                if len(p) >= 4:
                    full_prices.append(float(p))
                    prefix = p[:2]
                elif prefix:
                    full_prices.append(float(prefix + p))
            if full_prices:
                lo, hi = min(full_prices), max(full_prices)
                entry = round((lo + hi) / 2, 2) if len(full_prices) > 1 else lo
                # Only handle if TP is NOT already in the message
                has_tp = bool(re.findall(r'TP\d?:?\s*(\d+\.?\d*)', text, re.I))
                if not has_tp:
                    # Use MARKET order — bridge will calculate safe temp TP from actual fill
                    return {
                        "action": action, "symbol": "XAUUSD",
                        "order_type": "market", "entry": None,
                        "sl": sl, "tp": [],
                        "confidence": 1.0, "pending_tp": True,
                    }

        # — AK Capital Phase 1 (SL-less): "XAUUSD BUY NOW 4333-4330-4328" or "Xauusd buy now 4305-4303"
        #   Entry range only, NO SL in message. SL auto-generated by bridge (pending_tp auto SL).
        m = re.search(r'xauusd\s+(buy|sell)\s+now\s+([\d][\d\-\s]*)', text, re.I)
        if m:
            action = m.group(1).upper()
            prices_str = m.group(2).strip()
            # Verify NO explicit SL or TP in the text
            has_sl = bool(re.search(r'\bsl\b', text, re.I))
            has_tp = bool(re.findall(r'TP\d?:?\s*(\d+\.?\d*)', text, re.I))
            if not has_sl and not has_tp:
                parts = [p for p in re.split(r'[\-\s]+', prices_str) if p]
                full_prices = []
                prefix = None
                for p in parts:
                    if len(p) >= 4:
                        full_prices.append(float(p))
                        prefix = p[:2]
                    elif prefix:
                        full_prices.append(float(prefix + p))
                if full_prices:
                    lo, hi = min(full_prices), max(full_prices)
                    entry = round((lo + hi) / 2, 2) if len(full_prices) > 1 else lo
                    return {
                        "action": action, "symbol": "XAUUSD",
                        "order_type": "market", "entry": None,
                        "sl": None, "tp": [],
                        "confidence": 1.0, "pending_tp": True,
                    }

        # — AK Capital TP follow-up (Phase 2): "tp1 4455 | tp2 4435" or "tp1 4550 | to2 4535 | target 4520"
        m = re.search(r'tp1?\s*(\d+\.?\d*)\s*\|\s*t[op]2?\s*(\d+\.?\d*)', text, re.I)
        if m:
            tp1 = float(m.group(1))
            tp2 = float(m.group(2))
            tps = [tp1, tp2]
            # Optional third level: "target 4520"
            t3 = re.search(r'target\s+(\d+\.?\d*)', text, re.I)
            if t3:
                tps.append(float(t3.group(1)))
            return {
                "action": "MODIFY", "symbol": "XAUUSD",
                "manage_op": "modify_tp",
                "tp": tps, "confidence": 1.0,
            }

        # — AK Capital TP follow-up (Phase 2 alt format): no pipe, e.g. "tp1 4595 tp2 4610 target 4635" or "TP 4610 TP 4625"
        m = re.search(r'(?:^|\btp\b|\btp1\b|\bT\.P\b)\s*:?\s*(\d+\.?\d*)(?:[^\d]*?\btp\s*:?\s*(\d+\.?\d*)|\s*[–\-]\s*(\d+\.?\d*))', text, re.I)
        if m:
            tps = [float(x) for x in m.groups() if x is not None][:2]
            t3 = re.search(r'target\s+(\d+\.?\d*)', text, re.I)
            if t3:
                tps.append(float(t3.group(1)))
            if len(tps) >= 2:
                return {
                    "action": "MODIFY", "symbol": "XAUUSD",
                    "manage_op": "modify_tp",
                    "tp": tps, "confidence": 1.0,
                }

        # — AK Capital Breakeven: "set breakeven", "use breakeven", "ser breakeven", "set breakevem",
        #   "Stop Loss moved to BREAKEVEN", "SL moved to breakeven", "move to breakeven"
        #   Matches actionable instructions (NOT notifications like "breakeven hitttt" or "Breakeven Hit")
        #   Excludes "not set breakeven" (past-tense advisory, not an instruction)
        if re.search(r'not\s+set\s+breakeve', text, re.I):
            return None
        m = re.search(
            r'(?:use|set|ser|move[sd]?\s+to|sl\s+to)\s+breakeve', text, re.I
        )
        if m:
            return {
                "action": "MODIFY", "symbol": "XAUUSD",
                "manage_op": "close_and_be",
                "partial_pct": 50,
                "confidence": 1.0,
            }

    # — StarFx Phase 2 (full signal): "GOLD BUY NOW 4632-4628📊\n😮SL 4623\n✅TP1 4634\n✅TP2 4638\n✅TP3 4643"
    if channel_id == STAR_FX_ID:
        m = re.search(r'GOLD\s+(BUY|SELL)\s+NOW\s+(\d+\.?\d*)\s*[-–]\s*(\d+\.?\d*)', text, re.I)
        if m:
            action = m.group(1).upper()
            lo, hi = float(m.group(2)), float(m.group(3))
            entry = round((lo + hi) / 2, 2)
            sl_m = re.search(r'SL\s+(\d+\.?\d*)', text, re.I)
            tps = [float(x) for x in re.findall(r'TP\d?\s*(\d+\.?\d*)', text, re.I)]
            if sl_m and len(tps) >= 3:
                return {
                    "action": "MODIFY", "symbol": "XAUUSD",
                    "manage_op": "modify_signal",
                    "entry": entry, "sl": float(sl_m.group(1)),
                    "tp": tps[:3], "confidence": 1.0,
                    "order_type": "limit",
                }

        # — StarFx Phase 1 (quick entry): "Gold buy now" or "Gold sell now" (exact or with entry price)
        m = re.search(r'^gold\s+(buy|sell)\s+now(?:\s*@\s*\d+\.?\d*)?\s*$', text, re.I)
        if m:
            action = m.group(1).upper()
            return {
                "action": action, "symbol": "XAUUSD",
                "order_type": "market", "entry": None,
                "sl": None, "tp": [],
                "confidence": 1.0, "pending_tp": True,
            }

        # — StarFx Breakeven: "Secure your profit & move SLto BE"
        m = re.search(r'(?:Secure your profit|move SLto BE)', text, re.I)
        if m:
            return {
                "action": "MODIFY", "symbol": "XAUUSD",
                "manage_op": "close_and_be",
                "partial_pct": 50,
                "confidence": 1.0,
            }

    # — XAUUSD Analysis Team Phase 2 (full signal): "XAUUSD BUY Signal\nEntry: 4636\nTP1: 4641\nTP2: 4646\nTP3: 4651\nStop Loss: 4626"
    if channel_id == XAUUSD_ANALYSIS_ID:
        m = re.search(r'XAUUSD\s+(BUY|SELL)\s+SIGNAL', text, re.I)
        if m:
            action = m.group(1).upper()
            entry_m = re.search(r'Entry:\s*(\d+\.?\d*)', text)
            sl_m = re.search(r'Stop Loss:\s*(\d+\.?\d*)', text)
            tps = [float(x) for x in re.findall(r'TP\d:\s*(\d+\.?\d*)', text)]
            if entry_m and sl_m and len(tps) >= 3:
                return {
                    "action": "MODIFY", "symbol": "XAUUSD",
                    "manage_op": "modify_signal",
                    "entry": float(entry_m.group(1)),
                    "sl": float(sl_m.group(1)),
                    "tp": tps[:3], "confidence": 1.0,
                    "order_type": "limit",
                }

        # — XAUUSD Analysis Team Phase 1 (quick entry): "XAUUSD BUY NOW" (exact, no prices)
        m = re.search(r'^XAUUSD\s+(BUY|SELL)\s+NOW$', text, re.I)
        if m:
            action = m.group(1).upper()
            return {
                "action": action, "symbol": "XAUUSD",
                "order_type": "market", "entry": None,
                "sl": None, "tp": [],
                "confidence": 1.0, "pending_tp": True,
            }

    # — Trading Central Breakeven: "secure 30% profits by closing partial lotsize" or "First take profit target is hit" + partial close
    if channel_id == -1002439412709:
        m = re.search(r'secure.*?(?:\d+)%.*?profit.*?clos', text, re.I)
        if m:
            return {
                "action": "MODIFY", "symbol": "XAUUSD",
                "manage_op": "close_and_be",
                "partial_pct": 50,
                "confidence": 1.0,
            }

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

    log.info("Webhook: event=%s msg=%s channel=%s req=%s",
             ev.event, ev.message_id, ev.channel_id, ev.request_id)

    # Strip channel_title at source — MT5 rejects comments with trailing whitespace
    ch_title = (ev.channel_title or "").strip()
    ch_source = ch_title or ev.channel_id  # non-empty string for comment/ID use

    # ===== 1. DELETE EVENT — revert channel state, clean up MT5 =====
    if ev.event == "delete":
        log.info("Delete event: channel=%s msg=%s", ev.channel_id, ev.message_id)
        removed = channel_state.remove_signal_id(str(ev.channel_id), ev.message_id)
        if removed:
            comment = ch_source
            headers = {"Authorization": f"Bearer {BRIDGE_TOKEN}"}
            async with httpx.AsyncClient(timeout=15) as c:
                await c.post(
                    f"{BRIDGE_URL}/close_channel",
                    json={"comment": comment},
                    headers=headers,
                )
            log.info("Cleaned up MT5 for channel %s after signal deletion", ev.channel_id)
        return {"status": "delete_processed"}

    # ===== 1.5 TP3 HIT DETECTION — cancel pending orders =====
    # Case-insensitive. Handles typo variants: HIT, HlT (L instead of I), H1T (1 instead of I)
    TP3_HIT_PATTERN = re.compile(
        r'(?:TP\s*3\s*H[I1lL]T|TP3[👏😡🔥]|TP\s*³\s*H[I1lL]T|TP\s*3\s*ALL|ALL\s*TP)',
        re.I
    )
    if ev.text and TP3_HIT_PATTERN.search(ev.text):
        current = channel_state.current_signal(str(ev.channel_id))
        if current and len(current.get("tp", [])) >= 3:
            log.info("TP3 hit for %s:%s — booking profit + cancelling pending for '%s'",
                     ev.channel_id, ev.message_id, ch_title)
            headers = {"Authorization": f"Bearer {BRIDGE_TOKEN}"}
            async with httpx.AsyncClient(timeout=15) as c:
                # Step 1: Secure profit + move remaining SL to breakeven
                # close_and_be books 50% remaining lot, moves SL to entry.
                # If all lots already closed by TPs, this is a no-op.
                await c.post(
                    f"{BRIDGE_URL}/manage",
                    json={"comment": ch_title or str(ev.channel_id),
                          "op": "close_and_be", "value": 50},
                    headers=headers,
                )
                # Step 2: Cancel remaining pending orders (unfilled limit/stop orders)
                await c.post(
                    f"{BRIDGE_URL}/close_channel",
                    json={"comment": ch_title or str(ev.channel_id),
                          "pending_only": True},
                    headers=headers,
                )
            await alert(f"TP3 hit — booked profit + cancelled pending for {ch_title}: {ev.text[:200]}")
            return {"status": "tp3_hit_cleaned", "channel": ch_title}

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
        log.info("Routing msg %s from channel %s to TEXT parser", ev.message_id, ev.channel_id)
        if ev.channel_id == -1001918089511:
            log.info("=== PAUL DEBUG === text=%s", ev.text[:500])

        # Try regex pre-filter first (zero LLM cost)
        sig = parse_with_regex(ev.text, ev.channel_id)
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
        log.warning("Empty message for channel %s msg %s — no text, no image, no payload data",
                     ev.channel_id, ev.message_id)
        return {"status": "ignored", "reason": "empty message — no text or image"}

    # ===== 3. PROCESS PARSED RESULT =====
    action = sig.get("action", "IGNORE").upper()
    conf = float(sig.get("confidence", 0))
    log.info("parsed %s:%s -> %s conf=%.2f", ev.channel_id, ev.message_id, action, conf)

    # ── AK Capital Phase 1: signals with "Tp will update" → force pending_tp ──
    if ev.channel_id == AK_CAPITAL_ID and action in ("BUY", "SELL"):
        tp_list = sig.get("tp", [])
        if isinstance(tp_list, (int, float)):
            tp_list = [tp_list]
        has_tp = bool(tp_list) and not all(t == 0 for t in tp_list)
        text_lower = (ev.text or "").lower()
        if not has_tp and re.search(r'(tp will update|tp later|tp follow|tp soon)', text_lower):
            sig["pending_tp"] = True
            if conf < 0.7:
                conf = 0.7
                sig["confidence"] = 0.7
            log.info("AK Capital Phase 1 for %s:%s — pending_tp=True (TP to follow later)",
                     ev.channel_id, ev.message_id)

    if action == "IGNORE":
        return {"status": "ignored", "signal": sig}

    if action in ("BUY", "SELL"):
        is_pending = sig.get("pending_tp", False)
        if sig.get("sl") is None and not is_pending:
            log.warning("Rejected %s:%s — SL missing, conf=%.2f", ev.channel_id, ev.message_id, conf)
            return {"status": "ignored", "reason": "SL missing from signal"}
        if conf < 0.5 and not is_pending:
            log.warning("Rejected %s:%s — confidence %.2f below 0.5", ev.channel_id, ev.message_id, conf)
            return {"status": "ignored", "reason": f"confidence {conf:.2f} below 0.5"}

    # ── Trade.inn: reject promotional/preview messages containing future-tense language ──
    TRADE_INN_ID = -1001766321748
    if action in ("BUY", "SELL") and ev.channel_id == TRADE_INN_ID:
        lower_text = ev.text.lower()
        if re.search(r'(coming live|coming soon|will discuss|next best|stay tuned|preview)', lower_text):
            log.info("Rejected %s:%s — Trade.inn promotional/preview text", ev.channel_id, ev.message_id)
            await alert(f"Trade.inn preview rejected: {action} with promotional text: {ev.text[:120]}")
            return {"status": "ignored", "reason": "Trade.inn promotional/preview text"}

    # ── Reject single-TP signals for Distinct Forex (marketing) ──
    if action in ("BUY", "SELL") and ev.channel_id == -1001547583661:
        tp_list = sig.get("tp", [])
        if isinstance(tp_list, (int, float)):
            tp_list = [tp_list]
        if len(tp_list) <= 1:
            log.warning("Rejected %s:%s — Distinct Forex marketing (only %d TP)",
                        ev.channel_id, ev.message_id, len(tp_list))
            await alert(f"Distinct Forex marketing rejected: {action} with only {len(tp_list)} TP(s)")
            return {"status": "ignored", "reason": "single-TP marketing signal"}

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

    # ── Signal edit guard: skip edits that haven't changed the trade ──
    # Prevents edit-triggered pre-emptive cancel from killing orders just placed by the
    # original new event. Only proceed with an edit if the signal content actually changed.
    if ev.event == "edit" and action in ("BUY", "SELL"):
        current = channel_state.current_signal(str(ev.channel_id))
        if current and current.get("msg_id") == ev.message_id:
            same_action = current.get("action") == action
            same_entry = abs((current.get("entry") or 0) - (sig.get("entry") or 0)) < 0.01
            same_sl = abs((current.get("sl") or 0) - (sig.get("sl") or 0)) < 0.01
            old_tps = sorted(current.get("tp", []) or [])
            new_tps = sorted(sig.get("tp", []) or [])
            same_tp = old_tps == new_tps
            if same_action and same_entry and same_sl and same_tp:
                log.info("Edit for %s:%s — signal unchanged (same action/entry/SL/TP), ignoring edit",
                         ev.channel_id, ev.message_id)
                return {"status": "ignored", "reason": "edit unchanged"}

    # ===== 4. FORWARD TO BRIDGE =====
    headers = {"Authorization": f"Bearer {BRIDGE_TOKEN}"}
    # Dedup set for manage_ops — prevents the same MODIFY/tp-update from executing
    # multiple times when Telethon delivers the same message repeatedly.
    processed_manage_ops: set = getattr(app, "_processed_manage_ops", set())
    if not hasattr(app, "_processed_manage_ops"):
        app._processed_manage_ops = processed_manage_ops
    async with httpx.AsyncClient(timeout=30) as c:
        if action in ("BUY", "SELL"):
            # --- Msg_id monotonicity guard ---
            # Reject signals with msg_id <= current signal's msg_id (out-of-order delivery)
            current = channel_state.current_signal(str(ev.channel_id))
            if current and ev.message_id <= current.get("msg_id", 0):
                cur_id = current.get("msg_id", 0)
                if cur_id > 500000 and ev.message_id < 100000:
                    log.warning("msg_id rollover for %s: stored=%s, new=%s — accepting signal",
                                ev.channel_id, cur_id, ev.message_id)
                else:
                    log.info("Skipping %s:%s — msg_id %s <= current %s (out of order)",
                             ev.channel_id, ev.message_id, ev.message_id, cur_id)
                    return {"status": "skipped", "reason": f"msg_id {ev.message_id} <= current {cur_id} (out of order)"}

            # --- a) Pre-emptively cancel lingering pending orders from previous signals ---
            # Prevents old pending orders (e.g. BUY LIMIT from 7h ago) from surviving
            # past a new signal. Runs before the trade is placed to avoid race conditions
            # with the post-trade close_channel call.
            if ch_title:
                try:
                    await c.post(
                        f"{BRIDGE_URL}/close_channel",
                        json={"comment": ch_title, "pending_only": True},
                        headers=headers, timeout=10,
                    )
                    log.info("Pre-emptive pending cancel for '%s' before new signal", ch_title)
                except Exception as e:
                    log.warning("Pre-emptive pending cancel failed (non-fatal): %s", e)

            # --- b) Build trade body ---
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
                "pending_tp": sig.get("pending_tp", False),
                "signal_id": f"{ch_source}:{ev.message_id}",
                "comment": ch_title or "signal",
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

            # --- b) Send trade FIRST (race for best price) ---
            r = await c.post(f"{BRIDGE_URL}/trade", json=body, headers=headers)

            # --- c) Extract ticket IDs from the just-placed trade ---
            ticket_ids = []
            is_duplicate = False
            if r.status_code < 400:
                trade_result = r.json()
                if trade_result.get("status") == "executed":
                    ticket_ids = [o.get("order", 0) for o in trade_result.get("orders", []) if o.get("order")]
                elif trade_result.get("status") == "duplicate_ignored":
                    is_duplicate = True
                    log.info("Bridge returned duplicate_ignored for %s:%s — skipping close_channel",
                             ev.channel_id, ev.message_id)
                log.info("Trade placed — tickets: %s", ticket_ids)
            else:
                log.warning("Bridge rejected %s:%s — status=%s body=%s",
                            ev.channel_id, ev.message_id, r.status_code, r.text[:500])

            # --- d) THEN conditionally close old positions + cancel old orders ---
            # Guard (Fix B): skip close_channel if no tickets to exclude (prevents self-cancellation)
            # Guard (Fix B): skip close_channel if bridge said duplicate_ignored
            # Conditional (Fix C): skip close_channel if same direction + same entry (repost/duplicate)
            should_close = bool(ticket_ids) and not is_duplicate
            if should_close and action in ("BUY", "SELL"):
                current = channel_state.current_signal(str(ev.channel_id))
                if current:
                    prev_action = current.get("action")
                    prev_entry = current.get("entry")
                    if prev_action == action and prev_entry == sig.get("entry"):
                        # Same direction, same entry — likely a repost/duplicate from the channel
                        should_close = False
                        log.info("Same signal as previous for %s:%s (same direction+entry) — skipping close_channel",
                                 ev.channel_id, ev.message_id)
                    elif prev_action != action:
                        # Direction changed — this is a reversal, close old trades
                        log.info("Direction change %s→%s for %s:%s — closing old trades",
                                 prev_action, action, ev.channel_id, ev.message_id)

            if should_close:
                close_body = {"comment": ch_title or "signal"}
                close_body["exclude_tickets"] = ticket_ids
                try:
                    close_resp = await c.post(f"{BRIDGE_URL}/close_channel", json=close_body,
                                              headers=headers, timeout=15)
                    if close_resp.status_code < 400:
                        result = close_resp.json()
                        closed = result.get("closed", [])
                        cancelled = result.get("cancelled", [])
                        if closed or cancelled:
                            log.info("Closed %d positions + cancelled %d orders for '%s' after new signal",
                                     len(closed), len(cancelled), ev.channel_title)
                except Exception as e:
                    log.warning("close_channel call failed (non-fatal): %s", e)
            elif not is_duplicate:
                log.info("Skipped close_channel for %s:%s (reason: %s)",
                         ev.channel_id, ev.message_id,
                         "no tickets returned" if not ticket_ids else "same signal as previous")

        # ── Manage-op dedup: same channel+msg+op delivered multiple times → skip ──
        if sig.get("manage_op"):
            dedup_key = f"{ev.channel_id}:{ev.message_id}:{sig['manage_op']}"
            if dedup_key in processed_manage_ops:
                log.info("Skipping %s:%s — %s already processed (dup delivery)",
                         ev.channel_id, ev.message_id, sig["manage_op"])
                return {"status": "skipped", "reason": f"duplicate manage_op: {sig['manage_op']}"}
            processed_manage_ops.add(dedup_key)

        if sig.get("manage_op") == "modify_tp":
            # AK Capital Phase 2: modify existing positions' TPs from temp to real
            pending = channel_state.current_signal(str(ev.channel_id))
            title = pending.get("channel_title", ch_source) if pending else ch_source
            body = {
                "signal_id": f"{ch_source}:{ev.message_id}",
                "comment": title,
                "op": "modify_tp",
                "tp_values": json.dumps(sig.get("tp", [])),
            }
            r = await c.post(f"{BRIDGE_URL}/manage", json=body, headers=headers)

        elif sig.get("manage_op") == "sl_to_be":
            # AK Capital Breakeven — move SL to entry price for all AK Capital positions
            pending = channel_state.current_signal(str(ev.channel_id))
            title = pending.get("channel_title", ch_source) if pending else ch_source
            body = {
                "signal_id": f"{ch_source}:{ev.message_id}",
                "comment": title,
                "op": "sl_to_be",
            }
            r = await c.post(f"{BRIDGE_URL}/manage", json=body, headers=headers)

        elif sig.get("manage_op") == "modify_signal":
            # StarFx/XAUUSD Phase 2: full signal arrived
            pending = channel_state.current_signal(str(ev.channel_id))
            title = pending.get("channel_title", ch_source) if pending else ch_source
            
            if pending and pending.get("tickets", []):
                # Phase 2: modify existing temp trades — set real SL + distribute TPs
                body = {
                    "signal_id": f"{ev.channel_id}:{ev.message_id}",
                    "comment": title,
                    "op": "modify_signal",
                    "value": sig.get("sl"),
                    "tp_values": json.dumps(sig.get("tp", [])),
                }
                r = await c.post(f"{BRIDGE_URL}/manage", json=body, headers=headers)
            else:
                # No temp trades — open 3 fresh trades with the full signal params
                log.info("modify_signal without pending_tp — opening 3 fresh trades for %s:%s",
                         ev.channel_id, ev.message_id)
                # Determine direction from entry vs SL
                entry = sig.get("entry")
                sl = sig.get("sl")
                if entry is not None and sl is not None:
                    action = "BUY" if sl < entry else "SELL"
                body = {
                    "action": action,
                    "symbol": "XAUUSD",
                    "order_type": sig.get("order_type", "limit"),
                    "entry": entry,
                    "sl": sl,
                    "tp": sig.get("tp", []),
                    "signal_id": f"{ev.channel_id}:{ev.message_id}",
                    "comment": title,
                }
                r = await c.post(f"{BRIDGE_URL}/trade", json=body, headers=headers)

        elif sig.get("manage_op") == "close_and_be":
            # Book 50% profit + move remaining SL to breakeven
            pending = channel_state.current_signal(str(ev.channel_id))
            title = pending.get("channel_title", ch_source) if pending else ch_source
            body = {
                "signal_id": f"{ch_source}:{ev.message_id}",
                "comment": title,
                "op": "close_and_be",
                "value": sig.get("partial_pct", 50),
            }
            r = await c.post(f"{BRIDGE_URL}/manage", json=body, headers=headers)

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
            update = {
                "msg_id": ev.message_id,
                "channel_title": ch_title or str(ev.channel_id),
                "action": action,
                "entry": sig.get("entry"),
                "sl": sig.get("sl"),
                "tp": sig.get("tp", []),
                "order_type": sig.get("order_type", "market"),
                "from_image": is_image,
                "timestamp": ev.date,
            }
            # Always save tickets from bridge response — needed for exclude_tickets in close_channel
            if ticket_ids:
                update["tickets"] = ticket_ids
            if sig.get("pending_tp", False):
                update["pending_tp"] = True
            channel_state.update(str(ev.channel_id), update)
        elif sig.get("manage_op") == "modify_tp":
            # Clear pending_tp, store real TP
            current = channel_state.current_signal(str(ev.channel_id))
            if current:
                current["pending_tp"] = False
                current["tp"] = sig.get("tp", [])
                channel_state.update(str(ev.channel_id), current)
        elif sig.get("manage_op") == "modify_signal":
            # StarFx/XAUUSD Phase 2 — clear pending_tp, store real values
            current = channel_state.current_signal(str(ev.channel_id))
            if current and current.get("tickets", []):
                current["pending_tp"] = False
                current["entry"] = sig.get("entry")
                current["sl"] = sig.get("sl")
                current["tp"] = sig.get("tp", [])
                channel_state.update(str(ev.channel_id), current)

    return {"status": "forwarded", "bridge_status": r.status_code, "bridge_body": r.json()}