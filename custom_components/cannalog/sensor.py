"""Diagnostic sensors: last upload, queued readings, mapped sensors."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_USER_NAME, DOMAIN, SIGNAL_STATUS, VERSION
from .sync import CannalogSync


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    sync: CannalogSync = entry.runtime_data
    async_add_entities([
        LastUploadSensor(sync), QueuedSensor(sync), MappedSensor(sync), SentSensor(sync),
        LastSnapshotSensor(sync),
    ])


class _Base(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    key = ""

    def __init__(self, sync: CannalogSync) -> None:
        self.sync = sync
        entry = sync.entry
        self._attr_unique_id = f"{entry.entry_id}_{self.key}"
        self._attr_translation_key = self.key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Cannalog ({entry.data.get(CONF_USER_NAME) or 'account'})",
            manufacturer="Cannalog",
            model="Home Assistant link",
            sw_version=VERSION,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://cannalog.co.za",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_STATUS.format(self.sync.entry.entry_id), self._update))

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()


class LastUploadSensor(_Base):
    key = "last_upload"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:cloud-upload"

    @property
    def native_value(self):
        return self.sync.last_upload

    @property
    def extra_state_attributes(self):
        return {"last_error": self.sync.last_error, "interval_minutes": self.sync.interval}


class QueuedSensor(_Base):
    key = "queued"
    _attr_icon = "mdi:tray-full"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "readings"

    @property
    def native_value(self):
        return self.sync.queued


class SentSensor(_Base):
    key = "sent"
    _attr_icon = "mdi:counter"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "readings"

    @property
    def native_value(self):
        return self.sync.total_sent


class MappedSensor(_Base):
    key = "mapped"
    _attr_icon = "mdi:link-variant"
    _attr_native_unit_of_measurement = "sensors"

    @property
    def native_value(self):
        return len(self.sync.mappings)

    @property
    def extra_state_attributes(self):
        return {"mappings": [
            f"{m.get('target_name', m['target_id'])} / {m['metric']} <- {m['entity_id']}"
            for m in self.sync.mappings
        ]}


class LastSnapshotSensor(_Base):
    key = "last_snapshot"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:camera-outline"

    @property
    def native_value(self):
        return self.sync.last_snapshot

    @property
    def extra_state_attributes(self):
        return {
            "last_error": self.sync.last_snapshot_error,
            "interval_minutes": self.sync.snapshot_interval,
            "cameras": [f"{c.get('environment_name', c['environment_id'])} <- {c['entity_id']}"
                        for c in self.sync.cameras],
        }
