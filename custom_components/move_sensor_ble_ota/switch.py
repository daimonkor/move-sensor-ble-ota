"""Переключатель «Motion tracking LED»: задаётся в HA, применяется в ближайшее окно."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SETTINGS
from .entity import OtaEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    mgr = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LedSwitch(mgr), AutoWindowSwitch(mgr)])


class LedSwitch(OtaEntity, SwitchEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, mgr) -> None:
        name, *_rest, icon = SETTINGS["led"]
        super().__init__(mgr, "setting_led", f"{name} (BLE)")
        self._attr_icon = icon

    @property
    def is_on(self) -> bool | None:
        v = self.mgr.value("led")
        return None if v is None else bool(v)

    @property
    def extra_state_attributes(self) -> dict:
        return {"applied_value": self.mgr.applied.get("led"), "pending": "led" in self.mgr.pending()}

    async def async_turn_on(self, **kwargs) -> None:
        await self.mgr.async_set_setting("led", True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.mgr.async_set_setting("led", False)



class AutoWindowSwitch(OtaEntity, SwitchEntity):
    """Авто-окно рекламы: интеграция подбирает Motion/Climate window по RSSI и пропускам.

    Ручное изменение окна выключает авто. Настройка интеграции; на датчик уходят сами окна."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:signal-variant"

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "auto_window", "Auto advertising window")

    @property
    def is_on(self) -> bool:
        return self.mgr.auto_window

    @property
    def extra_state_attributes(self) -> dict:
        return self.mgr.window_attributes()

    async def async_turn_on(self, **kwargs) -> None:
        await self.mgr.async_set_auto_window(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.mgr.async_set_auto_window(False)
