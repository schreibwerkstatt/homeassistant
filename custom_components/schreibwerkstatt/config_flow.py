"""Config, reauth and options flow for Schreibwerkstatt."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_API_TOKEN, CONF_SCAN_INTERVAL, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import (
    SchreibwerkstattAuthError,
    SchreibwerkstattClient,
    SchreibwerkstattConnectionError,
    SchreibwerkstattNotSupportedError,
    SchreibwerkstattRedirectError,
    SchreibwerkstattSchemaError,
    normalize_url,
)
from .const import CONF_DASHBOARD_USERS, DEFAULT_SCAN_INTERVAL, DOMAIN, MAX_SCAN_INTERVAL, MIN_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): str,
        vol.Required(CONF_API_TOKEN): str,
        vol.Optional(CONF_VERIFY_SSL, default=True): bool,
    }
)
REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_API_TOKEN): str})
RECONFIGURE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): str,
        vol.Optional(CONF_VERIFY_SSL, default=True): bool,
    }
)


class _Fetch:
    """Outcome of one test fetch: the document, or an error key and its placeholders."""

    def __init__(
        self, doc: dict[str, Any] | None = None, error: str | None = None, location: str = ""
    ) -> None:
        self.doc = doc
        self.error = error
        self.placeholders = {"location": location}


async def _async_fetch(hass: HomeAssistant, url: str, token: str, verify_ssl: bool) -> _Fetch:
    """Try one fetch."""
    session = async_get_clientsession(hass, verify_ssl=verify_ssl)
    try:
        return _Fetch(await SchreibwerkstattClient(session, url, token).async_get_metrics())
    except SchreibwerkstattAuthError:
        return _Fetch(error="invalid_auth")
    except SchreibwerkstattNotSupportedError:
        return _Fetch(error="not_supported")
    except SchreibwerkstattSchemaError:
        return _Fetch(error="unsupported_schema")
    except SchreibwerkstattRedirectError as err:
        return _Fetch(error="redirect", location=err.location)
    except SchreibwerkstattConnectionError:
        return _Fetch(error="cannot_connect")
    except Exception:  # noqa: BLE001 - surface anything else as "unknown"
        _LOGGER.exception("Unexpected error while contacting Schreibwerkstatt")
        return _Fetch(error="unknown")


def _title(url: str) -> str:
    return urlparse(url).hostname or "Schreibwerkstatt"


class SchreibwerkstattConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a Schreibwerkstatt instance by URL and API token."""

    VERSION = 1
    # 1.2: unique IDs without the user_name label.
    MINOR_VERSION = 2

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"location": ""}
        if user_input is not None:
            url = normalize_url(user_input[CONF_URL])
            token = user_input[CONF_API_TOKEN].strip()
            verify = user_input.get(CONF_VERIFY_SSL, True)
            fetch = await _async_fetch(self.hass, url, token, verify)
            placeholders = fetch.placeholders
            if fetch.error:
                errors["base"] = fetch.error
            else:
                assert fetch.doc is not None
                # instance_id survives URL changes; older servers fall back to the URL.
                await self.async_set_unique_id(fetch.doc.get("instance_id") or url)
                self._abort_if_unique_id_configured(updates={CONF_URL: url})
                return self.async_create_entry(
                    title=_title(url),
                    data={CONF_URL: url, CONF_API_TOKEN: token, CONF_VERIFY_SSL: verify},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The token was revoked or expired."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"location": ""}
        if user_input is not None:
            token = user_input[CONF_API_TOKEN].strip()
            fetch = await _async_fetch(
                self.hass, entry.data[CONF_URL], token, entry.data.get(CONF_VERIFY_SSL, True)
            )
            placeholders = fetch.placeholders
            if fetch.error:
                errors["base"] = fetch.error
            else:
                return self.async_update_reload_and_abort(entry, data_updates={CONF_API_TOKEN: token})
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={**placeholders, "url": entry.data[CONF_URL]},
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """New address (or certificate check) for the same server; the token stays."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"location": ""}
        if user_input is not None:
            url = normalize_url(user_input[CONF_URL])
            verify = user_input.get(CONF_VERIFY_SSL, True)
            fetch = await _async_fetch(self.hass, url, entry.data[CONF_API_TOKEN], verify)
            placeholders = fetch.placeholders
            if fetch.error:
                errors["base"] = fetch.error
            else:
                assert fetch.doc is not None
                # Servers without instance_id are known by their URL, which is what changes.
                if instance_id := fetch.doc.get("instance_id"):
                    await self.async_set_unique_id(instance_id)
                    self._abort_if_unique_id_mismatch(reason="wrong_instance")
                old_title = _title(entry.data[CONF_URL])
                return self.async_update_reload_and_abort(
                    entry,
                    title=_title(url) if entry.title == old_title else entry.title,
                    data_updates={CONF_URL: url, CONF_VERIFY_SSL: verify},
                )
        suggested = user_input or {
            CONF_URL: entry.data[CONF_URL],
            CONF_VERIFY_SSL: entry.data.get(CONF_VERIFY_SSL, True),
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(RECONFIGURE_SCHEMA, suggested),
            errors=errors,
            description_placeholders=placeholders,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SchreibwerkstattOptionsFlow()


class SchreibwerkstattOptionsFlow(OptionsFlow):
    """Polling interval and the people the dashboard shows in full."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        schema: dict[Any, Any] = {
            vol.Required(
                CONF_SCAN_INTERVAL, default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ): vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL))
        }
        people = self._people(options.get(CONF_DASHBOARD_USERS, []))
        if people:
            schema[
                vol.Optional(
                    CONF_DASHBOARD_USERS, description={"suggested_value": options.get(CONF_DASHBOARD_USERS)}
                )
            ] = SelectSelector(
                SelectSelectorConfig(options=people, multiple=True, mode=SelectSelectorMode.LIST)
            )
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))

    def _people(self, chosen: list[str]) -> list[SelectOptionDict]:
        """Users the server reports (token with per-user values), plus chosen ones it no longer does."""
        coordinator = getattr(self.config_entry, "runtime_data", None)
        users = dict(coordinator.data.users) if coordinator and coordinator.data else {}
        for email in chosen:
            users.setdefault(email, email)
        return [
            SelectOptionDict(value=email, label=name)
            for email, name in sorted(users.items(), key=lambda u: u[1].casefold())
        ]
