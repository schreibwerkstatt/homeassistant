"""Binary sensors: daily writing goal reached, per user."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import SchreibwerkstattConfigEntry, SchreibwerkstattCoordinator
from .entity import SchreibwerkstattEntity, async_add_for_samples, goal_unique_id, is_goal
from .models import Sample


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SchreibwerkstattConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """One goal sensor per user with a daily goal; more appear when goals are set."""
    async_add_for_samples(entry, async_add_entities, is_goal, DailyGoalReachedSensor)


class DailyGoalReachedSensor(SchreibwerkstattEntity, BinarySensorEntity):
    """On once today's writing time reaches the user's daily goal."""

    _attr_translation_key = "daily_goal_reached"

    def __init__(self, coordinator: SchreibwerkstattCoordinator, sample: Sample) -> None:
        super().__init__(coordinator, sample)
        assert sample.user is not None
        self._attr_unique_id = goal_unique_id(coordinator, sample.user)

    @property
    def is_on(self) -> bool | None:
        sample = self.sample
        return None if sample is None else sample.value >= 100
