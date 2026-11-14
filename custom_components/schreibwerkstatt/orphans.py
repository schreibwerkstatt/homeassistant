"""Entities the server no longer reports: a repair issue offers to delete them.

An entity whose sample is missing turns unavailable and keeps its history. Only
once it has been missing for ORPHAN_AFTER (a sample can be gone for a while, e.g.
an average without books) does an issue list it; confirming the fix deletes those
entities, and devices left without any.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import SchreibwerkstattCoordinator
from .entity import goal_unique_id, is_goal, sensor_unique_id

ORPHAN_AFTER = timedelta(days=7)
TRANSLATION_KEY = "orphaned_entities"


def issue_id(entry_id: str) -> str:
    return f"{TRANSLATION_KEY}_{entry_id}"


def _key(entity: er.RegistryEntry) -> str:
    return f"{entity.domain}|{entity.unique_id}"


def _expected(coordinator: SchreibwerkstattCoordinator) -> set[str]:
    """Registry keys of every entity the current document feeds."""
    out = set()
    for key, sample in coordinator.data.samples.items():
        if sample.metric.entity:
            out.add(f"sensor|{sensor_unique_id(coordinator, key)}")
        if is_goal(sample):
            out.add(f"binary_sensor|{goal_unique_id(coordinator, sample.user)}")
    return out


@callback
def async_check_orphans(hass: HomeAssistant, coordinator: SchreibwerkstattCoordinator) -> None:
    """After a successful poll: note what went missing, raise or clear the issue."""
    if not coordinator.last_update_success or coordinator.data is None:
        return
    entry = coordinator.config_entry
    store = coordinator.store
    now = dt_util.utcnow()
    expected = _expected(coordinator)
    missing = {
        _key(e)
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if _key(e) not in expected
    }
    changed = False
    for key in missing - store.missing.keys():
        store.missing[key] = now
        changed = True
    for key in store.missing.keys() - missing:
        del store.missing[key]
        changed = True
    if changed:
        store.async_delay_save()
    _async_update_issue(hass, coordinator, now)


def orphaned(hass: HomeAssistant, coordinator: SchreibwerkstattCoordinator, now: datetime) -> list[str]:
    """Entity IDs missing for ORPHAN_AFTER or longer."""
    registry = er.async_get(hass)
    out = []
    for e in er.async_entries_for_config_entry(registry, coordinator.config_entry.entry_id):
        since = coordinator.store.missing.get(_key(e))
        if since is not None and now - since >= ORPHAN_AFTER:
            out.append(e.entity_id)
    return sorted(out)


@callback
def _async_update_issue(hass: HomeAssistant, coordinator: SchreibwerkstattCoordinator, now: datetime) -> None:
    entry = coordinator.config_entry
    entities = orphaned(hass, coordinator, now)
    if not entities:
        ir.async_delete_issue(hass, DOMAIN, issue_id(entry.entry_id))
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(entry.entry_id),
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=TRANSLATION_KEY,
        translation_placeholders=placeholders(entry.title, entities),
        data={"entry_id": entry.entry_id},
    )


def placeholders(title: str, entities: list[str]) -> dict[str, str]:
    """Placeholders of the issue and of its confirm form."""
    return {
        "title": title,
        "count": str(len(entities)),
        "days": str(ORPHAN_AFTER.days),
        "entities": "\n".join(f"- {e}" for e in entities),
    }


@callback
def async_remove_orphans(
    hass: HomeAssistant, coordinator: SchreibwerkstattCoordinator, shown: Collection[str]
) -> list[str]:
    """Delete the entities of `shown` that are still orphaned, and devices left without entities.

    One that came back since the list was shown stays, and so does one that went missing
    meanwhile: nothing is deleted that the user did not see in the list.
    """
    entry_id = coordinator.config_entry.entry_id
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    removed = [e for e in orphaned(hass, coordinator, dt_util.utcnow()) if e in shown]
    for entity_id in removed:
        entry = entities.async_get(entity_id)
        if entry is not None:
            coordinator.store.missing.pop(_key(entry), None)
            entities.async_remove(entity_id)
    in_use = {e.device_id for e in er.async_entries_for_config_entry(entities, entry_id)}
    for device in dr.async_entries_for_config_entry(devices, entry_id):
        if device.id not in in_use:
            devices.async_update_device(device.id, remove_config_entry_id=entry_id)
    coordinator.store.async_delay_save()
    # The next poll raises it again if more are orphaned than were shown.
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry_id))
    return removed
