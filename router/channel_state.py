"""Per-channel state machine — tracks latest valid signal per channel.
Persistence: atomic JSON write (write to tmp → rename). Thread-safe via Lock.
"""
import json
import os
import copy
import logging
import threading

log = logging.getLogger("channel_state")

STATE_FILE = os.environ.get("CHANNEL_STATE_FILE", "/data/channel_state.json")


class ChannelState:
    def __init__(self):
        self._data: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._load()

    @staticmethod
    def _default() -> dict:
        return {"current": None, "previous": None, "channel_title": ""}

    def get(self, channel_id: str) -> dict:
        """Return a COPY of the channel's full state."""
        with self._lock:
            entry = self._data.get(str(channel_id))
            return copy.deepcopy(entry) if entry else self._default()

    def current_signal(self, channel_id: str) -> dict | None:
        """Return the current valid signal for a channel, or None."""
        with self._lock:
            entry = self._data.get(str(channel_id))
            if entry and entry["current"]:
                return copy.deepcopy(entry["current"])
            return None

    def get_channel_title(self, channel_id: str) -> str:
        """Return the last known channel title, even after signal revert."""
        with self._lock:
            entry = self._data.get(str(channel_id))
            return entry.get("channel_title", "") if entry else ""

    def update(self, channel_id: str, signal: dict):
        """Register a new valid signal. Pushes current -> previous."""
        cid = str(channel_id)
        with self._lock:
            entry = self._data.get(cid)
            if entry is None:
                entry = self._default()
            entry["previous"] = entry["current"]
            entry["current"] = {
                "msg_id": signal.get("msg_id"),
                "channel_title": signal.get("channel_title", ""),
                "action": signal.get("action"),
                "entry": signal.get("entry"),
                "sl": signal.get("sl"),
                "tp": signal.get("tp", []),
                "order_type": signal.get("order_type", "market"),
                "from_image": signal.get("from_image", False),
                "timestamp": signal.get("timestamp", ""),
            }
            entry["channel_title"] = signal.get("channel_title", "")
            self._data[cid] = entry
            self._save()
        log.info("Updated state for channel %s: %s %s",
                 cid, signal.get("action"), signal.get("entry"))

    def remove_signal_id(self, channel_id: str, msg_id: int) -> bool:
        """Remove a specific signal by message_id. Returns True if removed."""
        cid = str(channel_id)
        with self._lock:
            entry = self._data.get(cid)
            if not entry or not entry["current"]:
                return False
            if entry["current"].get("msg_id") == msg_id:
                if entry["previous"] is not None:
                    entry["current"] = entry["previous"]
                    entry["previous"] = None
                else:
                    entry["current"] = None
                self._save()
                return True
            return False

    def _save(self):
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp, STATE_FILE)

    def _load(self):
        try:
            with open(STATE_FILE) as f:
                self._data = json.load(f)
            log.info("Loaded channel state from %s (%d channels)", STATE_FILE, len(self._data))
        except (FileNotFoundError, json.JSONDecodeError):
            self._data = {}
            log.info("No existing channel state — starting fresh")


channel_state = ChannelState()