"""Entities the server no longer reports: repair issue and its fix."""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

from freezegun.api import FrozenDateTimeFactory

from custom_components.schreibwerkstatt.const import DOMAIN
from custom_components.schreibwerkstatt.orphans import issue_id
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component

from .conftest import INSTANCE

BEN = "ben@example.com"
ANNA = "anna@example.com"


def _without(doc: dict[str, Any], *users: str) -> dict[str, Any]:
    doc = copy.deepcopy(doc)
    for metric in doc["metrics"]:
        metric["samples"] = [s for s in metric["samples"] if s.get("labels", {}).get("user") not in users]
    return doc


async def _poll(hass: HomeAssistant, entry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_gone_user_raises_issue_after_a_week_and_fix_deletes(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_metrics: AsyncMock,
    metrics_doc: dict[str, Any],
    config_entry,
) -> None:
    assert await async_setup_component(hass, "repairs", {})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    entities = er.async_get(hass)
    ben = entities.async_get_entity_id("sensor", DOMAIN, f"{INSTANCE}_sw_user_books|user={BEN}")
    assert ben
    issues = ir.async_get(hass)
    key = issue_id(config_entry.entry_id)

    mock_metrics.return_value = _without(metrics_doc, BEN)
    await _poll(hass, config_entry)
    assert hass.states.get(ben).state == "unavailable"
    assert issues.async_get_issue(DOMAIN, key) is None  # could be back tomorrow

    freezer.tick(timedelta(days=8))
    await _poll(hass, config_entry)
    issue = issues.async_get_issue(DOMAIN, key)
    assert issue is not None
    assert ben in issue.translation_placeholders["entities"]
    assert "sw_chars" not in issue.translation_placeholders["entities"]

    manager = repairs_flow_manager(hass)
    result = await manager.async_init(DOMAIN, data={"issue_id": key})
    assert result["step_id"] == "confirm"
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY

    assert entities.async_get(ben) is None
    assert issues.async_get_issue(DOMAIN, key) is None
    # Ben's device had nothing left; Anna's keeps hers.
    devices = dr.async_get(hass)
    assert devices.async_get_device(identifiers={(DOMAIN, f"{INSTANCE}_user_{BEN}")}) is None
    assert devices.async_get_device(identifiers={(DOMAIN, f"{INSTANCE}_user_anna@example.com")})
    assert entities.async_get_entity_id("sensor", DOMAIN, f"{INSTANCE}_sw_chars")


async def test_back_before_a_week_clears(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_metrics: AsyncMock,
    metrics_doc: dict[str, Any],
    config_entry,
) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    store = config_entry.runtime_data.store

    mock_metrics.return_value = _without(metrics_doc, BEN)
    await _poll(hass, config_entry)
    assert store.missing

    freezer.tick(timedelta(days=3))
    mock_metrics.return_value = metrics_doc
    await _poll(hass, config_entry)
    assert not store.missing

    freezer.tick(timedelta(days=5))
    await _poll(hass, config_entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id(config_entry.entry_id)) is None


async def test_fix_deletes_only_what_was_shown_and_is_still_gone(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_metrics: AsyncMock,
    metrics_doc: dict[str, Any],
    config_entry,
) -> None:
    assert await async_setup_component(hass, "repairs", {})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    entities = er.async_get(hass)
    ben = entities.async_get_entity_id("sensor", DOMAIN, f"{INSTANCE}_sw_user_books|user={BEN}")
    anna = entities.async_get_entity_id("sensor", DOMAIN, f"{INSTANCE}_sw_user_books|user={ANNA}")
    key = issue_id(config_entry.entry_id)

    mock_metrics.return_value = _without(metrics_doc, BEN)
    await _poll(hass, config_entry)
    freezer.tick(timedelta(days=2))
    mock_metrics.return_value = _without(metrics_doc, BEN, ANNA)
    await _poll(hass, config_entry)
    freezer.tick(timedelta(days=6))
    await _poll(hass, config_entry)

    manager = repairs_flow_manager(hass)
    result = await manager.async_init(DOMAIN, data={"issue_id": key})
    assert ben in result["description_placeholders"]["entities"]
    assert anna not in result["description_placeholders"]["entities"]  # gone for 6 days only

    # While the dialog is open Anna's week is over: she was not in the list, so she stays.
    freezer.tick(timedelta(days=2))
    await _poll(hass, config_entry)
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entities.async_get(ben) is None
    assert entities.async_get(anna) is not None
