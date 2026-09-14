"""Telegram listener.
- Logs in as YOUR account (userbot) via Telethon.
- Watches the channels in TG_CHANNELS.
- Pushes every new/edited message to Hermes' webhook immediately.
- Downloads images ONLY for image-enabled channels (IMAGE_CHANNEL_IDS).
- Tracks text hashes to skip unchanged edits (prevents duplicate processing).
- Handles message deletion events for state reconciliation.
"""
import asyncio, os, json, base64, logging, time
from datetime import datetime, timezone
import httpx
from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("listener")

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
CHANNELS = [int(c) for c in os.environ.get("TG_CHANNELS", "").split(",") if c.strip()]
WEBHOOK = os.environ["HERMES_WEBHOOK_URL"]
TOKEN = os.environ["HERMES_WEBHOOK_TOKEN"]
MAX_IMAGE_KB = int(os.environ.get("MAX_IMAGE_KB", 512))

# Only download images for these channels (defined in .env)
IMAGE_CHANNEL_IDS = [
    int(c.strip()) for c in os.environ.get("IMAGE_CHANNEL_IDS", "").split(",") if c.strip()
]

# ── Edit dedup + debounce: track pushed text hashes per message ──
# key=(chat_id, msg_id) -> value=(text_hash, timestamp)
# Only stored on SUCCESSFUL push — prevents signal loss on push failure
# DEBOUNCE_SECONDS: skip subsequent events for same msg_id within this window
DEBOUNCE_SECONDS = 3
last_pushed: dict[tuple[int, int], tuple[int, float]] = {}

# ── Polling fallback ──
# Polls every POLL_INTERVAL seconds per channel. Catches messages the
# event handler may have missed (Telethon subscription fade).
# Duplicate prevention: tracks last_known msg_id per channel + checks
# last_pushed dict — zero LLM cost when no messages are missed.
POLL_INTERVAL = 30
POLL_STATE_FILE = "/data/poll_state.json"  # persisted so last_known survives restarts

def load_poll_state() -> dict[int, int]:
    try:
        with open(POLL_STATE_FILE) as f:
            raw = json.load(f)
            return {int(k): v for k, v in raw.items()}
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}

def save_poll_state(state: dict[int, int]):
    tmp = POLL_STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, POLL_STATE_FILE)

last_polled_msg_id: dict[int, int] = load_poll_state()  # channel_id -> max msg_id seen

client = TelegramClient("/data/tg_session", API_ID, API_HASH)


async def push(payload: dict) -> bool:
    """Push payload to router webhook. Returns True on success, False on dead-letter.
    Creates a fresh httpx client per push attempt to avoid stale TCP keep-alive
    connections that can swallow responses (router HTTP 200 but handler never called).
    Each payload gets a unique request_id for end-to-end traceability.
    """
    import uuid
    request_id = uuid.uuid4().hex[:12]
    payload = {**payload, "request_id": request_id}
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                r = await http.post(WEBHOOK, json=payload,
                                    headers={"Authorization": f"Bearer {TOKEN}"})
                r.raise_for_status()
                body = r.json()
                status = body.get("status", "")
                # Valid router responses carry a known status string.
                # Unexpected status (or phantom 200 with no body) means the
                # handler may have silently dropped the request — retry.
                if status not in ("forwarded", "tp3_hit_cleaned", "delete_processed", "ignored", "skipped"):
                    log.warning("Push returned status=%r (req=%s) — retrying", status, request_id)
                    raise RuntimeError(f"unexpected router status: {status}")
            log.info("Pushed msg %s (%s) req=%s", payload["message_id"], payload["event"], request_id)
            return True
        except Exception as e:
            log.warning("Push attempt %d failed: %s (req=%s)", attempt + 1, e, request_id)
            await asyncio.sleep(2 ** attempt)
    with open("/data/failed_pushes.jsonl", "a") as f:
        f.write(json.dumps(payload) + "\n")
    log.error("DEAD-LETTERED message %s req=%s — check failed_pushes.jsonl", payload["message_id"], request_id)
    return False


async def download_image(event) -> str | None:
    """Download the message's image, return base64 jpeg string, or None."""
    m = event.message
    if not m.media:
        return None
    is_photo = isinstance(m.media, MessageMediaPhoto)
    is_image_doc = (
        isinstance(m.media, MessageMediaDocument)
        and m.media.document
        and any(getattr(a, "mime_type", "") for a in (m.media.document.attributes or []))
        and any(
            getattr(a, "mime_type", "").startswith("image/")
            for a in (m.media.document.attributes or [])
            if hasattr(a, "mime_type")
        )
    )
    if not is_photo and not is_image_doc:
        log.debug("msg %s has media but not an image — skipping download", m.id)
        return None
    try:
        raw = await m.download_media(bytes)
        if raw is None:
            return None
        size_kb = len(raw) / 1024
        if size_kb > MAX_IMAGE_KB:
            log.warning("Image msg %s too large: %.0f KB (limit %s KB) — skipping", m.id, size_kb, MAX_IMAGE_KB)
            return None
        b64 = base64.b64encode(raw).decode("ascii")
        log.info("Downloaded image msg %s: %.0f KB", m.id, size_kb)
        return b64
    except Exception as e:
        log.warning("Failed to download image msg %s: %s", m.id, e)
        return None


async def make_payload(event, kind: str) -> dict:
    m = event.message
    chat = getattr(event, "chat", None)
    payload = {
        "event": kind,
        "channel_id": event.chat_id,
        "channel_title": chat.title if chat else "",
        "message_id": m.id,
        "reply_to_id": m.reply_to_msg_id,
        "text": m.message or "",
        "has_media": m.media is not None,
        "date": m.date.isoformat(),
    }
    # Only download images for channels configured for image signals
    if m.media is not None and event.chat_id in IMAGE_CHANNEL_IDS:
        payload["image_data"] = await download_image(event)
    return payload


# ── Polling helpers ─────────────────────────────────────────


async def make_payload_from_msg(msg, channel_id: int, kind: str) -> dict:
    """Build payload from a Telethon Message object (for polling fallback).
    
    Telethon's get_messages(limit=1) intermittently returns msg.message = ''
    for messages with media even when a text caption exists. When the message
    has media and the text is empty, re-fetch by ID to hydrate the caption.
    """
    try:
        chat = await client.get_entity(channel_id)
        chat_title = chat.title if hasattr(chat, "title") else ""
    except Exception:
        chat_title = ""
    # Hydrate caption: if msg has media but empty text, re-fetch by ID
    text = msg.message or ""
    if not text.strip() and msg.media is not None:
        try:
            entity = await client.get_entity(channel_id)
            hydrated = await client.get_messages(entity, ids=msg.id)
            if hydrated and hydrated.message:
                text = hydrated.message
                log.info("Hydrated caption for msg %s: %.80r", msg.id, text)
        except Exception as e:
            log.debug("Caption re-fetch failed for msg %s: %s", msg.id, e)
    # Also retry text-only messages with empty text (Telethon polling quirk)
    elif not text.strip():
        try:
            entity = await client.get_entity(channel_id)
            refreshed = await client.get_messages(entity, ids=msg.id)
            if refreshed and refreshed.message:
                text = refreshed.message
                log.info("Refreshed text for msg %s: %.80r", msg.id, text)
        except Exception as e:
            log.debug("Text re-fetch failed for msg %s: %s", msg.id, e)
    payload = {
        "event": kind,
        "channel_id": channel_id,
        "channel_title": chat_title,
        "message_id": msg.id,
        "reply_to_id": msg.reply_to_msg_id,
        "text": text,
        "has_media": msg.media is not None,
        "date": msg.date.isoformat(),
    }
    if msg.media is not None and channel_id in IMAGE_CHANNEL_IDS:
        raw = await msg.download_media(bytes)
        if raw and len(raw) / 1024 <= MAX_IMAGE_KB:
            payload["image_data"] = base64.b64encode(raw).decode("ascii")
            log.info("Polling downloaded image msg %s: %.0f KB", msg.id, len(raw) / 1024)
    return payload


async def poll_channels():
    """Periodically poll each channel for messages the event handler may have missed.
    
    Runs alongside the event handlers. Only pushes messages that:
    - Have a higher msg_id than the last known (prevents re-pushing old messages)
    - Are NOT already in last_pushed (prevents duplicating event handler's work)
    
    When the polling detects a gap between last_known and the latest message,
    it fetches ALL missed messages in between — not just the latest — so that
    signals posted between polling cycles are not silently skipped.
    """
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        for channel_id in CHANNELS:
            try:
                entity = await client.get_entity(channel_id)
                msgs = await client.get_messages(entity, limit=1)
                if not msgs:
                    continue
                latest = msgs[0]
                last_known = last_polled_msg_id.get(channel_id, 0)

                if latest.id <= last_known:
                    continue  # no new messages

                # — Gap catch-up: fetch ALL missed messages between last_known and latest —
                missed = []
                if latest.id > last_known + 1:
                    gap_msgs = await client.get_messages(
                        entity, min_id=last_known, max_id=latest.id, limit=50
                    )
                    # Reverse to ascending order (Telethon returns newest-first)
                    gap_msgs.reverse()
                    for gm in gap_msgs:
                        key = (channel_id, gm.id)
                        if key not in last_pushed and gm.id > last_known:
                            missed.append(gm)
                    if missed:
                        log.info("Polling caught %d missed messages in channel %s gap [%s..%s]",
                                 len(missed), channel_id, last_known + 1, latest.id)

                # Process missed messages first (oldest to newest)
                for gm in missed:
                    key = (channel_id, gm.id)
                    payload = await make_payload_from_msg(gm, channel_id, "new")
                    if await push(payload):
                        last_pushed[key] = (hash(gm.message or ""), time.time())
                        last_polled_msg_id[channel_id] = gm.id
                        save_poll_state(last_polled_msg_id)

                # Process the latest message (skip if already handled by event handler)
                key = (channel_id, latest.id)
                if key in last_pushed:
                    # Event handler already processed — just update poll tracker
                    last_polled_msg_id[channel_id] = latest.id
                    save_poll_state(last_polled_msg_id)
                    continue

                log.info("Polling caught msg %s in channel %s (last_known=%s)",
                         latest.id, channel_id, last_known)
                payload = await make_payload_from_msg(latest, channel_id, "new")
                if await push(payload):
                    last_pushed[key] = (hash(latest.message or ""), time.time())
                    last_polled_msg_id[channel_id] = latest.id
                    save_poll_state(last_polled_msg_id)
            except Exception as e:
                log.debug("Polling error for channel %s: %s", channel_id, e)


if CHANNELS:
    @client.on(events.NewMessage(chats=CHANNELS))
    async def on_new(event):
        key = (event.chat_id, event.message.id)
        # Debounce: skip if same msg_id was processed within DEBOUNCE_SECONDS
        if key in last_pushed and time.time() - last_pushed[key][1] < DEBOUNCE_SECONDS:
            log.debug("Debounce: skipping new msg %s (processed %.1fs ago)",
                      event.message.id, time.time() - last_pushed[key][1])
            return
        payload = await make_payload(event, "new")
        if await push(payload):
            last_pushed[key] = (hash(event.message.message or ""), time.time())

    @client.on(events.MessageEdited(chats=CHANNELS))
    async def on_edit(event):
        key = (event.chat_id, event.message.id)
        # Debounce: skip if same msg_id was processed within DEBOUNCE_SECONDS
        if key in last_pushed and time.time() - last_pushed[key][1] < DEBOUNCE_SECONDS:
            log.debug("Debounce: skipping edit msg %s (processed %.1fs ago)",
                      event.message.id, time.time() - last_pushed[key][1])
            return
        # Skip if text hasn't changed (Telegram auto-formatting edits)
        current_hash = hash(event.message.message or "")
        if key in last_pushed and last_pushed[key][0] == current_hash:
            log.debug("Edit didn't change text — skipping msg %s", event.message.id)
            return
        payload = await make_payload(event, "edit")
        if await push(payload):
            last_pushed[key] = (current_hash, time.time())

    @client.on(events.MessageDeleted(chats=CHANNELS))
    async def on_delete(event):
        for msg_id in event.deleted_ids:
            log.info("Message %s deleted in chat %s", msg_id, event.chat_id)
            payload = {
                "event": "delete",
                "channel_id": event.chat_id,
                "message_id": msg_id,
                "date": datetime.now(timezone.utc).isoformat(),
            }
            await push(payload)  # no hash tracking needed for deletes

else:
    @client.on(events.NewMessage())
    async def discover(event):
        log.info("DISCOVERY | chat_id=%s | title=%r | text=%.60r",
                 event.chat_id, getattr(event.chat, "title", "?"), event.message.message)


async def main():
    await client.start()
    me = await client.get_me()
    log.info("Logged in as %s. Channels: %s %s", me.first_name, CHANNELS,
             "(DISCOVERY MODE — set TG_CHANNELS in .env)" if not CHANNELS else "")
    if CHANNELS:
        asyncio.create_task(poll_channels())
        log.info("Polling fallback started (every %s sec)", POLL_INTERVAL)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())