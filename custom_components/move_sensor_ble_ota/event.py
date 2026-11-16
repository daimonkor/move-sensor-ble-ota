"""Сущность-событие «Settings change» (v1.15): каждое изменение настроек датчика — отдельная точка
в истории и запись в журнале. Типы: saved_wifi / saved_ble (записал HA), changed_on_sensor (правку
сделали на датчике — сайт или ESPHome — HA принял), restored (датчик потерял настройки, HA вернул свои).
Атрибуты: changes {ключ: {from, to}}, summary, channel, settings_revision."""
from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SETTINGS_EVENT_TYPES, SIGNAL_SETTINGS_EVENT
from .entity import OtaEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([SettingsChangeEvent(hass.data[DOMAIN][entry.entry_id])])


class SettingsChangeEvent(OtaEntity, EventEntity):
    _attr_event_types = SETTINGS_EVENT_TYPES
    _attr_icon = "mdi:cog-transfer"

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "settings_change", "Settings change")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_SETTINGS_EVENT.format(self.mgr.entry.entry_id), self._on_event))

    @callback
    def _on_event(self, kind: str, data: dict) -> None:
        self._trigger_event(kind, data)
        self.async_write_ha_state()
