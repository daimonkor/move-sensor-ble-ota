"""Настройка интеграции + загрузка прошивки из интерфейса HA.

Настройки → Устройства и службы → Move Sensor BLE OTA → «Настроить»:
  • «Загрузить прошивку» — выбрать .bin (OTA format или factory), по желанию сразу прошить;
  • «Параметры» — ключ (пусто = из esphome/secrets.yaml) и папка с прошивками.
"""

from __future__ import annotations

import os
import shutil
from typing import Any

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import (
    BooleanSelector,
    FileSelector,
    FileSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import secrets_store
from .const import (CONF_ADDRESS, CONF_API_KEY, CONF_FIRMWARE_DIR, CONF_KEEP_IMAGES, CONF_KEY, CONF_OTA_PASSWORD,
                    DEFAULT_FIRMWARE_DIR, DEFAULT_KEEP_IMAGES, DEFAULT_NAME, DOMAIN, device_id)


def _secret_fields() -> dict:
    """v1.17.0: пароль OTA и ключ API — пусто = оставить текущие / сгенерировать (значения не показываем)."""
    pw = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
    return {vol.Optional(CONF_OTA_PASSWORD, default=""): pw, vol.Optional(CONF_API_KEY, default=""): pw}


async def _save_secrets(hass, user_input: dict, errors: dict) -> bool:
    """Ввод -> secrets.yaml; пусто — текущее или новое случайное. False — ключ API неверного формата."""
    api = (user_input.get(CONF_API_KEY) or "").strip()
    if api and not secrets_store.valid_api_key(api):
        errors[CONF_API_KEY] = "bad_api_key"
        return False
    for name, val in ((secrets_store.OTA_PASSWORD, user_input.get(CONF_OTA_PASSWORD)), (secrets_store.API_KEY, api)):
        await hass.async_add_executor_job(secrets_store.ensure, name, val)
    return True

BTHOME_UUID = "0000fcd2-0000-1000-8000-00805f9b34fb"
CONF_FIRMWARE = "firmware"
CONF_START = "start"


def _norm_mac(value: str) -> str:
    v = value.strip().upper().replace("-", ":")
    if len(v) == 12 and ":" not in v:
        v = ":".join(v[i : i + 2] for i in range(0, 12, 2))
    return v


class MoveSensorBleOtaFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            address = _norm_mac(user_input[CONF_ADDRESS])
            if len(address) != 17:
                errors[CONF_ADDRESS] = "bad_address"
            elif not await _save_secrets(self.hass, user_input, errors):
                pass
            else:
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                # имя по умолчанию — с Device ID (хвост MAC), чтобы различать несколько датчиков
                title = (user_input.get(CONF_NAME) or "").strip()
                if not title or title == DEFAULT_NAME:
                    title = f"{DEFAULT_NAME} {device_id(address)}"
                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_ADDRESS: address,
                        CONF_KEY: (user_input.get(CONF_KEY) or "").strip(),
                        CONF_FIRMWARE_DIR: user_input.get(CONF_FIRMWARE_DIR) or DEFAULT_FIRMWARE_DIR,
                    },
                )

        # подсказки: BTHome-устройства, которые сейчас слышит HA (наш датчик — move-sensor-ble)
        options: list[SelectOptionDict] = []
        dev_reg = dr.async_get(self.hass)
        for info in bluetooth.async_discovered_service_info(self.hass, connectable=False):
            if BTHOME_UUID in info.service_data or BTHOME_UUID in info.service_uuids:
                # имя из реестра устройств (BTHome-устройство в HA), а не BLE-имя: датчик его не рекламирует
                dev = dev_reg.async_get_device(connections={(dr.CONNECTION_BLUETOOTH, info.address)})
                name = (dev and (dev.name_by_user or dev.name)) or (
                    info.name if info.name and info.name.replace("-", ":").upper() != info.address else None
                )
                did = device_id(info.address)
                label = f"{name} · ID {did} ({info.address})" if name else f"ID {did} ({info.address})"
                if not any(o["value"] == info.address for o in options):
                    options.append(SelectOptionDict(value=info.address, label=label))
        options.sort(key=lambda o: (0 if "move" in o["label"].lower() else 1, o["label"]))

        addr_selector = (
            SelectSelector(SelectSelectorConfig(options=options, custom_value=True, mode=SelectSelectorMode.DROPDOWN))
            if options
            else TextSelector()
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS): addr_selector,
                vol.Optional(CONF_NAME, default=DEFAULT_NAME): TextSelector(),
                vol.Optional(CONF_KEY, default=""): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                vol.Optional(CONF_FIRMWARE_DIR, default=DEFAULT_FIRMWARE_DIR): TextSelector(),
                **_secret_fields(),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return MoveSensorBleOtaOptions()


class MoveSensorBleOtaOptions(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["upload", "settings"])

    # ---------- v1.15: вид (скрытые копии, панель) — без перезагрузки интеграции ----------
    # ---------- загрузка прошивки ----------
    async def async_step_upload(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        from .ota import OtaError, load_firmware  # noqa: PLC0415

        errors: dict[str, str] = {}
        placeholders = {"error": ""}
        mgr = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if user_input is not None:
            if mgr is None:
                return self.async_abort(reason="not_loaded")
            fw_dir = mgr.firmware_dir

            def _save(file_id: str) -> str:
                # временный файл HA -> папка прошивок под исходным именем
                with process_uploaded_file(self.hass, file_id) as tmp:
                    name = os.path.basename(tmp.name) or "firmware.bin"
                    if not name.lower().endswith(".bin"):
                        raise OtaError(f"{name}: нужен .bin")
                    load_firmware(str(tmp))  # проверка до копирования
                    os.makedirs(fw_dir, exist_ok=True)
                    dest = os.path.join(fw_dir, name)
                    shutil.copyfile(tmp, dest + ".part")
                    os.replace(dest + ".part", dest)
                    os.utime(dest, None)  # самый свежий — его возьмёт кнопка «Flash firmware (BLE)»
                    return dest

            try:
                path = await self.hass.async_add_executor_job(_save, user_input[CONF_FIRMWARE])
                image = await self.hass.async_add_executor_job(load_firmware, path)
            except OtaError as err:
                errors["base"] = "bad_firmware"
                placeholders["error"] = str(err)
            else:
                started = "нет"
                if user_input.get(CONF_START):
                    try:
                        await mgr.async_start(path)
                        started = "да — ждём пробуждения датчика (PIR или телеметрия)"
                    except OtaError as err:
                        started = f"не запущено: {err}"
                await mgr.async_prune_firmware(extra={path})
                # abort, а не create_entry: сохранение опций перезагрузило бы интеграцию и прервало прошивку
                return self.async_abort(
                    reason="firmware_uploaded",
                    description_placeholders={
                        "file": os.path.basename(path),
                        "version": image.version,
                        "project": image.project,
                        "size": f"{len(image.data) / 1024:.0f}",
                        "factory": " (вырезано из factory.bin)" if image.from_factory else "",
                        "started": started,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_FIRMWARE): FileSelector(FileSelectorConfig(accept=".bin")),
                vol.Optional(CONF_START, default=True): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="upload", data_schema=schema, errors=errors, description_placeholders=placeholders
        )

    # ---------- параметры ----------
    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and await _save_secrets(self.hass, user_input, errors):
            return self.async_create_entry(
                data={
                    CONF_KEY: (user_input.get(CONF_KEY) or "").strip(),
                    CONF_FIRMWARE_DIR: user_input.get(CONF_FIRMWARE_DIR) or DEFAULT_FIRMWARE_DIR,
                    CONF_KEEP_IMAGES: int(user_input.get(CONF_KEEP_IMAGES) or DEFAULT_KEEP_IMAGES),
                }
            )
        cur = {**self.config_entry.data, **self.config_entry.options}
        schema = vol.Schema(
            {
                vol.Optional(CONF_KEY, default=cur.get(CONF_KEY, "")): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                vol.Optional(CONF_FIRMWARE_DIR, default=cur.get(CONF_FIRMWARE_DIR, DEFAULT_FIRMWARE_DIR)): TextSelector(),
                vol.Optional(CONF_KEEP_IMAGES, default=int(cur.get(CONF_KEEP_IMAGES, DEFAULT_KEEP_IMAGES))):
                    NumberSelector(NumberSelectorConfig(min=1, max=10, step=1, mode=NumberSelectorMode.BOX)),
                **_secret_fields(),
            }
        )
        return self.async_show_form(step_id="settings", data_schema=schema, errors=errors)
