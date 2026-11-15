"""Thin async client for the Cannalog API."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

TIMEOUT = aiohttp.ClientTimeout(total=30)


class CannalogError(Exception):
    """Base error."""


class CannalogAuthError(CannalogError):
    """Token or pairing code rejected."""


class CannalogConnectionError(CannalogError):
    """Cannalog unreachable or returned a server error (retry later)."""


async def _request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    token: str | None = None,
    json: Any = None,
) -> Any:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with session.request(method, url, json=json, headers=headers, timeout=TIMEOUT) as resp:
            if resp.status in (401, 403):
                raise CannalogAuthError(await _message(resp))
            if resp.status >= 400:
                msg = await _message(resp)
                if resp.status >= 500 or resp.status == 429:
                    raise CannalogConnectionError(f"{resp.status}: {msg}")
                raise CannalogError(f"{resp.status}: {msg}")
            return await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise CannalogConnectionError(str(err)) from err


async def _message(resp: aiohttp.ClientResponse) -> str:
    try:
        return (await resp.json()).get("message", resp.reason)
    except Exception:  # noqa: BLE001 - non-JSON error page
        return resp.reason or "error"


class CannalogApi:
    """Authenticated client (integration token)."""

    def __init__(self, session: aiohttp.ClientSession, base_url: str, token: str) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        self._token = token

    @staticmethod
    async def pair(
        session: aiohttp.ClientSession,
        base_url: str,
        code: str,
        ha_name: str,
        ha_instance_id: str,
        version: str,
    ) -> dict[str, Any]:
        """Exchange a pairing code for an integration token."""
        return await _request(
            session,
            "POST",
            f"{base_url.rstrip('/')}/integrations/ha/pair",
            json={
                "code": code,
                "ha_name": ha_name,
                "ha_instance_id": ha_instance_id,
                "integration_version": version,
            },
        )

    async def get_targets(self) -> dict[str, Any]:
        return await _request(self._session, "GET", f"{self._base}/integrations/ha/targets", self._token)

    async def push_readings(self, readings: list[dict[str, Any]]) -> dict[str, Any]:
        return await _request(
            self._session,
            "POST",
            f"{self._base}/integrations/ha/readings",
            self._token,
            json={"readings": readings},
        )

    async def report_mappings(self, targets: list[dict[str, str]]) -> dict[str, Any]:
        """Tell Cannalog which environments / reservoirs this HA is configured
        to send readings for, so the app shows LIVE only for those."""
        return await _request(
            self._session,
            "PUT",
            f"{self._base}/integrations/ha/mappings",
            self._token,
            json={"targets": targets},
        )
