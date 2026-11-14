"""Base entity and device mapping."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, GOAL_METRIC, GROUP_NAMES, USER_MODEL
from .coordinator import SchreibwerkstattConfigEntry, SchreibwerkstattCoordinator
from .models import Sample


def ui_lang(coordinator: SchreibwerkstattCoordinator) -> str:
    """Language for server-provided names: de for German setups, else en."""
    return "de" if (coordinator.hass.config.language or "").startswith("de") else "en"


def instance_key(coordinator: SchreibwerkstattCoordinator) -> str:
    """Stable prefix for identifiers (server instance_id, else the entry id)."""
    return coordinator.config_entry.unique_id or coordinator.config_entry.entry_id


def sensor_unique_id(coordinator: SchreibwerkstattCoordinator, key: str) -> str:
    """Unique ID of the sensor showing the sample `key`."""
    return f"{instance_key(coordinator)}_{key}"


def goal_unique_id(coordinator: SchreibwerkstattCoordinator, user: str) -> str:
    """Unique ID of a user's "daily goal reached" binary sensor."""
    return f"{instance_key(coordinator)}_daily_goal_reached_{user}"


def is_goal(sample: Sample) -> bool:
    """A user's daily goal sample, which also feeds a "goal reached" binary sensor."""
    return sample.metric.name == GOAL_METRIC and sample.user is not None


def main_device_info(coordinator: SchreibwerkstattCoordinator) -> DeviceInfo:
    """The Schreibwerkstatt instance itself."""
    data = coordinator.data
    return DeviceInfo(
        identifiers={(DOMAIN, instance_key(coordinator))},
        name="Schreibwerkstatt",
        manufacturer="Schreibwerkstatt",
        model="Server",
        sw_version=data.label_value("sw_build_info", "version") or data.version,
        configuration_url=coordinator.client.url,
        entry_type=DeviceEntryType.SERVICE,
    )


def user_identifier(coordinator: SchreibwerkstattCoordinator, email: str) -> tuple[str, str]:
    """Device identifier of one user."""
    return (DOMAIN, f"{instance_key(coordinator)}_user_{email}")


def device_info_for(coordinator: SchreibwerkstattCoordinator, sample: Sample) -> DeviceInfo:
    """Device a sample belongs to: main, group or user device."""
    group = sample.metric.group
    if group == "server":
        return main_device_info(coordinator)
    lang = ui_lang(coordinator)
    root = instance_key(coordinator)
    sub = DeviceInfo(
        manufacturer="Schreibwerkstatt",
        via_device=(DOMAIN, root),
        entry_type=DeviceEntryType.SERVICE,
    )
    if group == "user" and sample.user:
        return sub | DeviceInfo(
            identifiers={user_identifier(coordinator, sample.user)},
            name=sample.labels.get("user_name") or sample.user,
            model=USER_MODEL[lang],
        )
    title = GROUP_NAMES[lang].get(group, group)
    return sub | DeviceInfo(
        identifiers={(DOMAIN, f"{root}_{group}")}, name=f"Schreibwerkstatt {title}", model=title
    )


@callback
def async_add_for_samples(
    entry: SchreibwerkstattConfigEntry,
    async_add_entities: AddEntitiesCallback,
    wanted: Callable[[Sample], bool],
    factory: Callable[[SchreibwerkstattCoordinator, Sample], SchreibwerkstattEntity],
) -> None:
    """Create an entity for every wanted sample now and whenever one shows up later.

    A sample counts as handled while its entity exists: one deleted from the registry
    (repair, device removal) is created again once the server reports it again.
    """
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new = []
        for key, sample in coordinator.data.samples.items():
            if key in known or not wanted(sample):
                continue
            known.add(key)
            entity = factory(coordinator, sample)
            entity.async_on_remove(lambda key=key: known.discard(key))
            new.append(entity)
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class SchreibwerkstattEntity(CoordinatorEntity[SchreibwerkstattCoordinator]):
    """Entity bound to one sample key of the metrics document."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SchreibwerkstattCoordinator, sample: Sample) -> None:
        super().__init__(coordinator)
        self._key = sample.key
        self._attr_device_info = device_info_for(coordinator, sample)

    @property
    def sample(self) -> Sample | None:
        """Current sample, None if the server no longer reports it."""
        return self.coordinator.data.samples.get(self._key)

    @property
    def available(self) -> bool:
        return super().available and self.sample is not None
