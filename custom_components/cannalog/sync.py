"""Collects mapped sensor values and pushes interval averages to Cannalog.

For every mapping (Cannalog target + metric <- HA entity) a time-weighted
average is accumulated from state changes. At each aligned interval boundary
(e.g. :00, :05, :10 for 5 minutes) one reading per mapping is queued with that
boundary as its timestamp, then the queue is uploaded. The queue is persisted,
so readings survive Cannalog outages and HA restarts; the API dedupes on
(target, metric, timestamp), so retries are always safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event, async_track_utc_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import CannalogApi, CannalogAuthError, CannalogConnectionError, CannalogError
from .const import (
    DEFAULT_INTERVAL,
    DEFAULT_SNAPSHOT_INTERVAL,
    OPT_CAMERAS,
    OPT_SNAPSHOT_INTERVAL,
    DOMAIN,
    MAX_QUEUE,
    OPT_INTERVAL,
    OPT_MAPPINGS,
    SIGNAL_STATUS,
    TEMPERATURE_METRICS,
    UPLOAD_CHUNK,
)

_LOGGER = logging.getLogger(__name__)
STORAGE_VERSION = 1


def convert(metric: str, state: State | None) -> float | None:
    """State -> float in Cannalog's canonical unit, or None if unusable."""
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
        return None
    try:
        value = float(state.state)
    except ValueError:
        return None
    unit = (state.attributes.get("unit_of_measurement") or "").strip()

    if metric in TEMPERATURE_METRICS and unit == UnitOfTemperature.FAHRENHEIT:
        return round((value - 32) * 5 / 9, 3)
    if metric == "ec":
        # Canonical EC is mS/cm. Accept µS/cm (and the common 'us/cm' spellings).
        if unit.lower().replace("μ", "µ") in ("µs/cm", "us/cm"):
            return round(value / 1000, 4)
        if not unit and value > 20:  # unitless and clearly µS/cm
            return round(value / 1000, 4)
    return value


@dataclass
class _Accumulator:
    entity_id: str
    target_type: str
    target_id: str
    metric: str
    value: float | None = None
    since: datetime | None = None
    weighted: float = 0.0
    seconds: float = 0.0

    def add_until(self, when: datetime) -> None:
        if self.value is not None and self.since is not None:
            secs = max((when - self.since).total_seconds(), 0.0)
            self.weighted += self.value * secs
            self.seconds += secs
        self.since = when

    def take_mean(self, when: datetime) -> float | None:
        self.add_until(when)
        mean = self.weighted / self.seconds if self.seconds > 0 else self.value
        self.weighted = 0.0
        self.seconds = 0.0
        return mean


class CannalogSync:
    """One per config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: CannalogApi) -> None:
        self.hass = hass
        self.entry = entry
        self.api = api
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.queue")
        self._queue: list[dict[str, Any]] = []
        self._acc: dict[str, list[_Accumulator]] = {}
        self._unsubs: list = []
        self._uploading = False
        self.last_upload: datetime | None = None
        self.last_error: str | None = None
        self.total_sent = 0
        # Whether Cannalog has been told the current set of mapped targets.
        # Sent on start (which includes every reload after Configure is
        # saved) and retried before uploads until it succeeds.
        self._mappings_reported = False
        self.last_snapshot: datetime | None = None
        self.last_snapshot_error: str | None = None
        self._snapping = False

    # ------------------------------------------------------------- lifecycle
    @property
    def mappings(self) -> list[dict[str, str]]:
        return list(self.entry.options.get(OPT_MAPPINGS, []))

    @property
    def interval(self) -> int:
        return int(self.entry.options.get(OPT_INTERVAL, DEFAULT_INTERVAL))

    @property
    def cameras(self) -> list[dict[str, str]]:
        return list(self.entry.options.get(OPT_CAMERAS, []))

    @property
    def snapshot_interval(self) -> int:
        return int(self.entry.options.get(OPT_SNAPSHOT_INTERVAL, DEFAULT_SNAPSHOT_INTERVAL))

    @property
    def queued(self) -> int:
        return len(self._queue)

    async def async_start(self) -> None:
        stored = await self._store.async_load()
        if stored and isinstance(stored.get("queue"), list):
            self._queue = stored["queue"][-MAX_QUEUE:]

        now = dt_util.utcnow()
        for m in self.mappings:
            acc = _Accumulator(m["entity_id"], m["target_type"], m["target_id"], m["metric"])
            acc.value = convert(acc.metric, self.hass.states.get(acc.entity_id))
            acc.since = now
            self._acc.setdefault(acc.entity_id, []).append(acc)

        if self._acc:
            self._unsubs.append(
                async_track_state_change_event(self.hass, list(self._acc), self._on_state)
            )
        minutes = list(range(0, 60, self.interval))
        self._unsubs.append(
            async_track_utc_time_change(self.hass, self._on_tick, minute=minutes, second=0)
        )
        if self.cameras:
            every = self.snapshot_interval
            if every >= 60:
                self._unsubs.append(async_track_utc_time_change(
                    self.hass, self._on_snapshot_tick, hour=list(range(0, 24, every // 60)), minute=0, second=30))
            else:
                self._unsubs.append(async_track_utc_time_change(
                    self.hass, self._on_snapshot_tick, minute=list(range(0, 60, every)), second=30))
            # One frame straight away, so a newly mapped camera shows up in the app.
            self.hass.async_create_task(self.async_snapshots())
        _LOGGER.debug("Cannalog: %d mappings, %d-min interval, %d queued",
                      len(self.mappings), self.interval, len(self._queue))
        self.hass.async_create_task(self.async_report_mappings())
        if self._queue:
            self.hass.async_create_task(self.async_upload())

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self._store.async_save({"queue": self._queue})

    # --------------------------------------------------------------- events
    @callback
    def _on_state(self, event: Event[EventStateChangedData]) -> None:
        when = event.time_fired
        for acc in self._acc.get(event.data["entity_id"], []):
            acc.add_until(when)
            acc.value = convert(acc.metric, event.data["new_state"])

    @callback
    def _on_tick(self, now: datetime) -> None:
        boundary = now.replace(second=0, microsecond=0)
        stamp = boundary.isoformat().replace("+00:00", "Z")
        for accs in self._acc.values():
            for acc in accs:
                mean = acc.take_mean(now)
                if mean is None:
                    continue
                self._queue.append({
                    "target_type": acc.target_type,
                    "target_id": acc.target_id,
                    "metric": acc.metric,
                    "value": round(mean, 4),
                    "recorded_at": stamp,
                })
        if len(self._queue) > MAX_QUEUE:
            del self._queue[: len(self._queue) - MAX_QUEUE]
        self._store.async_delay_save(lambda: {"queue": self._queue}, 30)
        self.hass.async_create_task(self.async_upload())

    # ------------------------------------------------------------ snapshots
    @callback
    def _on_snapshot_tick(self, now: datetime) -> None:
        self.hass.async_create_task(self.async_snapshots())

    async def async_snapshots(self) -> None:
        """Grab one frame per mapped camera and upload it. Not queued: Cannalog
        keeps only the latest frame, so a missed one is simply replaced by the
        next tick's."""
        if self._snapping or not self.cameras:
            return
        from homeassistant.components.camera import async_get_image  # camera may load after us

        self._snapping = True
        try:
            for cam in self.cameras:
                try:
                    image = await async_get_image(self.hass, cam["entity_id"], timeout=20)
                    stamp = dt_util.utcnow().replace(microsecond=0).isoformat().replace("+00:00", "Z")
                    result = await self.api.push_snapshot(
                        cam["environment_id"], image.content, image.content_type, stamp)
                    if result.get("stored") is False:
                        _LOGGER.debug("Cannalog skipped %s frame: %s", cam["entity_id"], result.get("reason"))
                    self.last_snapshot = dt_util.utcnow()
                    self.last_snapshot_error = None
                except CannalogAuthError as err:
                    self.last_snapshot_error = f"auth: {err}"
                    return  # the readings upload path starts reauth
                except CannalogError as err:
                    self.last_snapshot_error = str(err)
                    _LOGGER.warning("Cannalog snapshot upload failed for %s: %s", cam["entity_id"], err)
                except Exception as err:  # noqa: BLE001 - camera unavailable / HomeAssistantError
                    self.last_snapshot_error = f"{cam['entity_id']}: {err}"
                    _LOGGER.warning("Could not get a frame from %s: %s", cam["entity_id"], err)
        finally:
            self._snapping = False
            self._notify()

    # ------------------------------------------------------------- mappings
    async def async_report_mappings(self) -> None:
        targets = sorted(
            {(m["target_type"], m["target_id"]) for m in self.mappings}
        )
        try:
            await self.api.report_mappings(
                [{"target_type": t, "target_id": i} for t, i in targets]
            )
            self._mappings_reported = True
        except CannalogAuthError:
            pass  # the upload path starts reauth
        except CannalogConnectionError as err:
            _LOGGER.debug("Cannalog unreachable, mappings report retried later: %s", err)
        except CannalogError as err:
            # An older Cannalog server without this endpoint: nothing to retry.
            _LOGGER.debug("Cannalog did not accept the mappings report: %s", err)
            self._mappings_reported = True

    # --------------------------------------------------------------- upload
    async def async_upload(self) -> None:
        if not self._mappings_reported:
            await self.async_report_mappings()
        if self._uploading or not self._queue:
            self._notify()
            return
        self._uploading = True
        try:
            while self._queue:
                chunk = self._queue[:UPLOAD_CHUNK]
                result = await self.api.push_readings(chunk)
                del self._queue[: len(chunk)]
                self.total_sent += int(result.get("inserted", 0))
                if result.get("rejected"):
                    _LOGGER.warning("Cannalog rejected %s readings (unknown target or invalid value)",
                                    result["rejected"])
            self.last_upload = dt_util.utcnow()
            self.last_error = None
        except CannalogAuthError as err:
            self.last_error = f"auth: {err}"
            _LOGGER.error("Cannalog rejected the integration token (%s) - re-pair required", err)
            self.entry.async_start_reauth(self.hass)
        except CannalogConnectionError as err:
            self.last_error = str(err)
            _LOGGER.warning("Cannalog unreachable, %d readings queued: %s", len(self._queue), err)
        except CannalogError as err:
            # 4xx other than auth: the batch itself is bad. Drop it so it cannot
            # block every reading queued behind it forever.
            self.last_error = str(err)
            _LOGGER.error("Cannalog refused a batch, dropping %d readings: %s",
                          min(len(self._queue), UPLOAD_CHUNK), err)
            del self._queue[:UPLOAD_CHUNK]
        finally:
            self._uploading = False
            self._store.async_delay_save(lambda: {"queue": self._queue}, 5)
            self._notify()

    def _notify(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_STATUS.format(self.entry.entry_id))
