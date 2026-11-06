"""The milestone blueprint, run as a real automation."""

from __future__ import annotations

from pathlib import Path
import shutil

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.setup import async_setup_component

BLUEPRINT = Path(__file__).parent.parent / "blueprints/automation/schreibwerkstatt/milestone.yaml"


async def test_milestone(hass: HomeAssistant) -> None:
    target = Path(hass.config.path("blueprints/automation/schreibwerkstatt"))
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy(BLUEPRINT, target / "milestone.yaml")
    hass.states.async_set("sensor.chars", "95000", {"friendly_name": "Zeichen"})

    fired: list[dict] = []

    @callback
    def _record(event: Event) -> None:
        fired.append(event.data)

    hass.bus.async_listen("milestone", _record)
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "use_blueprint": {
                    "path": "schreibwerkstatt/milestone.yaml",
                    "input": {
                        "sensor": "sensor.chars",
                        "step": 100000,
                        "actions": [
                            {
                                "event": "milestone",
                                "event_data": {"mark": "{{ milestone }}", "name": "{{ name }}"},
                            }
                        ],
                    },
                }
            }
        },
    )

    for state in ("99000", "101500", "150000", "unavailable", "120000", "201000.5"):
        hass.states.async_set("sensor.chars", state, {"friendly_name": "Zeichen"})
        await hass.async_block_till_done()

    # 99000 → 101500 and 120000 → 201000.5 pass a mark; back from unavailable does not.
    assert fired == [{"mark": 100000, "name": "Zeichen"}, {"mark": 200000, "name": "Zeichen"}]


async def test_milestone_once_with_helper(hass: HomeAssistant) -> None:
    target = Path(hass.config.path("blueprints/automation/schreibwerkstatt"))
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy(BLUEPRINT, target / "milestone.yaml")
    assert await async_setup_component(
        hass, "input_number", {"input_number": {"best": {"min": 0, "max": 1000000, "step": 1}}}
    )
    hass.states.async_set("sensor.chars", "95000")

    fired: list[int] = []

    @callback
    def _record(event: Event) -> None:
        fired.append(event.data["mark"])

    hass.bus.async_listen("milestone", _record)
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "use_blueprint": {
                    "path": "schreibwerkstatt/milestone.yaml",
                    "input": {
                        "sensor": "sensor.chars",
                        "step": 100000,
                        "record": "input_number.best",
                        "actions": [{"event": "milestone", "event_data": {"mark": "{{ milestone }}"}}],
                    },
                }
            }
        },
    )

    # Up past 100k, text deleted, up past 100k again, then on to 200k.
    for state in ("101000", "98000", "102000", "205000"):
        hass.states.async_set("sensor.chars", state)
        await hass.async_block_till_done()

    assert fired == [100000, 200000]
    assert float(hass.states.get("input_number.best").state) == 200000
