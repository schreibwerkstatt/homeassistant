"""Fix flow for the "entities no longer reported" issue."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.util import dt as dt_util

from .orphans import async_remove_orphans, orphaned, placeholders


class OrphanedEntitiesFlow(RepairsFlow):
    """Confirm, then delete the listed entities."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id
        # Entity IDs the confirm form listed: only these may be deleted.
        self._shown: list[str] = []

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None or entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_loaded")
        if user_input is not None:
            async_remove_orphans(self.hass, entry.runtime_data, self._shown)
            return self.async_create_entry(data={})
        # The list as it is now, which is at most what confirming deletes.
        self._shown = orphaned(self.hass, entry.runtime_data, dt_util.utcnow())
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders=placeholders(entry.title, self._shown),
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    return OrphanedEntitiesFlow((data or {})["entry_id"])
