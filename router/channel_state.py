"""Per-channel state machine — tracks latest valid signal per channel.
Persistence: atomic JSON write (write to tmp → rename). Thread-safe via Lock.

V2.0 fix: Every method reads from FILE first, not from stale in-memory cache.
This prevents race conditions between uvicorn workers where one worker's
in-memory state diverges from another's.
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

    # ── Readers (read from file every call for cross-worker sync) ──

    def get(self, channel_id: str) -> dict:
        """Return a COPY of the channel's full state. Reads from file for cross-worker sync."""
        with self._lock:
            try:
                with open(STATE_FILE, 'r') as f:
                    data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                return self._default()
            entry = data.get(str(channel_id))
            return copy.deepcopy(entry) if entry else self._default()

    def current_signal(self, channel_id: str) -> dict | None:
        """Return the current valid signal for a channel, or None.
        Reads from file each time to stay in sync across multiple workers.
        """
        with self._lock:
            try:
                with open(STATE_FILE, 'r') as f:
                    data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                return None
            entry = data.get(str(channel_id))
            if entry and entry.get("current"):
                return copy.deepcopy(entry["current"])
            return None

    def get_channel_title(self, channel_id: str) -> str:
        with self._lock:
            try:
                with open(STATE_FILE, 'r') as f:
                    data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                return ""
            return data.get(str(channel_id), {}).get("channel_title", "")

    # ── Writers (reload from file before merging, then write) ──

    def update(self, channel_id: str, signal: dict):
        """Push a new signal: current → previous, then set new current.
        Reloads from file first to merge changes from other workers.
        """
        with self._lock:
            cid = str(channel_id)
            # Reload from file to merge with other workers' updates
            try:
                with open(STATE_FILE, 'r') as f:
                    file_data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                file_data = {}
            self._data = file_data  # sync cache from file

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
                "pending_tp": signal.get("pending_tp", False),
                "tickets": signal.get("tickets", []),
                "timestamp": signal.get("timestamp", ""),
            }
            if signal.get("channel_title"):
                entry["channel_title"] = signal["channel_title"]
            self._save()

    def remove_signal_id(self, channel_id: str, msg_id: int) -> bool:
        """Revert to previous signal when a message is deleted. Returns True if reverted.
        Reads from file first to stay in sync across workers.
        """
        with self._lock:
            cid = str(channel_id)
            # Reload from file
            try:
                with open(STATE_FILE, 'r') as f:
                    data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                data = {}
            self._data = data  # sync cache from file

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

    # ── Persistence ──

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