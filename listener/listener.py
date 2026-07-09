"""
Telegram listener.
- Logs in as YOUR account (userbot) via Telethon.
- Watches the channels in TG_CHANNELS.
- Pushes every new/edited message to Hermes' webhook immediately.
"""
import asyncio, os, json, logging
import httpx
from telethon import TelegramClient, events

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("listener")

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
CHANNELS = [int(c) for c in os.environ.get("TG_CHANNELS", "").split(",") if c.strip()]
WEBHOOK = os.environ["HERMES_WEBHOOK_URL"]
TOKEN = os.environ["HERMES_WEBHOOK_TOKEN"]

client = TelegramClient("/data/tg_session", API_ID, API_HASH)
http = httpx.AsyncClient(timeout=30)

async def push(payload: dict):
    for attempt in range(3):
        try:
            r = await http.post(WEBHOOK, json=payload,
                                headers={"Authorization": f"Bearer {TOKEN}"})
            r.raise_for_status()
            log.info("Pushed msg %s (%s)", payload["message_id"], payload["event"])
            return
        except Exception as e:
            log.warning("Push attempt %d failed: %s", attempt + 1, e)
            await asyncio.sleep(2 ** attempt)
    with open("/data/failed_pushes.jsonl", "a") as f:
        f.write(json.dumps(payload) + "\n")
    log.error("DEAD-LETTERED message %s — check failed_pushes.jsonl", payload["message_id"])

def make_payload(event, kind: str) -> dict:
    m = event.message
    return {
        "event": kind,
        "channel_id": event.chat_id,
        "message_id": m.id,
        "reply_to_id": m.reply_to_msg_id,
        "text": m.message or "",
        "has_media": m.media is not None,
        "date": m.date.isoformat(),
    }

if CHANNELS:
    @client.on(events.NewMessage(chats=CHANNELS))
    async def on_new(event):
        await push(make_payload(event, "new"))

    @client.on(events.MessageEdited(chats=CHANNELS))
    async def on_edit(event):
        await push(make_payload(event, "edit"))
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
