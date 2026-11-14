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


type Fetched = tuple[dict[str, Any] | None, dict[str, str], dict[str, str]]


async def _async_fetch(hass: HomeAssistant, url: str, token: str, verify_ssl: bool) -> Fetched:
    """Try one fetch: the document, or form errors and their placeholders."""
    session = async_get_clientsession(hass, verify_ssl=verify_ssl)
    location = ""
    try:
        return await SchreibwerkstattClient(session, url, token).async_get_metrics(), {}, {"location": ""}
    except SchreibwerkstattAuthError:
        error = "invalid_auth"
    except SchreibwerkstattNotSupportedError:
        error = "not_supported"
    except SchreibwerkstattSchemaError:
        error = "unsupported_schema"
    except SchreibwerkstattRedirectError as err:
        error, location = "redirect", err.location
    except SchreibwerkstattConnectionError:
        error = "cannot_connect"
    except Exception:  # noqa: BLE001 - surface anything else as "unknown"
        _LOGGER.exception("Unexpected error while contacting Schreibwerkstatt")
        error = "unknown"
    return None, {"base": error}, {"location": location}


def _title(url: str) -> str:
    return urlparse(url).hostname or "Schreibwerkstatt"


def _keyed_by_url(entry: ConfigEntry) -> bool:
    """Set up from a server without instance_id: the entry's unique ID is its first URL.

    It stays that URL for good, also once the server reports an instance_id: every
    entity's unique ID starts with it.
    """
    return "://" in (entry.unique_id or "")


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
            # Also catches an entry keyed by URL whose server reports an instance_id by now.
            self._async_abort_entries_match({CONF_URL: url})
            doc, errors, placeholders = await _async_fetch(self.hass, url, token, verify)
            if doc is not None:
                # instance_id survives URL changes; older servers fall back to the URL.
                await self.async_set_unique_id(doc.get("instance_id") or url)
                self._abort_if_unique_id_configured(updates={CONF_URL: url, CONF_VERIFY_SSL: verify})
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
            doc, errors, placeholders = await _async_fetch(
                self.hass, entry.data[CONF_URL], token, entry.data.get(CONF_VERIFY_SSL, True)
            )
            if doc is not None:
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
            doc, errors, placeholders = await _async_fetch(self.hass, url, entry.data[CONF_API_TOKEN], verify)
            if doc is not None:
                # An entry keyed by URL cannot tell: its key is the URL, which is what changes.
                if (instance_id := doc.get("instance_id")) and not _keyed_by_url(entry):
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
