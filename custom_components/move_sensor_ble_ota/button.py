"""Кнопки: прошить самым свежим .bin из папки / отменить."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEFAULT_ESPHOME_CONFIG, DEFAULT_TIMEOUT_MIN, DOMAIN
from .entity import OtaEntity
from .ota import OtaError


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    mgr = hass.data[DOMAIN][entry.entry_id]
    # v1.15: Flash / Build & flash / Cancel заменены сущностью обновления «Firmware» (update.py);
    # службы flash / build_and_flash / cancel остаются
    async_add_entities([ReadSettingsButton(mgr), ResetWifiButton(mgr), IdentifyButton(mgr)])


class FlashButton(OtaEntity, ButtonEntity):

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "ota_flash", "Flash firmware (BLE)")

    async def async_press(self) -> None:
        try:
            await self.mgr.async_start()
        except OtaError as err:
            raise HomeAssistantError(str(err)) from err


class BuildFlashButton(OtaEntity, ButtonEntity):
    """Собрать YAML этого датчика (move-sensor-ble-<device id>.yaml) в аддоне ESPHome и сразу прошить по BLE."""


    def __init__(self, mgr) -> None:
        super().__init__(mgr, "ota_build_flash", "Build & flash firmware (BLE)")

    async def async_press(self) -> None:
        from . import async_flash_gate, start_build  # noqa: PLC0415

        await async_flash_gate(self.mgr, True)
        start_build(self.hass, self.mgr, DEFAULT_ESPHOME_CONFIG, True, DEFAULT_TIMEOUT_MIN)


class CancelButton(OtaEntity, ButtonEntity):

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "ota_cancel", "Cancel OTA")

    async def async_press(self) -> None:
        await self.mgr.async_cancel()


class ReadSettingsButton(OtaEntity, ButtonEntity):
    """Прочитать фактические настройки с датчика в ближайшее окно."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "settings_read", "Read settings (BLE)")

    async def async_press(self) -> None:
        await self.mgr.async_request_read()


class IdentifyButton(OtaEntity, ButtonEntity):
    """Помигать светодиодом датчика 10 с — найти нужный из нескольких (2026-11-15).
    Уходит по BLE в ближайшее пробуждение (движение перед датчиком будит его сразу)."""

    _attr_device_class = ButtonDeviceClass.IDENTIFY
    _attr_entity_category = EntityCategory.DIAGNOSTIC   # v1.20: правило HA — Identify всегда «diagnostic»

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "identify", "Identify (BLE)")

    @property
    def extra_state_attributes(self) -> dict:
        return {"device_id": self.mgr.device_id} | (self.mgr.identify or {})

    async def async_press(self) -> None:
        await self.mgr.async_request_identify()


class ResetWifiButton(OtaEntity, ButtonEntity):
    """Сбросить сохранённую сеть Wi-Fi на сеть из прошивки — по BLE в ближайшее окно пробуждения.
    Датчик ставит флаг и мигает LED 6 раз; сеть меняется при входе в сервисный режим (или сразу, если он включён)."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "wifi_reset", "Reset Wi-Fi to default (BLE)")

    @property
    def extra_state_attributes(self) -> dict:
        return self.mgr.wifi_reset or {}

    async def async_press(self) -> None:
        await self.mgr.async_request_wifi_reset()
