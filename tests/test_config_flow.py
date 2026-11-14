"""Config, reauth and options flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.schreibwerkstatt.api import (
    SchreibwerkstattAuthError,
    SchreibwerkstattConnectionError,
    SchreibwerkstattNotSupportedError,
    SchreibwerkstattRedirectError,
    SchreibwerkstattSchemaError,
)
from custom_components.schreibwerkstatt.const import DOMAIN
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_API_TOKEN, CONF_SCAN_INTERVAL, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import CLIENT, INSTANCE, TOKEN, URL


async def test_user_flow_creates_entry(hass: HomeAssistant, mock_metrics: AsyncMock) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        # A pasted endpoint URL and stray whitespace are normalised away.
        {CONF_URL: f"{URL}/metrics.json/", CONF_API_TOKEN: f" {TOKEN} ", CONF_VERIFY_SSL: False},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "sw.example.com"
    assert result["data"] == {CONF_URL: URL, CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: False}
    assert result["result"].unique_id == INSTANCE


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (SchreibwerkstattAuthError("401"), "invalid_auth"),
        (SchreibwerkstattConnectionError("down"), "cannot_connect"),
        (SchreibwerkstattNotSupportedError("404"), "not_supported"),
        (SchreibwerkstattSchemaError("schema 2"), "unsupported_schema"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_flow_errors(hass: HomeAssistant, error: Exception, key: str) -> None:
    with patch(CLIENT, new_callable=AsyncMock, side_effect=error):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: URL, CONF_API_TOKEN: TOKEN}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}


async def test_same_instance_aborts_and_updates_url(
    hass: HomeAssistant, mock_metrics: AsyncMock, config_entry
) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "https://new-host.example.com", CONF_API_TOKEN: TOKEN}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert config_entry.data[CONF_URL] == "https://new-host.example.com"


async def test_reauth_replaces_token(hass: HomeAssistant, mock_metrics: AsyncMock, config_entry) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "sw_" + "b" * 64}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_API_TOKEN] == "sw_" + "b" * 64


async def test_options_scan_interval(hass: HomeAssistant, mock_metrics: AsyncMock, config_entry) -> None:
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_SCAN_INTERVAL: 120})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {CONF_SCAN_INTERVAL: 120}
    assert config_entry.runtime_data.update_interval.total_seconds() == 120


async def test_options_dashboard_users(hass: HomeAssistant, mock_metrics: AsyncMock, config_entry) -> None:
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    (field,) = (k for k in result["data_schema"].schema if k == "dashboard_users")
    options = result["data_schema"].schema[field].config["options"]
    assert [o["label"] for o in options] == ["Anna", "Ben"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 60, "dashboard_users": ["anna@example.com"]}
    )
    await hass.async_block_till_done()
    assert config_entry.options == {CONF_SCAN_INTERVAL: 60, "dashboard_users": ["anna@example.com"]}


async def test_user_flow_redirect_names_the_target(hass: HomeAssistant) -> None:
    with patch(CLIENT, new_callable=AsyncMock, side_effect=SchreibwerkstattRedirectError(URL)):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: "http://sw.example.com", CONF_API_TOKEN: TOKEN}
        )
    assert result["errors"] == {"base": "redirect"}
    assert result["description_placeholders"]["location"] == URL


async def test_reconfigure_changes_url(hass: HomeAssistant, mock_metrics: AsyncMock, config_entry) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    result = await config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "https://neu.example.com/", CONF_VERIFY_SSL: False}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data == {
        CONF_URL: "https://neu.example.com",
        CONF_API_TOKEN: TOKEN,
        CONF_VERIFY_SSL: False,
    }
    assert config_entry.title == "neu.example.com"


async def test_reconfigure_rejects_other_instance(
    hass: HomeAssistant, mock_metrics: AsyncMock, metrics_doc, config_entry
) -> None:
    config_entry.add_to_hass(hass)
    mock_metrics.return_value = {**metrics_doc, "instance_id": "someone-else"}
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "https://other.example.com"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_instance"
    assert config_entry.data[CONF_URL] == URL


def _keyed_by_url(hass: HomeAssistant) -> MockConfigEntry:
    """Entry set up from a server that did not report an instance_id yet."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="sw.example.com",
        unique_id=URL,
        data={CONF_URL: URL, CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: True},
    )
    entry.add_to_hass(hass)
    return entry


async def test_reconfigure_entry_keyed_by_url_after_server_update(
    hass: HomeAssistant, mock_metrics: AsyncMock
) -> None:
    # The server reports an instance_id by now; the entry keeps its URL key for good.
    entry = _keyed_by_url(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "https://neu.example.com"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_URL] == "https://neu.example.com"
    assert entry.unique_id == URL


async def test_user_flow_same_url_as_entry_keyed_by_url(hass: HomeAssistant, mock_metrics: AsyncMock) -> None:
    _keyed_by_url(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: f"{URL}/", CONF_API_TOKEN: TOKEN}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    mock_metrics.assert_not_called()


async def test_same_instance_takes_over_certificate_check(
    hass: HomeAssistant, mock_metrics: AsyncMock, config_entry
) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: "https://sw-intern.example.com", CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: False},
    )
    assert result["reason"] == "already_configured"
    assert config_entry.data[CONF_URL] == "https://sw-intern.example.com"
    assert config_entry.data[CONF_VERIFY_SSL] is False
