"""Cannalog - push grow-room sensor readings from Home Assistant to Cannalog."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CannalogApi
from .const import CONF_API_URL, CONF_TOKEN
from .sync import CannalogSync

PLATFORMS = [Platform.SENSOR]

type CannalogConfigEntry = ConfigEntry[CannalogSync]


async def async_setup_entry(hass: HomeAssistant, entry: CannalogConfigEntry) -> bool:
    api = CannalogApi(async_get_clientsession(hass), entry.data[CONF_API_URL], entry.data[CONF_TOKEN])
    sync = CannalogSync(hass, entry, api)
    await sync.async_start()
    entry.runtime_data = sync
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: CannalogConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: CannalogConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_stop()
    return ok
