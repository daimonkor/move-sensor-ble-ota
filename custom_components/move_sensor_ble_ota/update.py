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

from homeassistant.util import dt as dt_util

from . import fwnotes
from .const import DOMAIN, ST_FLASHING
from .entity import OtaEntity
from .ota import OtaError, ota_status_text

CHANGELOG_URL = "https://github.com/daimonkor/move-sensor-ble-ota/blob/main/esphome/move_sensor_ble/CHANGELOG.md"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([FirmwareUpdate(hass.data[DOMAIN][entry.entry_id])])


class FirmwareUpdate(OtaEntity, UpdateEntity):
    _attr_supported_features = (UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
                                | UpdateEntityFeature.RELEASE_NOTES | UpdateEntityFeature.BACKUP)
    # v1.18: ссылка «Список изменений» в окне обновления — CHANGELOG прошивки на GitHub
    _attr_release_url = CHANGELOG_URL
    _attr_entity_category = EntityCategory.CONFIG
    _attr_title = "Move Sensor BLE firmware"   # v1.15.4: в «Обновлениях» видно, что это прошивка датчика

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "firmware", "Firmware")

    @property
    def installed_version(self) -> str | None:
        label = self.mgr.build_label(self.mgr.firmware_version)   # «build 16.11 13:48»
        v = self.mgr.fw_version_flashed                           # v1.16.2: «1.3.1 · build 16.11 13:48»
        return f"{v} · {label}" if v and label else label

    @property
    def latest_version(self) -> str | None:
        m = self.mgr
        if m.firmware_version is None:
            return None
        if not m.new_build_label:
            # v1.27: YAML тот же, но в HA новый ESPHome Builder (новая платформа esp32) — пересобрать
            tc = m.toolchain_update
            return f"{self.installed_version} · ESPHome {tc}" if tc else self.installed_version
        if m.fw_version_new and m.fw_version_new != m.fw_version_flashed:
            return f"{m.fw_version_new} · new build"                # версия из CHANGELOG прошивки
        return m.new_build_label

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
        if self.latest_version and self.latest_version != self.installed_version and m.toolchain_update \
                and not m.new_build_label:
            was = (m.toolchain or {}).get("esphome")
            return (f"ESPHome Builder обновлён: прошивка датчика собрана в ESPHome {was}, сейчас {m.toolchain_update} "
                    "(новая платформа esp32). «Установить» пересоберёт и прошьёт.")[:250]
        if self.latest_version and self.latest_version != self.installed_version:
            line = fwnotes.summary_line(m.fw_sections, m.fw_changes)
            if line:
                return line[:250]
            files = m.changed_files_short
            return ((f"Изменено: {files}. " if files else "YAML датчика изменён — новая сборка. ")
                    + "Подробно — «Примечания к выпуску».")[:250]
        if m.last_flash_summary:
            return f"Last flash: {m.last_flash_summary}"
        return None

    @property
    def extra_state_attributes(self) -> dict:
        lf = self.mgr.last_flash or {}
        imgs = self.mgr.fw_images
        stored = (f"{imgs[0]} · {imgs[1] / 1048576:.1f} MB (keep {self.mgr.keep_images})" if imgs else None)
        b = self.mgr.backups[0] if self.mgr.backups else None
        return {"ota_status": ota_status_text(self.mgr.status, self.mgr.progress, self.mgr.error,
                                              self.mgr.last_build),   # v1.18.1
                "last_flash": self.mgr.last_flash_summary, "last_flash_channel": lf.get("channel"),
                "last_flash_result": lf.get("result"), "stored_images": stored,
                # v1.18.1: «1.4.1 · build 17.11 00:49» (раньше версия повторялась: «1.4.1 · 1.4.1 v2026…»)
                "backup": (" · ".join(x for x in (b.get("fw_version"),
                                                  self.mgr.build_label(b.get("firmware_version"))
                                                  or b.get("firmware_version")) if x) if b else None),
                "backups": len(self.mgr.backups),
                # v1.27: чем собрана прошивка на датчике и что сейчас в ESPHome Builder
                "esphome_flashed": (self.mgr.toolchain or {}).get("esphome"),
                "esp_idf_flashed": (self.mgr.toolchain or {}).get("idf"),
                "esphome_builder": self.mgr.builder_info.get("version"),
                "esphome_builder_latest": self.mgr.builder_info.get("version_latest"),
                "esphome_update": self.mgr.toolchain_update}

    async def async_release_notes(self) -> str | None:
        m = self.mgr

        def fmt(ts: float) -> str:
            return dt_util.as_local(dt_util.utc_from_timestamp(ts)).strftime("%d.%m %H:%M")

        pending = bool(self.latest_version and self.latest_version != self.installed_version)
        if pending:
            lines = fwnotes.render_user(m.fw_sections, m.fw_changes)
        else:   # обновления нет — что в прошивке на датчике (верхний раздел её CHANGELOG)
            lines = fwnotes.render_user(m.fw_current_section, None) if m.fw_current_section else []
        tc = m.toolchain_update
        if pending and tc:   # v1.27
            t = m.toolchain or {}
            lines = [
                "### Новый ESPHome Builder",
                f"В Home Assistant стоит ESPHome **{tc}**, а прошивка на датчике собрана в ESPHome "
                f"{t.get('esphome') or '?'}" + (f" (ESP-IDF {t['idf']})" if t.get("idf") else "") + ". "
                "Вместе с ESPHome обновляется и платформа esp32 (ESP-IDF). «Установить» пересоберёт прошивку "
                "новой версией и прошьёт датчик — настройки сохранятся.",
                "",
                *lines,
            ]
        tech = fwnotes.render(m.fw_changes, m.fw_changed_since, fmt)
        lines += [*tech, ""] if tech else []
        lines += [
            "### Как ставится",
            "Сборка из YAML датчика в аддоне ESPHome и прошивка: **по Wi-Fi сразу**, если датчик в сервисном "
            "режиме (WiFi включён), иначе **по BLE** в ближайшее пробуждение (движение перед датчиком будит его). "
            "Оборвался Wi-Fi — продолжит по BLE. Перезапуск HA задание не отменяет. "
            "Если эта сборка уже на датчике (тот же хеш образа) — прошивки не будет.",
            "",
            f"- На датчике: {m.build_label(m.firmware_version)} (`{m.firmware_version}`)",
            f"- YAML сейчас: `{m.config_hash}`, прошит: `{m.flashed_hash}`",
        ]
        t = m.toolchain or {}
        if t.get("esphome") or t.get("idf"):
            lines.append(f"- Собрана в: ESPHome {t.get('esphome') or '?'}" + (f" · ESP-IDF {t['idf']}" if t.get("idf") else ""))
        if m.builder_info.get("version"):
            lines.append(f"- ESPHome Builder сейчас: {m.builder_info['version']}"
                         + (f" (доступно {m.builder_info['version_latest']})" if m.builder_info.get("update_available") else ""))
        if m.last_flash_summary:
            lines.append(f"- Последняя прошивка: {m.last_flash_summary}")
        elif m.last_result:
            lines.append(f"- Последняя прошивка: {m.last_result}")
        if m.error:
            lines.append(f"- Ошибка: {m.error}")
        lines += ["", "Отмена — служба `move_sensor_ble_ota.cancel` или кнопка на панели Move Sensor BLE.", "",
                  "### Резервная копия и откат",
                  "Галочка «Резервная копия» перед «Установить» сохраняет прошивку, что сейчас на датчике, и копию её "
                  "YAML в `<папка прошивок>/backup/` (последние 3). Откат — служба `move_sensor_ble_ota.rollback`: "
                  "прошьёт последнюю копию тем же путём (Wi-Fi или BLE), после чего «Firmware» снова предложит текущий YAML."]
        if m.backups:
            b = m.backups[0]
            lines.append(f"- Последняя копия: {b.get('fw_version') or '?'} (`{b.get('firmware_version')}`)")
        return "\n".join(lines)

    async def async_install(self, version: str | None, backup: bool, **kwargs) -> None:
        if self.mgr.firmware_in_progress:
            raise HomeAssistantError("сборка или прошивка уже идёт")
        try:
            await self.mgr.async_auth_gate("build and flash")
        except OtaError as err:
            raise HomeAssistantError(str(err)) from err
        if backup:   # v1.18: прошивка, что сейчас на датчике, — в backup/ для отката
            try:
                await self.mgr.async_backup_current()
            except OtaError as err:
                raise HomeAssistantError(f"резервная копия: {err}") from err

        # сборка (0,5–5 мин) — в рамках «Установить»: ошибка сборки или чужой ключ OTA всплывают
        # сразу (как просил Дима: попап при прошивке); дальше прошивка ждёт окно BLE в фоне
        try:
            await self.mgr.async_build_and_flash(start=True)
        except OtaError as err:
            raise HomeAssistantError(str(err)) from err
