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

client = TelegramClient("/data/tg_session", API_ID, API_HASH)
http = httpx.AsyncClient(timeout=30)


async def push(payload: dict) -> bool:
    """Push payload to router webhook. Returns True on success, False on dead-letter."""
    for attempt in range(3):
        try:
            r = await http.post(WEBHOOK, json=payload,
                                headers={"Authorization": f"Bearer {TOKEN}"})
            r.raise_for_status()
            log.info("Pushed msg %s (%s)", payload["message_id"], payload["event"])
            return True
        except Exception as e:
            log.warning("Push attempt %d failed: %s", attempt + 1, e)
            await asyncio.sleep(2 ** attempt)
    with open("/data/failed_pushes.jsonl", "a") as f:
        f.write(json.dumps(payload) + "\n")
    log.error("DEAD-LETTERED message %s — check failed_pushes.jsonl", payload["message_id"])
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
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())