"""Сущность обновления «Firmware» (v1.15): одна вместо Flash / Build & flash / Cancel /
OTA status / OTA progress / Firmware version.

Новая версия = YAML датчика (дерево move_sensor_ble/, components/bthome/, secrets.yaml) изменён
после последней прошивки. «Установить» — сборка в аддоне ESPHome, проверка ключа OTA в образе, прошивка:
по Wi-Fi сразу, если ESPHome-устройство на связи (сервисный режим), иначе по BLE в ближайшее пробуждение;
оборвался Wi-Fi — продолжает по BLE. Задание переживает перезапуск HA; отмена — и во время сборки.
Прошивка кнопкой «Установить» в ESPHome Builder идёт мимо интеграции и ключ не проверяет.
"""
from __future__ import annotations

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, ST_FLASHING
from .entity import OtaEntity
from .ota import OtaError


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([FirmwareUpdate(hass.data[DOMAIN][entry.entry_id])])


class FirmwareUpdate(OtaEntity, UpdateEntity):
    _attr_supported_features = (UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
                                | UpdateEntityFeature.RELEASE_NOTES)
    _attr_entity_category = EntityCategory.CONFIG
    _attr_title = "Move Sensor BLE"

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "firmware", "Firmware")

    @property
    def installed_version(self) -> str | None:
        return self.mgr.firmware_version

    @property
    def latest_version(self) -> str | None:
        return self.mgr.latest_firmware

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        # версии — метки сборок, не semver: другая = новее
        return latest_version != installed_version

    @property
    def in_progress(self) -> bool:
        return self.mgr.firmware_in_progress

    @property
    def update_percentage(self) -> int | None:
        return self.mgr.progress if self.mgr.status == ST_FLASHING else None

    @property
    def release_summary(self) -> str | None:
        m = self.mgr
        if m.firmware_in_progress:
            return f"{m.status}: {m.progress} %"
        if m.error:
            return f"Ошибка: {m.error}"[:250]
        if self.latest_version and self.latest_version != self.installed_version:
            return "YAML датчика изменён — новая сборка. Ключ OTA проверяется после сборки."
        if m.last_flash_summary:
            return f"Last flash: {m.last_flash_summary}"
        return None

    @property
    def extra_state_attributes(self) -> dict:
        lf = self.mgr.last_flash or {}
        return {"last_flash": self.mgr.last_flash_summary, "last_flash_channel": lf.get("channel"),
                "last_flash_result": lf.get("result")}

    async def async_release_notes(self) -> str | None:
        m = self.mgr
        lines = [
            "Сборка из YAML датчика в аддоне ESPHome и прошивка: **по Wi-Fi сразу**, если датчик в сервисном "
            "режиме (WiFi включён), иначе **по BLE** в ближайшее пробуждение (движение перед датчиком будит его). "
            "Оборвался Wi-Fi — продолжит по BLE. Перезапуск HA задание не отменяет. "
            "Если эта сборка уже на датчике (тот же хеш образа) — прошивки не будет.",
            "",
            f"- На датчике: `{m.firmware_version}`",
            f"- YAML сейчас: `{m.config_hash}`, прошит: `{m.flashed_hash}`",
        ]
        if m.last_flash_summary:
            lines.append(f"- Последняя прошивка: {m.last_flash_summary}")
        elif m.last_result:
            lines.append(f"- Последняя прошивка: {m.last_result}")
        if m.error:
            lines.append(f"- Ошибка: {m.error}")
        lines += ["", "Отмена — служба `move_sensor_ble_ota.cancel` или кнопка на панели Move Sensor BLE."]
        return "\n".join(lines)

    async def async_install(self, version: str | None, backup: bool, **kwargs) -> None:
        if self.mgr.firmware_in_progress:
            raise HomeAssistantError("сборка или прошивка уже идёт")
        try:
            await self.mgr.async_auth_gate("build and flash")
        except OtaError as err:
            raise HomeAssistantError(str(err)) from err

        # сборка (0,5–5 мин) — в рамках «Установить»: ошибка сборки или чужой ключ OTA всплывают
        # сразу (как просил Дима: попап при прошивке); дальше прошивка ждёт окно BLE в фоне
        try:
            await self.mgr.async_build_and_flash(start=True)
        except OtaError as err:
            raise HomeAssistantError(str(err)) from err
