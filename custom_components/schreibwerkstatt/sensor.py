"""Sensors: one generic entity per metric sample of /metrics.json."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfInformation, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import SchreibwerkstattConfigEntry, SchreibwerkstattCoordinator
from .entity import SchreibwerkstattEntity, sensor_unique_id, ui_lang
from .models import Sample, zone

# Calendar month on the billing side is cut in UTC (Anthropic cost report).
UTC_RESET_METRICS = {"sw_billed_usd_month"}
# How far HA's clock and the server's reset may lie apart at a period change.
MIN_RESET_WINDOW = timedelta(minutes=10)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SchreibwerkstattConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create sensors now and for every sample that shows up later."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new = []
        for key, sample in coordinator.data.samples.items():
            if key in known or not sample.metric.entity:
                continue
            known.add(key)
            new.append(SchreibwerkstattSensor(coordinator, sample))
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


def _enum[T](cls: type[T], value: str | None) -> T | None:
    if not value:
        return None
    try:
        return cls(value)  # type: ignore[call-arg]
    except ValueError:
        return None


class SchreibwerkstattSensor(SchreibwerkstattEntity, SensorEntity):
    """A metric sample; all presentation comes from the server description."""

    def __init__(self, coordinator: SchreibwerkstattCoordinator, sample: Sample) -> None:
        super().__init__(coordinator, sample)
        metric = sample.metric
        self._attr_unique_id = sensor_unique_id(coordinator, sample.key)
        self._period: Period | None = None
        name = metric.title_for(ui_lang(coordinator))
        if sample.name_labels:
            name = f"{name} ({' · '.join(sample.name_labels)})"
        self._attr_name = name
        self._attr_icon = metric.icon
        self._attr_entity_registry_enabled_default = metric.enabled_default
        if metric.diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

        device_class = _enum(SensorDeviceClass, metric.device_class)
        self._attr_device_class = device_class
        if device_class == SensorDeviceClass.TIMESTAMP:
            return
        self._attr_native_unit_of_measurement = metric.unit
        self._attr_state_class = _enum(SensorStateClass, metric.state_class)
        if device_class == SensorDeviceClass.DURATION:
            # Seconds on the wire; minutes read better for daily times, hours for uptime.
            self._attr_suggested_unit_of_measurement = (
                UnitOfTime.MINUTES if metric.reset else UnitOfTime.HOURS
            )
            self._attr_suggested_display_precision = 0 if metric.reset else 1
        elif device_class == SensorDeviceClass.DATA_SIZE:
            self._attr_suggested_unit_of_measurement = UnitOfInformation.MEBIBYTES
            self._attr_suggested_display_precision = 1
        elif device_class == SensorDeviceClass.MONETARY:
            self._attr_suggested_display_precision = 2

    @property
    def native_value(self) -> float | int | datetime | None:
        sample = self.sample
        if sample is None:
            return None
        if self._attr_device_class == SensorDeviceClass.TIMESTAMP:
            return datetime.fromtimestamp(sample.value, UTC)
        value = sample.value
        return int(value) if value.is_integer() else value

    @property
    def last_reset(self) -> datetime | None:
        """Start of the current day/month for periodically restarting totals."""
        sample = self.sample
        if sample is None or sample.metric.reset not in ("day", "month"):
            return None
        if self._attr_state_class != SensorStateClass.TOTAL:
            return None
        if self._period is None:
            self._period = Period(sample.metric.reset)
        tz = UTC if sample.metric.name in UTC_RESET_METRICS else zone(self.coordinator.data.timezone)
        interval = self.coordinator.update_interval or timedelta()
        return self._period.update(sample.value, datetime.now(tz), max(MIN_RESET_WINDOW, 2 * interval))


def period_start(now: datetime, reset: str) -> datetime:
    """Start of the day or month `now` falls in, in now's time zone."""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.replace(day=1) if reset == "month" else start


def next_period(start: datetime, reset: str) -> datetime:
    """Start of the period after the one beginning at `start` (DST-safe)."""
    tz: tzinfo | None = start.tzinfo
    day = start.date() + timedelta(days=32 if reset == "month" else 1)
    if reset == "month":
        day = day.replace(day=1)
    return datetime(day.year, day.month, day.day, tzinfo=tz)


@dataclass(slots=True)
class Period:
    """The period the server's value counts in, which need not be the one on HA's clock.

    The server restarts its daily and monthly totals by its own clock. A new
    last_reset makes the recorder start a new cycle at the value it sees then, so
    when the two clocks disagree around midnight, one day takes what belongs to the
    other. Within `window` of a period change the value decides:
    - it falls to less than half while HA's clock is still in the old period: the
      server has started the new one;
    - HA's clock is in the new period, but the value has not fallen yet: the server
      still reports the old one.
    """

    reset: str
    start: datetime | None = None
    last: float | None = None

    def update(self, value: float, now: datetime, window: timedelta) -> datetime:
        clock = period_start(now, self.reset)
        if self.start is None:
            self.start = clock
        elif clock > self.start:
            late = self.last is not None and self.last > 0 and value >= self.last and now - clock < window
            if not late:
                self.start = clock
        elif (
            clock == self.start
            and self.last is not None
            and self.last > 0
            and value < self.last / 2
            and next_period(clock, self.reset) - now < window
        ):
            self.start = next_period(clock, self.reset)
        self.last = value
        return self.start
