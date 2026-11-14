"""PIR stuck: «занято» держится дольше порога (2026-11-14)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .entity import OtaEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([PirStuckSensor(hass.data[DOMAIN][entry.entry_id])])


class PirStuckSensor(OtaEntity, BinarySensorEntity):
    """ON, если датчик показывает «занято» непрерывно дольше «PIR stuck after».
    Причины: отвалился провод сигнала PIR (во сне по LOW на D1 подтяжка вверх — пин читается HIGH),
    тепло/солнце/сквозняк в поле линзы, просадка питания SR602, влага."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:motion-sensor-off"

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "pir_stuck", "PIR stuck")

    @property
    def is_on(self) -> bool:
        return self.mgr.pir_stuck

    @property
    def extra_state_attributes(self) -> dict:
        since = self.mgr.occupied_since if self.mgr.occupied else None
        return {
            "occupied_since": since.isoformat() if since else None,
            "occupied_min": round((dt_util.utcnow() - since).total_seconds() / 60, 1) if since else 0,
            "threshold_min": self.mgr.stuck_threshold_min,
        }
