"""Polling coordinator for Schreibwerkstatt."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SchreibwerkstattAuthError, SchreibwerkstattClient, SchreibwerkstattError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .models import MetricsData
from .store import EntryStore

_LOGGER = logging.getLogger(__name__)

type SchreibwerkstattConfigEntry = ConfigEntry[SchreibwerkstattCoordinator]


class SchreibwerkstattCoordinator(DataUpdateCoordinator[MetricsData]):
    """Fetches /metrics.json once per interval for all entities."""

    config_entry: SchreibwerkstattConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SchreibwerkstattConfigEntry,
        client: SchreibwerkstattClient,
        store: EntryStore,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=scan_interval(entry),
        )
        self.client = client
        self.store = store

    async def _async_update_data(self) -> MetricsData:
        try:
            raw = await self.client.async_get_metrics()
        except SchreibwerkstattAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SchreibwerkstattError as err:
            raise UpdateFailed(str(err)) from err
        try:
            return MetricsData.from_json(raw)
        except (AttributeError, KeyError, TypeError, ValueError) as err:
            raise UpdateFailed(f"unexpected metrics document: {err!r}") from err


def scan_interval(entry: ConfigEntry) -> timedelta:
    """Polling interval from the options."""
    return timedelta(seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
