"""What one config entry remembers across restarts.

- history: which sensors the back-fill has finished, so a start does not fetch the
  whole daily series again when nothing new can come of it;
- missing: since when an entity of this entry no longer has a value on the server.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

STORAGE_VERSION = 1
SAVE_DELAY = 30


class EntryStore:
    """Small JSON store per config entry (.storage/schreibwerkstatt.<entry_id>)."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")
        # Server version of the last finished back-fill; a new one may report more series.
        self.history_version: str | None = None
        self.history_done: set[str] = set()
        # "<platform>|<unique_id>" → first poll without a value.
        self.missing: dict[str, datetime] = {}

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        history = data.get("history") or {}
        self.history_version = history.get("version")
        self.history_done = set(history.get("done") or [])
        self.missing = {}
        for key, since in (data.get("missing") or {}).items():
            if parsed := dt_util.parse_datetime(since):
                self.missing[key] = parsed

    async def async_save(self) -> None:
        await self._store.async_save(self._data())

    @callback
    def async_delay_save(self) -> None:
        self._store.async_delay_save(self._data, SAVE_DELAY)

    async def async_remove(self) -> None:
        await self._store.async_remove()

    def _data(self) -> dict[str, Any]:
        return {
            "history": {"version": self.history_version, "done": sorted(self.history_done)},
            "missing": {key: since.isoformat() for key, since in sorted(self.missing.items())},
        }
