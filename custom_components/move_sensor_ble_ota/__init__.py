"""Move Sensor BLE OTA — прошивка датчика движения по BLE через Bluetooth-прокси.

Загрузка прошивки:
  * положить .bin в /config/esphome/ota/ (берётся самый свежий), или
  * POST /api/move_sensor_ble_ota/upload?filename=<имя>.bin[&start=1]
    (тело — сам файл или multipart, нужен токен администратора).
Запуск: кнопка «Flash firmware (BLE)» или сервис move_sensor_ble_ota.flash.
Сборка + прошивка одной командой (2026-11-14): кнопка «Build & flash firmware (BLE)»,
сервис move_sensor_ble_ota.build_and_flash или POST /api/move_sensor_ble_ota/build
— YAML собирается в аддоне ESPHome Builder (через Supervisor), .bin кладётся в папку
прошивок и сразу уходит на датчик по BLE в ближайшее окно.
Настройки (Detect delay, Keep time, Climate report interval, Voltage factor, Motion
tracking LED) меняются сущностями «… (BLE)»; уходят в то же окно, «Settings status»
показывает applied / pending.
Дальше интеграция ждёт пробуждения датчика (телеметрия по таймеру или PIR),
подключается в окно и заливает образ. Ход — в сенсорах «OTA status» / «OTA progress».
"""

from __future__ import annotations

import logging
import os
import re

from aiohttp import web
import voluptuous as vol

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv

from .const import (
    ATTR_ENTRY_ID,
    ATTR_FILE,
    ATTR_TIMEOUT,
    DEFAULT_TIMEOUT_MIN,
    DOMAIN,
    ATTR_CONFIGURATION,
    ATTR_START,
    DEFAULT_ESPHOME_CONFIG,
    SERVICE_BUILD_FLASH,
    SERVICE_CANCEL,
    SERVICE_FLASH,
    SERVICE_READ_SETTINGS,
    SERVICE_RESET_WIFI,
    SERVICE_FAST_TEST,
)
from .hidden import async_rollback_unhide
from .ota import OtaError, OtaManager, load_firmware

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON, Platform.NUMBER, Platform.SWITCH]

FLASH_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): cv.string,
        vol.Optional(ATTR_FILE): cv.string,
        vol.Optional(ATTR_TIMEOUT, default=DEFAULT_TIMEOUT_MIN): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=24 * 60)
        ),
    }
)
CANCEL_SCHEMA = vol.Schema({vol.Optional(ATTR_ENTRY_ID): cv.string})
BUILD_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): cv.string,
        vol.Optional(ATTR_CONFIGURATION, default=""): cv.string,   # пусто = YAML датчика по Device ID
        vol.Optional(ATTR_START, default=True): cv.boolean,
        vol.Optional(ATTR_TIMEOUT, default=DEFAULT_TIMEOUT_MIN): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=24 * 60)
        ),
    }
)


async def async_flash_gate(mgr: OtaManager, start: bool) -> None:
    """До фоновой сборки: датчик уже отклонил этот ключ -> ошибка сразу (всплывает в UI)."""
    if not start:
        return
    try:
        await mgr.async_auth_gate("build and flash")
    except OtaError as err:
        raise HomeAssistantError(str(err)) from err


def start_build(hass: HomeAssistant, mgr: OtaManager, configuration: str, start: bool, timeout: int) -> None:
    """Сборка + прошивка в фоне (сборка 0.5-5 мин, ожидание окна — до timeout).
    Ход — в «OTA status» (building -> waiting -> flashing -> success) и /status."""
    if mgr.busy or mgr.status == "building":
        raise HomeAssistantError("сборка или прошивка уже идёт")

    async def _run() -> None:
        try:
            await mgr.async_build_and_flash(configuration, timeout, start)
        except OtaError as err:
            _LOGGER.error("%s: build_and_flash: %s", mgr.name, err)

    hass.async_create_background_task(_run(), f"{DOMAIN}_build_{mgr.entry.entry_id}")


def _managers(hass: HomeAssistant) -> dict[str, OtaManager]:
    return hass.data.setdefault(DOMAIN, {})


def _pick(hass: HomeAssistant, entry_id: str | None) -> OtaManager:
    managers = _managers(hass)
    if entry_id:
        if entry_id not in managers:
            raise HomeAssistantError(f"нет записи {entry_id}")
        return managers[entry_id]
    if len(managers) != 1:
        raise HomeAssistantError(f"датчиков {len(managers)} — укажи {ATTR_ENTRY_ID}")
    return next(iter(managers.values()))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    first = not _managers(hass)
    mgr = OtaManager(hass, entry)
    _managers(hass)[entry.entry_id] = mgr

    if first:
        _register_services(hass)
        hass.http.register_view(UploadView(hass))
        hass.http.register_view(StatusView(hass))
        hass.http.register_view(BuildView(hass))
    await _register_docs(hass)

    # настройки из хранилища; если ещё не читали — воркер ждёт окно и читает их с датчика
    await mgr.async_load()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _setup_bthome_names(hass, entry, mgr.address)
    _remove_radio_device(hass, mgr.address)
    # v1.14.2: защита отката — показать то, что скрыла более новая версия (1.15+)
    await async_rollback_unhide(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


# 2026-11-14: понятные имена для сущностей BTHome того же датчика. HA BTHome называет их по типу
# объекта («Count», «Батарея» у battery_low); прошивка имён не передаёт. Меняем только имя в реестре
# (entity_id не трогаем — история и автоматизации целы) и только если пользователь не переименовал сам.
BTHOME_NAMES = {
    ("sensor", "count"): "Wake count",
    ("binary_sensor", "battery"): "Battery low",
}


def _remove_radio_device(hass: HomeAssistant, address: str) -> None:
    """v1.10.1 выносила радио-настройки в под-устройство «… Radio» — убрано (v1.10.2):
    группа «Радио (BLE)» живёт на дашборде. Сущности уже вернулись на основное устройство —
    удаляем опустевшее под-устройство."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er  # noqa: PLC0415

    dev_reg = dr.async_get(hass)
    dev = dev_reg.async_get_device(identifiers={(DOMAIN, f"{address}_radio")})
    if dev is None:
        return
    if er.async_entries_for_device(er.async_get(hass), dev.id, include_disabled_entities=True):
        return
    dev_reg.async_remove_device(dev.id)


def _setup_bthome_names(hass: HomeAssistant, entry: ConfigEntry, address: str) -> None:
    from homeassistant.core import callback  # noqa: PLC0415
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

    reg = er.async_get(hass)
    prefix = f"{address.upper()}-"

    @callback
    def _apply(entity_id: str | None = None) -> None:
        for ent in list(reg.entities.values()):
            if entity_id and ent.entity_id != entity_id:
                continue
            if ent.platform != "bthome" or not (ent.unique_id or "").upper().startswith(prefix):
                continue
            key = ent.unique_id.split("-", 1)[1]
            name = BTHOME_NAMES.get((ent.domain, key))
            if name and ent.name is None:
                reg.async_update_entity(ent.entity_id, name=name)
                _LOGGER.info("BTHome %s -> «%s»", ent.entity_id, name)

    @callback
    def _on_registry(event) -> None:
        if event.data.get("action") == "create":
            _apply(event.data.get("entity_id"))

    _apply()
    entry.async_on_unload(hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _on_registry))


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        mgr = _managers(hass).pop(entry.entry_id, None)
        if mgr:
            await mgr.async_shutdown()
        if not _managers(hass):
            hass.services.async_remove(DOMAIN, SERVICE_FLASH)
            hass.services.async_remove(DOMAIN, SERVICE_CANCEL)
            hass.services.async_remove(DOMAIN, SERVICE_READ_SETTINGS)
    return ok


DOCS_URL = "/move_sensor_ble_ota/docs"
DOCS_PANEL = "move-sensor-ble-ota-docs"


async def _register_docs(hass: HomeAssistant) -> None:
    """Документация (docs/index.html из README.md) прямо в HA: кнопка «Документация» на странице
    интеграции (manifest.documentation) и «Посетить» на странице устройства (скрытая панель)."""
    flag = f"{DOMAIN}_docs_registered"
    if hass.data.get(flag):
        return
    hass.data[flag] = True
    docs_dir = os.path.join(os.path.dirname(__file__), "docs")
    try:
        from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415

        await hass.http.async_register_static_paths([StaticPathConfig(DOCS_URL, docs_dir, False)])
    except ImportError:  # старый HA
        hass.http.register_static_path(DOCS_URL, docs_dir, False)
    from homeassistant.components import frontend  # noqa: PLC0415

    # без sidebar_title панель в боковом меню не показывается, открывается по ссылке
    frontend.async_register_built_in_panel(
        hass, "iframe", frontend_url_path=DOCS_PANEL, config={"url": f"{DOCS_URL}/index.html"}
    )


def _register_services(hass: HomeAssistant) -> None:
    async def flash(call: ServiceCall) -> ServiceResponse:
        mgr = _pick(hass, call.data.get(ATTR_ENTRY_ID))
        try:
            await mgr.async_start(call.data.get(ATTR_FILE), call.data[ATTR_TIMEOUT])
        except OtaError as err:
            raise HomeAssistantError(str(err)) from err
        return {"status": mgr.status, **mgr.attributes()}

    async def cancel(call: ServiceCall) -> None:
        await _pick(hass, call.data.get(ATTR_ENTRY_ID)).async_cancel()

    hass.services.async_register(
        DOMAIN, SERVICE_FLASH, flash, schema=FLASH_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(DOMAIN, SERVICE_CANCEL, cancel, schema=CANCEL_SCHEMA)

    async def build_and_flash(call: ServiceCall) -> ServiceResponse:
        mgr = _pick(hass, call.data.get(ATTR_ENTRY_ID))
        await async_flash_gate(mgr, call.data[ATTR_START])
        start_build(hass, mgr, call.data[ATTR_CONFIGURATION], call.data[ATTR_START], call.data[ATTR_TIMEOUT])
        return {"status": "building", "configuration": call.data[ATTR_CONFIGURATION]}

    hass.services.async_register(
        DOMAIN, SERVICE_BUILD_FLASH, build_and_flash, schema=BUILD_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def read_settings(call: ServiceCall) -> None:
        await _pick(hass, call.data.get(ATTR_ENTRY_ID)).async_request_read()

    hass.services.async_register(DOMAIN, SERVICE_READ_SETTINGS, read_settings, schema=CANCEL_SCHEMA)

    async def reset_wifi(call: ServiceCall) -> None:
        await _pick(hass, call.data.get(ATTR_ENTRY_ID)).async_request_wifi_reset()

    hass.services.async_register(DOMAIN, SERVICE_RESET_WIFI, reset_wifi, schema=CANCEL_SCHEMA)

    # отладка: быстрый тест (датчик без движения просыпается раз в 30 с), minutes=0 — выключить
    async def fast_test(call: ServiceCall) -> ServiceResponse:
        mgr = _pick(hass, call.data.get(ATTR_ENTRY_ID))
        minutes = call.data["minutes"]
        await mgr.async_set_test_mode(minutes > 0, minutes, call.data["tx_window_ms"])
        return {"test_mode": mgr.test_mode}

    hass.services.async_register(
        DOMAIN, SERVICE_FAST_TEST, fast_test,
        schema=vol.Schema({vol.Optional(ATTR_ENTRY_ID): cv.string,
                           vol.Optional("minutes", default=30): vol.All(vol.Coerce(int), vol.Range(min=0, max=120)),
                           # окно рекламы на время теста, мс (0 = как в прошивке) — замер окна для прокси
                           vol.Optional("tx_window_ms", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=10000))}),
        supports_response=SupportsResponse.OPTIONAL,
    )


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class UploadView(HomeAssistantView):
    """Загрузка .bin: POST /api/move_sensor_ble_ota/upload?filename=x.bin[&start=1][&entry_id=...]"""

    url = "/api/move_sensor_ble_ota/upload"
    name = "api:move_sensor_ble_ota:upload"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        user = request.get("hass_user")
        if user is None or not user.is_admin:
            return self.json_message("нужен администратор", status_code=403)
        try:
            mgr = _pick(self.hass, request.query.get("entry_id"))
        except HomeAssistantError as err:
            return self.json_message(str(err), status_code=400)

        filename = request.query.get("filename") or "firmware.bin"
        data: bytes
        if request.content_type.startswith("multipart/"):
            reader = await request.multipart()
            part = await reader.next()
            while part is not None and not getattr(part, "filename", None):
                part = await reader.next()
            if part is None:
                return self.json_message("в multipart нет файла", status_code=400)
            filename = request.query.get("filename") or part.filename
            data = bytes(await part.read(decode=False))
        else:
            data = await request.read()
        filename = _SAFE_NAME.sub("_", os.path.basename(filename))
        if not filename.lower().endswith(".bin"):
            filename += ".bin"
        path = os.path.join(mgr.firmware_dir, filename)

        def _save() -> None:
            os.makedirs(mgr.firmware_dir, exist_ok=True)
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)

        await self.hass.async_add_executor_job(_save)
        try:
            image = await self.hass.async_add_executor_job(load_firmware, path)
        except OtaError as err:
            return self.json_message(f"файл сохранён, но не годится: {err}", status_code=400)

        result = {
            "path": path,
            "size": len(image.data),
            "project": image.project,
            "version": image.version,
            "from_factory": image.from_factory,
            "started": False,
        }
        if request.query.get("start") in ("1", "true", "yes"):
            try:
                await mgr.async_start(path, int(request.query.get("timeout", 180)))
                result["started"] = True
            except OtaError as err:
                result["start_error"] = str(err)
        result["status"] = mgr.status
        return self.json(result)


class BuildView(HomeAssistantView):
    """Сборка + прошивка: POST /api/move_sensor_ble_ota/build[?configuration=x.yaml][&start=0][&timeout=180]
    [&entry_id=...]. Отвечает сразу (сборка идёт в фоне); ход — GET /api/move_sensor_ble_ota/status."""

    url = "/api/move_sensor_ble_ota/build"
    name = "api:move_sensor_ble_ota:build"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        user = request.get("hass_user")
        if user is None or not user.is_admin:
            return self.json_message("нужен администратор", status_code=403)
        q = request.query
        try:
            mgr = _pick(self.hass, q.get("entry_id"))
            cfg = os.path.basename(q.get("configuration") or "")   # пусто = YAML датчика по Device ID
            start = q.get("start", "1") not in ("0", "false", "no")
            timeout = int(q.get("timeout", DEFAULT_TIMEOUT_MIN))
            await async_flash_gate(mgr, start)
            start_build(self.hass, mgr, cfg, start, timeout)
        except (HomeAssistantError, ValueError) as err:
            return self.json_message(str(err), status_code=409)
        return self.json({"status": "building", "configuration": cfg, "start": start})


class StatusView(HomeAssistantView):
    """GET /api/move_sensor_ble_ota/status — состояние всех датчиков."""

    url = "/api/move_sensor_ble_ota/status"
    name = "api:move_sensor_ble_ota:status"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        return self.json(
            {
                eid: {
                    "title": m.name,
                    "status": m.status,
                    "progress": m.progress,
                    **m.attributes(),
                    "settings_status": m.settings_status,
                    "settings": m.settings_attributes(),
                }
                for eid, m in _managers(self.hass).items()
            }
        )
