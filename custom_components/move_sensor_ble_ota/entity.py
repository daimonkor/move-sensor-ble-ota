"""Общая база сущностей: привязка к устройству BTHome по Bluetooth-адресу."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, SIGNAL_UPDATE
from .ota import OtaManager


class OtaEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, mgr: OtaManager, key: str, name: str) -> None:
        self.mgr = mgr
        self._attr_unique_id = f"{mgr.address}_{key}"
        self._attr_name = name
        # connections совпадает с устройством BTHome — сущности появятся на его странице
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mgr.address)},
            connections={(CONNECTION_BLUETOOTH, mgr.address)},
            name=mgr.name,
            configuration_url="homeassistant://move-sensor-ble-ota-docs",  # документация интеграции
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_UPDATE.format(self.mgr.entry.entry_id), self.async_write_ha_state
            )
        )
