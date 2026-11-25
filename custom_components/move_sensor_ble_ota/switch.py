"""Переключатель «Motion tracking LED»: задаётся в HA, применяется в ближайшее окно."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from homeassistant.helpers.dispatcher import async_dispatcher_connect

from . import selfupdate
from .const import DOMAIN, SETTINGS
from .entity import OtaEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    mgr = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LedSwitch(mgr), AutoWindowSwitch(mgr), AutoUpdateSwitch(mgr)])


class LedSwitch(OtaEntity, SwitchEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, mgr) -> None:
        name, *_rest, icon = SETTINGS["led"]
        super().__init__(mgr, "setting_led", f"{name} (BLE)")

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


class AutoUpdateSwitch(OtaEntity, SwitchEntity):
    """v1.29.2: ставить новые версии интеграции из Marketplace самой (по умолчанию выключено).

    Новая версия показывается всегда (selfupdate); с переключателем она ещё и устанавливается —
    перезапуск HA остаётся за пользователем. Настройка на всю интеграцию, не на датчик."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:update"

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "integration_auto_update", "Auto-update integration")

    @property
    def is_on(self) -> bool:
        return selfupdate.auto_install(self.hass)

    @property
    def extra_state_attributes(self) -> dict:
        return {"update_entity": selfupdate.update_entity_id(self.hass)}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, selfupdate.SIGNAL_AUTO, self.async_write_ha_state))

    async def async_turn_on(self, **kwargs) -> None:
        await selfupdate.async_set_auto_install(self.hass, True)

    async def async_turn_off(self, **kwargs) -> None:
        await selfupdate.async_set_auto_install(self.hass, False)
