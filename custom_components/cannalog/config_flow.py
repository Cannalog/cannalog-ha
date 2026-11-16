"""Config flow: pair with a code from the Cannalog app, then map sensors in Options."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import instance_id
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .api import CannalogApi, CannalogAuthError, CannalogConnectionError, CannalogError
from .const import (
    CONF_API_URL,
    CONF_CONNECTION_ID,
    CONF_PAIRING_CODE,
    CONF_TOKEN,
    CONF_USER_ID,
    CONF_USER_NAME,
    DEFAULT_API_URL,
    DEFAULT_INTERVAL,
    DOMAIN,
    CAMERA_FIELD,
    DEFAULT_SNAPSHOT_INTERVAL,
    INTERVAL_CHOICES,
    METRICS,
    OPT_CAMERAS,
    OPT_INTERVAL,
    OPT_LIVE_AUDIO,
    OPT_MAPPINGS,
    OPT_PUMPS,
    OPT_SNAPSHOT_INTERVAL,
    PUMP_DOMAINS,
    PUMP_FIELD,
    PUMP_ML_FIELD,
    SNAPSHOT_INTERVAL_CHOICES,
    VERSION,
)

_LOGGER = logging.getLogger(__name__)


async def _pair(flow, api_url: str, code: str) -> tuple[dict[str, Any] | None, str | None]:
    """Returns (pair result, error key)."""
    hass = flow.hass
    try:
        result = await CannalogApi.pair(
            async_get_clientsession(hass),
            api_url,
            code,
            hass.config.location_name or "Home Assistant",
            await instance_id.async_get(hass),
            VERSION,
        )
        return result, None
    except CannalogAuthError:
        return None, "invalid_code"
    except CannalogConnectionError:
        return None, "cannot_connect"
    except CannalogError:
        return None, "invalid_code"
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Unexpected error pairing with Cannalog")
        return None, "unknown"


class CannalogConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            api_url = user_input[CONF_API_URL].strip().rstrip("/")
            result, error = await _pair(self, api_url, user_input[CONF_PAIRING_CODE])
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(result["user_id"])
                self._abort_if_unique_id_configured(
                    updates={CONF_TOKEN: result["token"], CONF_CONNECTION_ID: result["connection_id"],
                             CONF_API_URL: api_url})
                return self.async_create_entry(
                    title=f"Cannalog ({result.get('user_name') or 'account'})",
                    data={
                        CONF_API_URL: api_url,
                        CONF_TOKEN: result["token"],
                        CONF_CONNECTION_ID: result["connection_id"],
                        CONF_USER_ID: result["user_id"],
                        CONF_USER_NAME: result.get("user_name"),
                    },
                    options={OPT_MAPPINGS: [], OPT_INTERVAL: DEFAULT_INTERVAL,
                             OPT_CAMERAS: [], OPT_SNAPSHOT_INTERVAL: DEFAULT_SNAPSHOT_INTERVAL,
                             OPT_PUMPS: []},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_PAIRING_CODE): TextSelector(),
                vol.Required(CONF_API_URL, default=DEFAULT_API_URL): TextSelector(),
            }),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            result, error = await _pair(self, entry.data[CONF_API_URL], user_input[CONF_PAIRING_CODE])
            if error:
                errors["base"] = error
            elif result["user_id"] != entry.data[CONF_USER_ID]:
                errors["base"] = "wrong_account"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_TOKEN: result["token"], CONF_CONNECTION_ID: result["connection_id"]},
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PAIRING_CODE): TextSelector()}),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the Cannalog server address, keeping the token and sensor mappings.

        The current token is checked against the new server first; if that
        server doesn't know it, a pairing code from the app can be given instead.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            api_url = user_input[CONF_API_URL].strip().rstrip("/")
            code = (user_input.get(CONF_PAIRING_CODE) or "").strip()
            if code:
                result, error = await _pair(self, api_url, code)
                if error:
                    errors["base"] = error
                elif result["user_id"] != entry.data[CONF_USER_ID]:
                    errors["base"] = "wrong_account"
                else:
                    return self.async_update_reload_and_abort(entry, data_updates={
                        CONF_API_URL: api_url, CONF_TOKEN: result["token"],
                        CONF_CONNECTION_ID: result["connection_id"]})
            else:
                api = CannalogApi(async_get_clientsession(self.hass), api_url, entry.data[CONF_TOKEN])
                try:
                    await api.get_targets()
                except CannalogAuthError:
                    errors["base"] = "token_not_accepted"
                except CannalogConnectionError:
                    errors["base"] = "cannot_connect"
                except CannalogError:
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_update_reload_and_abort(entry, data_updates={CONF_API_URL: api_url})

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({
                vol.Required(CONF_API_URL, default=entry.data.get(CONF_API_URL, DEFAULT_API_URL)): TextSelector(),
                vol.Optional(CONF_PAIRING_CODE): TextSelector(),
            }),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return CannalogOptionsFlow()


class CannalogOptionsFlow(OptionsFlow):
    """Map HA sensors to Cannalog environments / reservoirs."""

    def __init__(self) -> None:
        self._targets: dict[str, dict[str, str]] = {}
        self._target_key: str | None = None

    def _mappings(self) -> list[dict[str, str]]:
        return list(self.config_entry.options.get(OPT_MAPPINGS, []))

    def _save(self, **changes: Any) -> ConfigFlowResult:
        return self.async_create_entry(data={**self.config_entry.options, **changes})

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["map_target", "settings"])

    async def async_step_map_target(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._target_key = user_input["target"]
            return await self.async_step_sensors()

        sync = self.config_entry.runtime_data
        try:
            data = await sync.api.get_targets()
        except CannalogAuthError:
            return self.async_abort(reason="auth_failed")
        except CannalogError:
            return self.async_abort(reason="cannot_connect")

        mapped = {f"{m['target_type']}:{m['target_id']}" for m in self._mappings()}
        mapped |= {f"environment:{c['environment_id']}" for c in self.config_entry.options.get(OPT_CAMERAS, [])}
        mapped |= {f"reservoir:{p['reservoir_id']}" for p in self.config_entry.options.get(OPT_PUMPS, [])}
        options: list[SelectOptionDict] = []
        for ttype, items, icon in (("environment", data.get("environments", []), "🌱"),
                                   ("reservoir", data.get("reservoirs", []), "💧")):
            for t in items:
                key = f"{ttype}:{t['id']}"
                self._targets[key] = {"type": ttype, "id": t["id"], "name": t["name"]}
                tick = " ✓" if key in mapped else ""
                options.append(SelectOptionDict(value=key, label=f"{icon} {t['name']} ({ttype}){tick}"))
        if not options:
            return self.async_abort(reason="no_targets")

        return self.async_show_form(
            step_id="map_target",
            data_schema=vol.Schema({
                vol.Required("target"): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)),
            }),
            errors=errors,
        )

    async def async_step_sensors(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        target = self._targets[self._target_key]
        metrics = METRICS[target["type"]]
        current = {m["metric"]: m["entity_id"] for m in self._mappings()
                   if m["target_type"] == target["type"] and m["target_id"] == target["id"]}

        if user_input is not None:
            keep = [m for m in self._mappings()
                    if not (m["target_type"] == target["type"] and m["target_id"] == target["id"])]
            for metric in metrics:
                entity_id = user_input.get(metric)
                if entity_id:
                    keep.append({
                        "target_type": target["type"],
                        "target_id": target["id"],
                        "target_name": target["name"],
                        "metric": metric,
                        "entity_id": entity_id,
                    })
            changes: dict[str, Any] = {OPT_MAPPINGS: keep}
            if target["type"] == "environment":
                cams = [c for c in self.config_entry.options.get(OPT_CAMERAS, [])
                        if c["environment_id"] != target["id"]]
                if user_input.get(CAMERA_FIELD):
                    cams.append({"environment_id": target["id"], "environment_name": target["name"],
                                 "entity_id": user_input[CAMERA_FIELD]})
                changes[OPT_CAMERAS] = cams
            if target["type"] == "reservoir":
                pumps = [p for p in self.config_entry.options.get(OPT_PUMPS, [])
                         if p["reservoir_id"] != target["id"]]
                if user_input.get(PUMP_FIELD):
                    rate = user_input.get(PUMP_ML_FIELD)
                    pumps.append({"reservoir_id": target["id"], "reservoir_name": target["name"],
                                  "entity_id": user_input[PUMP_FIELD],
                                  "ml_per_minute": float(rate) if rate else None})
                changes[OPT_PUMPS] = pumps
            return self._save(**changes)

        schema: dict = {}
        for metric in metrics:
            schema[vol.Optional(metric, description={"suggested_value": current.get(metric)})] = \
                EntitySelector(EntitySelectorConfig(domain="sensor"))
        if target["type"] == "environment":
            camera = next((c["entity_id"] for c in self.config_entry.options.get(OPT_CAMERAS, [])
                           if c["environment_id"] == target["id"]), None)
            schema[vol.Optional(CAMERA_FIELD, description={"suggested_value": camera})] = \
                EntitySelector(EntitySelectorConfig(domain="camera"))
        if target["type"] == "reservoir":
            pump = next((p for p in self.config_entry.options.get(OPT_PUMPS, [])
                         if p["reservoir_id"] == target["id"]), None) or {}
            schema[vol.Optional(PUMP_FIELD, description={"suggested_value": pump.get("entity_id")})] = \
                EntitySelector(EntitySelectorConfig(domain=PUMP_DOMAINS))
            schema[vol.Optional(PUMP_ML_FIELD, description={"suggested_value": pump.get("ml_per_minute")})] = \
                NumberSelector(NumberSelectorConfig(min=0, max=100000, step="any",
                                                    unit_of_measurement="ml/min",
                                                    mode=NumberSelectorMode.BOX))
        return self.async_show_form(
            step_id=f"sensors_{target['type']}",
            data_schema=vol.Schema(schema),
            description_placeholders={"target": target["name"]},
            last_step=True,
        )

    # The form's step_id differs per target type (so each gets its own labels);
    # both submit back into async_step_sensors.
    async def async_step_sensors_environment(self, user_input=None) -> ConfigFlowResult:
        return await self.async_step_sensors(user_input)

    async def async_step_sensors_reservoir(self, user_input=None) -> ConfigFlowResult:
        return await self.async_step_sensors(user_input)

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(**{OPT_INTERVAL: int(user_input[OPT_INTERVAL]),
                                  OPT_SNAPSHOT_INTERVAL: int(user_input[OPT_SNAPSHOT_INTERVAL]),
                                  OPT_LIVE_AUDIO: bool(user_input.get(OPT_LIVE_AUDIO, False))})
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema({
                vol.Required(OPT_INTERVAL, default=str(self.config_entry.options.get(OPT_INTERVAL, DEFAULT_INTERVAL))):
                    SelectSelector(SelectSelectorConfig(
                        options=[SelectOptionDict(value=str(i), label=f"{i} min") for i in INTERVAL_CHOICES],
                        mode=SelectSelectorMode.DROPDOWN)),
                vol.Required(OPT_SNAPSHOT_INTERVAL, default=str(self.config_entry.options.get(
                    OPT_SNAPSHOT_INTERVAL, DEFAULT_SNAPSHOT_INTERVAL))):
                    SelectSelector(SelectSelectorConfig(
                        options=[SelectOptionDict(value=str(i), label=f"{i} min") for i in SNAPSHOT_INTERVAL_CHOICES],
                        mode=SelectSelectorMode.DROPDOWN)),
                vol.Required(OPT_LIVE_AUDIO, default=bool(self.config_entry.options.get(OPT_LIVE_AUDIO, False))):
                    BooleanSelector(),
            }),
        )
