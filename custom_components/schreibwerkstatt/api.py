"""Minimal client for the Schreibwerkstatt metrics endpoints."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
from yarl import URL

from .const import HISTORY_PATH, HISTORY_TIMEOUT, METRICS_PATH, REQUEST_TIMEOUT, SUPPORTED_SCHEMA


class SchreibwerkstattError(Exception):
    """Base error."""


class SchreibwerkstattAuthError(SchreibwerkstattError):
    """Token missing, invalid, revoked or without metrics:read (401/403)."""


class SchreibwerkstattConnectionError(SchreibwerkstattError):
    """Server unreachable, timed out or answered with an unexpected status."""


class SchreibwerkstattRedirectError(SchreibwerkstattConnectionError):
    """Server answered with a redirect; the token is not sent after it."""

    def __init__(self, location: str) -> None:
        super().__init__(f"redirect to {location}")
        self.location = location


class SchreibwerkstattNotSupportedError(SchreibwerkstattError):
    """Server has no /metrics.json (app too old)."""


class SchreibwerkstattSchemaError(SchreibwerkstattError):
    """Server speaks a newer, incompatible /metrics.json contract."""


def normalize_url(url: str) -> str:
    """Strip whitespace, trailing slashes and a pasted metrics path."""
    url = url.strip().rstrip("/")
    for suffix in (METRICS_PATH, "/metrics"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url.rstrip("/")


class SchreibwerkstattClient:
    """Fetches the self-describing metrics document."""

    def __init__(self, session: aiohttp.ClientSession, url: str, token: str) -> None:
        self._session = session
        self._url = normalize_url(url)
        self._token = token.strip()

    @property
    def url(self) -> str:
        """Base URL of the Schreibwerkstatt instance."""
        return self._url

    async def async_get_metrics(self) -> dict[str, Any]:
        """Return the parsed /metrics.json document."""
        return _checked(await self._get(METRICS_PATH, REQUEST_TIMEOUT), "metrics")

    async def async_get_history(self) -> dict[str, Any]:
        """Return the daily series of /metrics/history.json (servers from 4.21 on)."""
        return _checked(await self._get(HISTORY_PATH, HISTORY_TIMEOUT), "series")

    async def _get(self, path: str, timeout: float) -> Any:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        try:
            async with asyncio.timeout(timeout):
                async with self._session.get(
                    self._url + path, headers=headers, allow_redirects=False
                ) as resp:
                    if resp.status in (401, 403):
                        raise SchreibwerkstattAuthError(f"HTTP {resp.status}")
                    if resp.status in (301, 302, 303, 307, 308):
                        # Not followed: the token would go along to wherever it points.
                        location = resp.headers.get("Location", "")
                        target = resp.url.join(URL(location)) if location else resp.url
                        raise SchreibwerkstattRedirectError(str(target).removesuffix(path))
                    if resp.status == 404:
                        raise SchreibwerkstattNotSupportedError(f"no {path}")
                    if resp.status != 200:
                        raise SchreibwerkstattConnectionError(f"HTTP {resp.status}")
                    return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise SchreibwerkstattConnectionError(str(err) or type(err).__name__) from err


def _checked(data: Any, items: str) -> dict[str, Any]:
    """The document if it is one this integration understands."""
    if not isinstance(data, dict) or not isinstance(data.get(items), list):
        raise SchreibwerkstattSchemaError("unexpected document")
    try:
        schema = int(data.get("schema") or 0)
    except (TypeError, ValueError) as err:
        raise SchreibwerkstattSchemaError(f"schema {data.get('schema')!r}") from err
    if schema > SUPPORTED_SCHEMA:
        raise SchreibwerkstattSchemaError(f"schema {schema}")
    return data
