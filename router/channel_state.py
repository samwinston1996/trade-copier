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
            if entry and entry.get("current"):
                return copy.deepcopy(entry["current"])
            return None

    def update(self, channel_id: str, signal: dict):
        """Push a new signal: current → previous, then set new current."""
        with self._lock:
            cid = str(channel_id)
            entry = self._data.setdefault(cid, self._default())
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
            if signal.get("channel_title"):
                entry["channel_title"] = signal["channel_title"]
            self._save()

    def get_channel_title(self, channel_id: str) -> str:
        with self._lock:
            return self._data.get(str(channel_id), {}).get("channel_title", "")

    def remove_signal_id(self, channel_id: str, msg_id: int) -> bool:
        """Revert to previous signal when a message is deleted. Returns True if reverted."""
        with self._lock:
            cid = str(channel_id)
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
