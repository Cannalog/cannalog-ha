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


class CannalogGoneError(CannalogError):
    """410: the thing this request is for has ended (e.g. nobody watching any more)."""


class CannalogConnectionError(CannalogError):
    """Cannalog unreachable or returned a server error (retry later)."""


async def _request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    token: str | None = None,
    json: Any = None,
    data: Any = None,
    timeout: aiohttp.ClientTimeout = TIMEOUT,
) -> Any:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with session.request(method, url, json=json, data=data, headers=headers, timeout=timeout) as resp:
            if resp.status in (401, 403):
                raise CannalogAuthError(await _message(resp))
            if resp.status == 410:
                raise CannalogGoneError(await _message(resp))
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

    async def push_snapshot(
        self, environment_id: str, image: bytes, content_type: str, captured_at: str
    ) -> dict[str, Any]:
        """Upload one camera frame; Cannalog keeps it as the environment's latest."""
        form = aiohttp.FormData()
        form.add_field("environment_id", environment_id)
        form.add_field("captured_at", captured_at)
        ext = "png" if "png" in content_type else "jpg"
        form.add_field("image", image, filename=f"snapshot.{ext}", content_type=content_type or "image/jpeg")
        return await _request(
            self._session, "POST", f"{self._base}/integrations/ha/snapshots", self._token, data=form
        )

    async def live_poll(self, have: list[str], timeout: float) -> dict[str, Any]:
        """Long-poll: environments someone is watching live right now.

        Returns {"environments": [...], "video": {env_id: {"stream_key", "srt_url"}}};
        `video` is empty when the server has no video relay (frames only)."""
        result = await _request(
            self._session, "GET",
            f"{self._base}/integrations/ha/live/poll?have={','.join(sorted(have))}",
            self._token, timeout=aiohttp.ClientTimeout(total=timeout),
        )
        return {
            "environments": list(result.get("environments", [])),
            "video": dict(result.get("video") or {}),
        }

    async def push_live_frame(self, environment_id: str, image: bytes, content_type: str) -> None:
        """One live frame, relayed to the app. Raises CannalogGoneError once nobody is watching."""
        form = aiohttp.FormData()
        form.add_field("environment_id", environment_id)
        ext = "png" if "png" in content_type else "jpg"
        form.add_field("image", image, filename=f"frame.{ext}", content_type=content_type or "image/jpeg")
        await _request(
            self._session, "POST", f"{self._base}/integrations/ha/live/frame", self._token,
            data=form, timeout=aiohttp.ClientTimeout(total=10),
        )

    async def report_mappings(
        self, targets: list[dict[str, str]], pumps: list[dict[str, str]]
    ) -> dict[str, Any]:
        """Tell Cannalog which environments / reservoirs this HA is configured
        to send readings for (the app shows LIVE only for those), and which
        reservoirs are fed by a pump here (no schedule prompts for those)."""
        return await _request(
            self._session,
            "PUT",
            f"{self._base}/integrations/ha/mappings",
            self._token,
            json={"targets": targets, "pumps": pumps},
        )

    async def push_irrigation_runs(self, runs: list[dict[str, Any]]) -> dict[str, Any]:
        """Pump runs; Cannalog logs each as a feed. Retries are safe (deduped)."""
        return await _request(
            self._session,
            "POST",
            f"{self._base}/integrations/ha/irrigation_runs",
            self._token,
            json={"runs": runs},
        )
