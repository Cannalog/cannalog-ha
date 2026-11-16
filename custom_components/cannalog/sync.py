"""Collects mapped sensor values and pushes interval averages to Cannalog.

For every mapping (Cannalog target + metric <- HA entity) a time-weighted
average is accumulated from state changes. At each aligned interval boundary
(e.g. :00, :05, :10 for 5 minutes) one reading per mapping is queued with that
boundary as its timestamp, then the queue is uploaded. The queue is persisted,
so readings survive Cannalog outages and HA restarts; the API dedupes on
(target, metric, timestamp), so retries are always safe.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event, async_track_utc_time_change
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import CannalogApi, CannalogAuthError, CannalogConnectionError, CannalogError, CannalogGoneError
from .const import (
    DEFAULT_INTERVAL,
    DEFAULT_SNAPSHOT_INTERVAL,
    LIVE_FRAME_INTERVAL,
    LIVE_FRAME_SIZE,
    LIVE_POLL_TIMEOUT,
    OPT_CAMERAS,
    OPT_LIVE_AUDIO,
    OPT_SNAPSHOT_INTERVAL,
    DOMAIN,
    MAX_QUEUE,
    OPT_INTERVAL,
    OPT_MAPPINGS,
    OPT_PUMPS,
    MAX_RUNS_QUEUE,
    MIN_RUN_SECONDS,
    PUMP_RUNNING_STATES,
    RUNS_CHUNK,
    SIGNAL_STATUS,
    TEMPERATURE_METRICS,
    UPLOAD_CHUNK,
)

_LOGGER = logging.getLogger(__name__)
STORAGE_VERSION = 1


def _iso(when: datetime) -> str:
    return dt_util.as_utc(when).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
        # Live view: environment_id -> frame-loop task, plus the poll loop.
        self._live: dict[str, asyncio.Task] = {}
        self._live_poller: asyncio.Task | None = None
        self.live_viewing: list[str] = []
        # Irrigation: runs waiting to be sent, and pumps currently running
        # (entity_id -> ISO start). Both persisted with the readings queue, so
        # a run spanning an HA restart or a Cannalog outage is not lost.
        self._runs: list[dict[str, Any]] = []
        self._pump_on: dict[str, str] = {}
        self._sending_runs = False
        self.runs_sent = 0

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
    def pumps(self) -> list[dict[str, Any]]:
        return list(self.entry.options.get(OPT_PUMPS, []))

    def _stored(self) -> dict[str, Any]:
        return {"queue": self._queue, "runs": self._runs, "pump_on": self._pump_on}

    @property
    def queued(self) -> int:
        return len(self._queue)

    async def async_start(self) -> None:
        stored = await self._store.async_load()
        if stored and isinstance(stored.get("queue"), list):
            self._queue = stored["queue"][-MAX_QUEUE:]
        if stored and isinstance(stored.get("runs"), list):
            self._runs = stored["runs"][-MAX_RUNS_QUEUE:]
        if stored and isinstance(stored.get("pump_on"), dict):
            self._pump_on = dict(stored["pump_on"])
        self._start_pumps()

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
            # One frame once HA has finished starting (the camera platform may
            # load after us), so a newly mapped camera shows up in the app.
            async def _first_snapshot(_hass: HomeAssistant) -> None:
                await self.async_snapshots()

            self._unsubs.append(async_at_started(self.hass, _first_snapshot))
            # Live view: wait (outbound long-poll) for the app to ask for it.
            self._live_poller = self.entry.async_create_background_task(
                self.hass, self._live_poll_loop(), f"{DOMAIN}_live_poll")
        _LOGGER.debug("Cannalog: %d mappings, %d-min interval, %d queued",
                      len(self.mappings), self.interval, len(self._queue))
        self.hass.async_create_task(self.async_report_mappings())
        if self._queue:
            self.hass.async_create_task(self.async_upload())
        if self._runs:
            self.hass.async_create_task(self.async_send_runs())

    async def async_stop(self) -> None:
        if self._live_poller:
            self._live_poller.cancel()
        for task in self._live.values():
            task.cancel()
        self._live.clear()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self._store.async_save(self._stored())

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
        self._store.async_delay_save(self._stored, 30)
        self.hass.async_create_task(self.async_upload())
        if self._runs:
            self.hass.async_create_task(self.async_send_runs())

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
        self._snapping = True
        try:
            for cam in self.cameras:
                try:
                    content, content_type = await self._grab_image(cam["entity_id"], timeout=20)
                    stamp = dt_util.utcnow().replace(microsecond=0).isoformat().replace("+00:00", "Z")
                    result = await self.api.push_snapshot(
                        cam["environment_id"], content, content_type, stamp)
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

    # ------------------------------------------------------------ live view
    async def _live_poll_loop(self) -> None:
        """Ask Cannalog which environments are being watched and run one live
        task per watched environment: real video (ffmpeg -> SRT to Cannalog's
        relay) when the server offers it and the camera has a stream, otherwise
        still frames. Outbound only, like everything else."""
        cams = {c["environment_id"]: c["entity_id"] for c in self.cameras}
        backoff = 5
        have: list[str] = []
        modes: dict[str, tuple] = {}  # env -> the mode its running task was started with
        while True:
            try:
                # `have` = the server's own last answer ("env:key" when it sent
                # stream keys), so an environment watched without a camera here
                # doesn't make every poll return at once, and a restarted
                # session (new key) is noticed.
                answer = await self.api.live_poll(have, LIVE_POLL_TIMEOUT)
                backoff = 5
            except asyncio.CancelledError:
                raise
            except CannalogAuthError:
                _LOGGER.debug("Cannalog live view: token rejected, stopping poll")
                return
            except CannalogError as err:
                # Older server without live view (404) or an outage: retry slowly.
                _LOGGER.debug("Cannalog live view poll failed: %s", err)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
                continue
            watched, video = answer["environments"], answer["video"]
            have = [f"{e}:{video[e]['stream_key']}" if e in video else e for e in watched]

            wanted: dict[str, tuple] = {}
            for env_id in watched:
                if env_id not in cams:
                    continue
                source = await self._stream_source(cams[env_id]) if env_id in video else None
                wanted[env_id] = ("video", source, video[env_id]["srt_url"]) if source else ("frames",)

            for env_id in list(self._live):
                if modes.get(env_id) != wanted.get(env_id):
                    self._live.pop(env_id).cancel()
                    modes.pop(env_id, None)
            for env_id, mode in wanted.items():
                task = self._live.get(env_id)
                if task is None or task.done():
                    coro = (self._live_video(env_id, mode[1], mode[2]) if mode[0] == "video"
                            else self._live_frames(env_id, cams[env_id]))
                    self._live[env_id] = self.entry.async_create_background_task(
                        self.hass, coro, f"{DOMAIN}_live_{env_id}")
                    modes[env_id] = mode
            if sorted(wanted) != self.live_viewing:
                self.live_viewing = sorted(wanted)
                self._notify()

    async def _stream_source(self, entity_id: str) -> str | None:
        """The camera's video stream URL (RTSP etc.), or None for still-only cameras."""
        from homeassistant.components.camera import async_get_stream_source

        try:
            return await async_get_stream_source(self.hass, entity_id)
        except Exception as err:  # noqa: BLE001 - camera missing / no stream support
            _LOGGER.debug("No stream source for %s: %s", entity_id, err)
            return None

    def _ffmpeg_binary(self) -> str:
        try:
            from homeassistant.components.ffmpeg import get_ffmpeg_manager

            return get_ffmpeg_manager(self.hass).binary
        except Exception:  # noqa: BLE001 - ffmpeg integration not set up
            return "ffmpeg"

    async def _live_video(self, environment_id: str, source: str, srt_url: str) -> None:
        """Forward the camera's stream to Cannalog's relay as-is (no re-encode,
        so it costs the host very little) until cancelled. ffmpeg is restarted
        if it exits (camera or network hiccup) while still being watched."""
        cmd = [self._ffmpeg_binary(), "-hide_banner", "-nostdin", "-loglevel", "error"]
        if source.startswith("rtsp"):
            cmd += ["-rtsp_transport", "tcp"]
        cmd += ["-i", source, "-map", "0:v:0", "-c:v", "copy"]
        if self.entry.options.get(OPT_LIVE_AUDIO):
            # The camera's first audio track, if it has one ("?"), as-is.
            cmd += ["-map", "0:a:0?", "-c:a", "copy"]
        else:
            cmd += ["-an"]
        cmd += ["-f", "mpegts", srt_url]
        failures = 0
        _LOGGER.debug("Cannalog live video started for %s", environment_id)
        try:
            while failures < 10:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                try:
                    started = asyncio.get_running_loop().time()
                    _, err = await proc.communicate()
                except asyncio.CancelledError:
                    if proc.returncode is None:
                        proc.terminate()
                        try:
                            await asyncio.wait_for(proc.wait(), 5)
                        except asyncio.TimeoutError:
                            proc.kill()
                    raise
                ran = asyncio.get_running_loop().time() - started
                failures = 0 if ran > 30 else failures + 1
                _LOGGER.debug("Cannalog live video ffmpeg exited (%s) after %.0fs: %s",
                              proc.returncode, ran, (err or b"").decode(errors="replace")[-300:])
                await asyncio.sleep(min(2 * failures, 10))
        finally:
            if self._live.get(environment_id) is asyncio.current_task():
                self._live.pop(environment_id, None)
            _LOGGER.debug("Cannalog live video stopped for %s", environment_id)

    async def _grab_image(self, entity_id: str, timeout: int = 10,
                          size: tuple[int, int] | None = None) -> tuple[bytes, str]:
        """A still from the camera; for stream-only cameras (no still image
        support) one frame is grabbed from its stream with ffmpeg instead."""
        from homeassistant.components.camera import async_get_image  # camera may load after us

        try:
            kw = {"width": size[0], "height": size[1]} if size else {}
            image = await async_get_image(self.hass, entity_id, timeout=timeout, **kw)
            return image.content, image.content_type
        except Exception as err:  # noqa: BLE001 - HomeAssistantError: no still image
            source = await self._stream_source(entity_id)
            if not source:
                raise
            _LOGGER.debug("No still from %s (%s), grabbing a frame from its stream", entity_id, err)
        cmd = [self._ffmpeg_binary(), "-hide_banner", "-nostdin", "-loglevel", "error"]
        if source.startswith("rtsp"):
            cmd += ["-rtsp_transport", "tcp"]
        cmd += ["-i", source, "-frames:v", "1"]
        if size:
            cmd += ["-vf", f"scale='min({size[0]},iw)':-2"]
        cmd += ["-f", "image2", "-c:v", "mjpeg", "-q:v", "4", "pipe:1"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise HomeAssistantError(f"Timed out grabbing a frame from {entity_id}") from None
        if proc.returncode or not out:
            raise HomeAssistantError(
                f"Could not grab a frame from {entity_id}: {(err or b'').decode(errors='replace')[-200:]}")
        return out, "image/jpeg"

    async def _live_frames(self, environment_id: str, entity_id: str) -> None:
        loop = asyncio.get_running_loop()
        failures = 0
        _LOGGER.debug("Cannalog live view started for %s (%s)", environment_id, entity_id)
        try:
            while True:
                began = loop.time()
                try:
                    content, content_type = await self._grab_image(entity_id, 10, LIVE_FRAME_SIZE)
                    await self.api.push_live_frame(environment_id, content, content_type)
                    failures = 0
                except (CannalogGoneError, CannalogAuthError):
                    return  # nobody watching any more / token revoked
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001 - camera or network hiccup
                    failures += 1
                    _LOGGER.debug("Cannalog live frame failed (%s): %s", entity_id, err)
                    if failures >= 10:
                        return
                await asyncio.sleep(max(0.2, LIVE_FRAME_INTERVAL - (loop.time() - began)))
        finally:
            if self._live.get(environment_id) is asyncio.current_task():
                self._live.pop(environment_id, None)
            _LOGGER.debug("Cannalog live view stopped for %s", environment_id)

    # ----------------------------------------------------------- irrigation
    def _start_pumps(self) -> None:
        """Track mapped pumps. A pump that changed while HA was down is
        reconciled from its current state: still running keeps (or gets) a
        start; stopped closes a stored start at the time it changed."""
        entities = {p["entity_id"] for p in self.pumps}
        for entity_id in list(self._pump_on):
            if entity_id not in entities:
                self._pump_on.pop(entity_id)
        for entity_id in entities:
            state = self.hass.states.get(entity_id)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue  # no information yet; the first real state decides
            if state.state in PUMP_RUNNING_STATES:
                self._pump_on.setdefault(entity_id, _iso(state.last_changed))
            elif entity_id in self._pump_on:
                self._close_run(entity_id, state.last_changed)
        if entities:
            self._unsubs.append(async_track_state_change_event(self.hass, list(entities), self._on_pump))

    @callback
    def _on_pump(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        new = event.data["new_state"]
        if new is None or new.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return  # a dropped relay is not a stopped pump; wait for a real state
        running = new.state in PUMP_RUNNING_STATES
        if running and entity_id not in self._pump_on:
            self._pump_on[entity_id] = _iso(new.last_changed)
            self._store.async_delay_save(self._stored, 5)
        elif not running and entity_id in self._pump_on:
            self._close_run(entity_id, new.last_changed)
            self.hass.async_create_task(self.async_send_runs())

    def _close_run(self, entity_id: str, ended: datetime) -> None:
        started = dt_util.parse_datetime(self._pump_on.pop(entity_id))
        self._store.async_delay_save(self._stored, 5)
        if started is None:
            return
        seconds = (ended - started).total_seconds()
        if seconds < MIN_RUN_SECONDS:
            return
        for pump in self.pumps:
            if pump["entity_id"] != entity_id:
                continue
            run: dict[str, Any] = {
                "reservoir_id": pump["reservoir_id"],
                "started_at": _iso(started),
                "duration_seconds": round(seconds),
                "time_zone": self.hass.config.time_zone,
            }
            if pump.get("ml_per_minute"):
                run["water_amount_ml"] = round(float(pump["ml_per_minute"]) * seconds / 60, 1)
            self._runs.append(run)
            _LOGGER.debug("Cannalog: %s ran %.0fs for reservoir %s", entity_id, seconds, pump["reservoir_id"])
        if len(self._runs) > MAX_RUNS_QUEUE:
            del self._runs[: len(self._runs) - MAX_RUNS_QUEUE]

    async def async_send_runs(self) -> None:
        if self._sending_runs or not self._runs:
            return
        self._sending_runs = True
        try:
            while self._runs:
                chunk = self._runs[:RUNS_CHUNK]
                result = await self.api.push_irrigation_runs(chunk)
                del self._runs[: len(chunk)]
                self.runs_sent += int(result.get("inserted", 0))
                if result.get("rejected"):
                    _LOGGER.warning("Cannalog rejected %s pump runs (unknown reservoir or invalid run)",
                                    result["rejected"])
        except CannalogAuthError:
            pass  # the readings upload path starts reauth
        except CannalogConnectionError as err:
            _LOGGER.debug("Cannalog unreachable, %d pump runs queued: %s", len(self._runs), err)
        except CannalogError as err:
            _LOGGER.error("Cannalog refused pump runs, dropping %d: %s",
                          min(len(self._runs), RUNS_CHUNK), err)
            del self._runs[:RUNS_CHUNK]
        finally:
            self._sending_runs = False
            self._store.async_delay_save(self._stored, 5)
            self._notify()

    # ------------------------------------------------------------- mappings
    async def async_report_mappings(self) -> None:
        targets = sorted(
            {(m["target_type"], m["target_id"]) for m in self.mappings}
        )
        try:
            await self.api.report_mappings(
                [{"target_type": t, "target_id": i} for t, i in targets],
                [{"reservoir_id": p["reservoir_id"], "entity_id": p["entity_id"]} for p in self.pumps],
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
            self._store.async_delay_save(self._stored, 5)
            self._notify()

    def _notify(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_STATUS.format(self.entry.entry_id))
