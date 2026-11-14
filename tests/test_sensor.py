"""Entities built from the metrics description."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from custom_components.schreibwerkstatt.api import SchreibwerkstattAuthError
from custom_components.schreibwerkstatt.const import DOMAIN
from custom_components.schreibwerkstatt.diagnostics import async_get_config_entry_diagnostics
from custom_components.schreibwerkstatt.entity import find_device
from custom_components.schreibwerkstatt.sensor import Period
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_API_TOKEN, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed

from .conftest import INSTANCE


async def _setup(hass: HomeAssistant, entry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _entity_id(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{INSTANCE}_{key}")
    assert entity_id, key
    return entity_id


ANNA = "user=anna@example.com"


async def test_sensors_follow_the_description(
    hass: HomeAssistant, mock_metrics: AsyncMock, config_entry
) -> None:
    await _setup(hass, config_entry)

    month = hass.states.get(_entity_id(hass, "sensor", "sw_cost_usd_month"))
    assert float(month.state) == 0.42
    assert month.attributes["unit_of_measurement"] == "USD"
    assert month.attributes["device_class"] == "monetary"
    assert month.attributes["state_class"] == "total"
    assert month.attributes["last_reset"].endswith(("+01:00", "+02:00"))  # Europe/Zurich midnight

    writing = hass.states.get(_entity_id(hass, "sensor", f"sw_user_writing_seconds_today|{ANNA}"))
    assert float(writing.state) == 30  # 1800 s shown in minutes
    assert writing.attributes["unit_of_measurement"] == "min"

    seen = hass.states.get(_entity_id(hass, "sensor", f"sw_user_last_seen_timestamp_seconds|{ANNA}"))
    assert seen.state == "2026-10-04T08:00:00+00:00"

    users = hass.states.get(_entity_id(hass, "sensor", "sw_users|status=active"))
    assert users.state == "2"
    assert users.name.endswith("(active)")


async def test_devices(hass: HomeAssistant, mock_metrics: AsyncMock, config_entry) -> None:
    await _setup(hass, config_entry)
    devices = dr.async_get(hass)
    main = find_device(devices, config_entry.runtime_data, (DOMAIN, INSTANCE))
    assert main.sw_version == "4.16.0"
    assert main.configuration_url == "https://sw.example.com"
    anna = find_device(devices, config_entry.runtime_data, (DOMAIN, f"{INSTANCE}_user_anna@example.com"))
    assert anna.name == "Anna"
    assert anna.via_device_id == main.id
    ai = find_device(devices, config_entry.runtime_data, (DOMAIN, f"{INSTANCE}_ai"))
    assert ai.name == "Schreibwerkstatt AI"
    # sw_build_info is device info, not an entity.
    assert (
        er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{INSTANCE}_sw_build_info|version=4.16.0")
        is None
    )


async def test_disabled_and_diagnostic_defaults(
    hass: HomeAssistant, mock_metrics: AsyncMock, config_entry
) -> None:
    await _setup(hass, config_entry)
    registry = er.async_get(hass)
    # Label-heavy breakdowns are off by default.
    finished = registry.async_get(
        _entity_id(hass, "sensor", "sw_cache_read_tokens_total|model=claude-sonnet-4-6|provider=claude")
    )
    assert finished.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    uptime = registry.async_get(_entity_id(hass, "sensor", "sw_process_uptime_seconds"))
    assert uptime.entity_category == "diagnostic"


async def test_daily_goal_binary_sensor(hass: HomeAssistant, mock_metrics: AsyncMock, config_entry) -> None:
    await _setup(hass, config_entry)
    goal = hass.states.get(_entity_id(hass, "binary_sensor", "daily_goal_reached_anna@example.com"))
    assert goal.state == "on"  # 30 of 30 minutes
    # Ben has no daily goal → no goal sensor.
    assert (
        er.async_get(hass).async_get_entity_id(
            "binary_sensor", DOMAIN, f"{INSTANCE}_daily_goal_reached_ben@example.com"
        )
        is None
    )


async def test_new_samples_appear_and_missing_become_unavailable(
    hass: HomeAssistant, mock_metrics: AsyncMock, metrics_doc: dict[str, Any], config_entry
) -> None:
    await _setup(hass, config_entry)
    doc = copy.deepcopy(metrics_doc)
    for metric in doc["metrics"]:
        if metric["name"] == "sw_user_books":
            metric["samples"].append(
                {"labels": {"user": "cleo@example.com", "user_name": "Cleo"}, "value": 3}
            )
        if metric["name"] == "sw_cost_usd_today":
            metric["samples"] = []
    mock_metrics.return_value = doc
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    cleo = hass.states.get(_entity_id(hass, "sensor", "sw_user_books|user=cleo@example.com"))
    assert cleo.state == "3"
    assert hass.states.get(_entity_id(hass, "sensor", "sw_cost_usd_today")).state == "unavailable"


async def test_character_averages(
    hass: HomeAssistant, mock_metrics: AsyncMock, metrics_doc: dict[str, Any], config_entry
) -> None:
    await _setup(hass, config_entry)
    per_book = _entity_id(hass, "sensor", "sw_chars_per_book")
    per_author = _entity_id(hass, "sensor", "sw_chars_per_author")
    assert hass.states.get(per_book).state == "7200"  # 7200 characters, 1 book
    assert hass.states.get(per_book).attributes["state_class"] == "measurement"
    assert hass.states.get(per_author).state == "7200"  # Ben has none of his own: not counted
    assert hass.states.get(_entity_id(hass, "sensor", "sw_chars_per_hour")).state == "3600"
    assert hass.states.get(_entity_id(hass, "sensor", f"sw_user_chars_per_hour|{ANNA}")).state == "2400"
    # Ben did not write today: no pace, no sensor.
    assert (
        er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, f"{INSTANCE}_sw_user_chars_per_hour|user=ben@example.com"
        )
        is None
    )

    doc = copy.deepcopy(metrics_doc)
    for metric in doc["metrics"]:
        if metric["name"] == "sw_user_chars":
            metric["samples"].append(
                {"labels": {"user": "cleo@example.com", "user_name": "Cleo"}, "value": 3000}
            )
        if metric["name"] == "sw_books_written":
            metric["samples"][0]["value"] = 0
    mock_metrics.return_value = doc
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(per_author).state == "5100"
    assert hass.states.get(per_book).state == "unavailable"  # no book with text, no average


async def test_revoked_token_starts_reauth(
    hass: HomeAssistant, mock_metrics: AsyncMock, config_entry
) -> None:
    await _setup(hass, config_entry)
    mock_metrics.side_effect = SchreibwerkstattAuthError("401")
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == SOURCE_REAUTH for f in flows)


async def test_unload(hass: HomeAssistant, mock_metrics: AsyncMock, config_entry) -> None:
    await _setup(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_renamed_user_keeps_entities(
    hass: HomeAssistant, mock_metrics: AsyncMock, metrics_doc: dict[str, Any], config_entry
) -> None:
    await _setup(hass, config_entry)
    writing = _entity_id(hass, "sensor", f"sw_user_writing_seconds_today|{ANNA}")
    doc = copy.deepcopy(metrics_doc)
    for metric in doc["metrics"]:
        for sample in metric["samples"]:
            if sample["labels"].get("user") == "anna@example.com":
                sample["labels"]["user_name"] = "Anna Muster"
    mock_metrics.return_value = doc
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    # Same entity, still reporting; the device follows the new display name.
    assert _entity_id(hass, "sensor", f"sw_user_writing_seconds_today|{ANNA}") == writing
    assert float(hass.states.get(writing).state) == 30
    anna = (DOMAIN, f"{INSTANCE}_user_anna@example.com")
    device = find_device(dr.async_get(hass), config_entry.runtime_data, anna)
    assert device.name == "Anna Muster"


async def test_migrates_unique_ids_without_user_name(
    hass: HomeAssistant, mock_metrics: AsyncMock, config_entry
) -> None:
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, minor_version=1)
    registry = er.async_get(hass)
    old = f"{INSTANCE}_sw_user_writing_seconds_today|user=anna@example.com|user_name=Anna"
    kept = registry.async_get_or_create(
        "sensor", DOMAIN, old, config_entry=config_entry, suggested_object_id="anna_old"
    )
    # Renamed once already under 1.1: a second entity for the same user and metric.
    dup = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{INSTANCE}_sw_user_writing_seconds_today|user=anna@example.com|user_name=Ann",
        config_entry=config_entry,
        suggested_object_id="ann_old",
    )
    untouched = registry.async_get_or_create(
        "sensor", DOMAIN, f"{INSTANCE}_sw_users|status=active", config_entry=config_entry
    )

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.minor_version == 2
    assert registry.async_get(kept.entity_id).unique_id == f"{INSTANCE}_sw_user_writing_seconds_today|{ANNA}"
    assert hass.states.get(kept.entity_id).state not in ("unavailable", "unknown")  # fed by the new key
    assert registry.async_get(dup.entity_id).unique_id.endswith("user_name=Ann")
    assert registry.async_get(untouched.entity_id).unique_id == f"{INSTANCE}_sw_users|status=active"


async def test_missing_value_is_unavailable_not_zero(
    hass: HomeAssistant, mock_metrics: AsyncMock, metrics_doc: dict[str, Any], config_entry
) -> None:
    """A counter at 0 for one poll would count as a reset and its value twice after."""
    await _setup(hass, config_entry)
    tokens = _entity_id(hass, "sensor", "sw_tokens_in_total|model=claude-sonnet-4-6|provider=claude")
    doc = copy.deepcopy(metrics_doc)
    for metric in doc["metrics"]:
        if metric["name"] == "sw_tokens_in_total":
            for sample in metric["samples"]:
                sample["value"] = None
    # Broken entries are left out, the rest still arrives.
    doc["metrics"].append({"title": {"en": "no name"}, "samples": [{"value": 1}]})
    doc["metrics"].append({"name": "sw_garbage", "samples": [{"value": "abc"}, "nonsense"]})
    mock_metrics.return_value = doc
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert config_entry.runtime_data.last_update_success
    assert hass.states.get(tokens).state == "unavailable"
    assert hass.states.get(_entity_id(hass, "sensor", "sw_chars")).state == "7200"


async def test_broken_document_fails_the_update_cleanly(
    hass: HomeAssistant, mock_metrics: AsyncMock, metrics_doc: dict[str, Any], config_entry
) -> None:
    await _setup(hass, config_entry)
    doc = copy.deepcopy(metrics_doc)
    doc["metrics"][0]["samples"][0]["labels"] = ["not", "a", "mapping"]
    mock_metrics.return_value = doc
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert not config_entry.runtime_data.last_update_success
    assert isinstance(config_entry.runtime_data.last_exception, UpdateFailed)


async def test_options_change_does_not_reload(
    hass: HomeAssistant, mock_metrics: AsyncMock, config_entry
) -> None:
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    hass.config_entries.async_update_entry(config_entry, options={CONF_SCAN_INTERVAL: 300})
    await hass.async_block_till_done()
    assert config_entry.runtime_data is coordinator
    assert coordinator.update_interval == timedelta(seconds=300)


async def test_diagnostics_redact_token_and_people(
    hass: HomeAssistant, mock_metrics: AsyncMock, config_entry
) -> None:
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={"dashboard_users": ["anna@example.com"]})
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["entry"]["data"][CONF_API_TOKEN] == "**REDACTED**"
    assert diag["entry"]["options"]["dashboard_users"] == "**REDACTED**"
    assert "anna@example.com" not in repr(diag)


ZURICH = ZoneInfo("Europe/Zurich")
WINDOW = timedelta(minutes=10)


def _now(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=ZURICH)


def test_period_follows_the_clock() -> None:
    period = Period("day")
    assert period.update(100, _now(4, 12), WINDOW) == _now(4, 0)
    assert period.update(150, _now(4, 23, 50), WINDOW) == _now(4, 0)
    # Reset on time: new day on both clocks.
    assert period.update(0, _now(5, 0, 1), WINDOW) == _now(5, 0)


def test_period_server_resets_before_ha_midnight() -> None:
    period = Period("day")
    period.update(150, _now(4, 23, 55), WINDOW)
    # The server is ahead: its day is over, HA's clock is not there yet.
    assert period.update(0, _now(4, 23, 58), WINDOW) == _now(5, 0)
    # Midnight on HA's clock is no second reset.
    assert period.update(40, _now(5, 0, 1), WINDOW) == _now(5, 0)


def test_period_server_resets_after_ha_midnight() -> None:
    period = Period("day")
    period.update(150, _now(4, 23, 59), WINDOW)
    # HA's clock has passed midnight, the server still reports yesterday.
    assert period.update(150, _now(5, 0, 1), WINDOW) == _now(4, 0)
    assert period.update(0, _now(5, 0, 2), WINDOW) == _now(5, 0)


def test_period_deleting_text_is_no_reset() -> None:
    period = Period("day")
    period.update(1000, _now(4, 12), WINDOW)
    # Net characters fall during the day: outside the window, nothing happens.
    assert period.update(100, _now(4, 13), WINDOW) == _now(4, 0)
    # Close to midnight a small drop is still no reset.
    period.update(1000, _now(4, 23, 50), WINDOW)
    assert period.update(800, _now(4, 23, 55), WINDOW) == _now(4, 0)


def test_period_month() -> None:
    period = Period("month")
    assert period.update(5, _now(4, 12), WINDOW) == datetime(2026, 10, 1, tzinfo=ZURICH)
    assert period.update(0, datetime(2026, 10, 31, 23, 57, tzinfo=ZURICH), WINDOW) == datetime(
        2026, 11, 1, tzinfo=ZURICH
    )


async def test_deleted_entity_comes_back_with_its_value(
    hass: HomeAssistant, mock_metrics: AsyncMock, config_entry
) -> None:
    await _setup(hass, config_entry)
    registry = er.async_get(hass)
    books = _entity_id(hass, "sensor", f"sw_user_books|{ANNA}")
    goal = _entity_id(hass, "binary_sensor", "daily_goal_reached_anna@example.com")
    registry.async_remove(books)
    registry.async_remove(goal)
    await hass.async_block_till_done()
    assert hass.states.get(books) is None

    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(_entity_id(hass, "sensor", f"sw_user_books|{ANNA}")).state == "1"
    assert hass.states.get(_entity_id(hass, "binary_sensor", "daily_goal_reached_anna@example.com"))


async def test_last_reset_of_totals(hass: HomeAssistant, mock_metrics: AsyncMock, config_entry) -> None:
    await _setup(hass, config_entry)
    state = hass.states.get(_entity_id(hass, "sensor", "sw_cost_usd_month"))
    assert datetime.fromisoformat(state.attributes["last_reset"]).day == 1
    # total_increasing restarts on its own: no last_reset.
    state = hass.states.get(_entity_id(hass, "sensor", "sw_writing_seconds_today"))
    assert "last_reset" not in state.attributes


async def test_server_update_reaches_the_device(
    hass: HomeAssistant, mock_metrics: AsyncMock, metrics_doc: dict[str, Any], config_entry
) -> None:
    await _setup(hass, config_entry)
    doc = copy.deepcopy(metrics_doc)
    doc["version"] = "4.30.0"
    for metric in doc["metrics"]:
        if metric["name"] == "sw_build_info":
            metric["samples"][0]["labels"]["version"] = "4.30.0"
    mock_metrics.return_value = doc
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    device = find_device(dr.async_get(hass), config_entry.runtime_data, (DOMAIN, INSTANCE))
    assert device.sw_version == "4.30.0"


async def test_goal_without_user_has_no_binary_sensor(
    hass: HomeAssistant, mock_metrics: AsyncMock, metrics_doc: dict[str, Any], config_entry
) -> None:
    doc = copy.deepcopy(metrics_doc)
    for metric in doc["metrics"]:
        if metric["name"] == "sw_user_daily_goal_percent":
            metric["samples"] = [{"labels": {}, "value": 50}]
    mock_metrics.return_value = doc
    await _setup(hass, config_entry)
    assert not hass.states.async_entity_ids("binary_sensor")


async def test_duration_without_unit_is_seconds(
    hass: HomeAssistant, mock_metrics: AsyncMock, metrics_doc: dict[str, Any], config_entry
) -> None:
    doc = copy.deepcopy(metrics_doc)
    for metric in doc["metrics"]:
        if metric["name"] == "sw_writing_seconds_today":
            metric["unit"] = None
    mock_metrics.return_value = doc
    await _setup(hass, config_entry)
    state = hass.states.get(_entity_id(hass, "sensor", "sw_writing_seconds_today"))
    assert state.attributes["unit_of_measurement"] == "min"
