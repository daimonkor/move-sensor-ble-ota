"""Панель «Move Sensor BLE» в боковом меню (v1.15) — от интеграции, не дашборд пользователя.

Своя страница (panel_custom) из стандартных карточек HA. Набор сущностей собирается из реестра
автоматически: появилась новая сущность интеграции — она на панели, ничего править руками не нужно.
Показ в меню — галочка в «Настроить» (хранится в .storage/move_sensor_ble_ota.panel, не в опциях:
смена опций перезагрузила бы интеграцию и оборвала бы прошивку).
"""
from __future__ import annotations

import logging
import os

from homeassistant.components import frontend
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# своё имя: «move-sensor-ble» может быть у дашборда пользователя (Lovelace) — его не трогаем
PANEL_URL = "move-sensor-ble-panel"
PANEL_COMPONENT = "move-sensor-ble-panel"
STATIC_URL = f"/{DOMAIN}_static"
_STATIC_DONE = f"{DOMAIN}_panel_static"


def _remove_own(hass: HomeAssistant) -> None:
    """Убрать только свою панель (custom), чужую с тем же адресом не трогать."""
    panels = hass.data.get(getattr(frontend, "DATA_PANELS", "frontend_panels"), {})
    panel = panels.get(PANEL_URL)
    if panel is not None and getattr(panel, "component_name", None) == "custom":
        frontend.async_remove_panel(hass, PANEL_URL, warn_if_unknown=False)


# v1.20: панель — табы Overview / Settings / Advanced на встроенных карточках HA (tile + features).
# Раскладку собирает frontend/panel.js; отсюда — сущности по ключам (unique_id = <адрес>_<ключ>).
# Новая сущность интеграции без места в раскладке попадает в Advanced › Diagnostics (не теряется).
PLATFORMS_ORDER = ("binary_sensor", "sensor", "number", "switch", "button", "event", "update")


_STALE_DONE = f"{DOMAIN}_panel_store_removed"


def _store(hass: HomeAssistant) -> Store:
    return Store(hass, 1, f"{DOMAIN}.panel")


def _firmware_rows(fw: str | None, entry_id: str) -> list:
    """Строки раздела Firmware: само обновление, этап, кнопки (с подтверждением на необратимом)."""
    if not fw:
        return []
    return [
        fw,
        # v1.18.1: этап сборки/прошивки на всех шагах (у самой «Firmware» процент — только при прошивке)
        {"type": "attribute", "entity": fw, "attribute": "ota_status", "name": "OTA status", "icon": "mdi:progress-upload"},
        {"type": "button", "name": "Install firmware (with backup)", "icon": "mdi:download", "action_name": "Install",
         "tap_action": {"action": "perform-action", "perform_action": "update.install",
                        "target": {"entity_id": fw}, "data": {"backup": True},
                        "confirmation": {"text": "Build and flash the sensor firmware? The current firmware is backed up first."}}},
        {"type": "button", "name": "Build only (check)", "icon": "mdi:hammer-wrench", "action_name": "Build",
         "tap_action": {"action": "perform-action", "perform_action": f"{DOMAIN}.build_and_flash",
                        "data": {"config_entry_id": entry_id, "start": False}}},
        {"type": "attribute", "entity": fw, "attribute": "backup", "name": "Backup", "icon": "mdi:backup-restore"},
        {"type": "button", "name": "Roll back to backup", "icon": "mdi:undo-variant", "action_name": "Roll back",
         "tap_action": {"action": "perform-action", "perform_action": f"{DOMAIN}.rollback",
                        "data": {"config_entry_id": entry_id},
                        "confirmation": {"text": "Roll back to the backup firmware? The sensor is flashed again on its next wake."}}},
        {"type": "attribute", "entity": fw, "attribute": "last_flash", "name": "Last flash", "icon": "mdi:history"},
        {"type": "attribute", "entity": fw, "attribute": "stored_images", "name": "Stored images", "icon": "mdi:folder-zip-outline"},
        {"type": "button", "name": "Cancel firmware update", "icon": "mdi:cancel", "action_name": "Cancel",
         "tap_action": {"action": "perform-action", "perform_action": f"{DOMAIN}.cancel",
                        "data": {"config_entry_id": entry_id}}},
    ]


def _config(hass: HomeAssistant) -> dict:
    from homeassistant.helpers import device_registry as dr  # noqa: PLC0415

    from .wifi import ESPHOME_VISIBLE  # noqa: PLC0415

    reg = er.async_get(hass)
    devices = []
    for entry_id, mgr in (hass.data.get(DOMAIN) or {}).items():
        prefix = f"{mgr.address}_"
        ids: dict[str, str] = {}
        for ent in er.async_entries_for_config_entry(reg, entry_id):
            if ent.platform != DOMAIN or ent.disabled_by or not (ent.unique_id or "").startswith(prefix):
                continue
            ids[ent.unique_id[len(prefix):]] = ent.entity_id
        wifi_ids = []
        wifi = getattr(mgr, "wifi", None)
        if wifi is not None and wifi.device_id:
            for reg_id, suf in wifi.all_entities.items():
                ent = reg.entities.get_entry(reg_id)
                if ent is not None and suf in ESPHOME_VISIBLE and not ent.disabled_by:
                    wifi_ids.append(ent.entity_id)
        # имена строк — без имени устройства («Move Sensor BLE Keep time» -> «Keep time»)
        strip = {mgr.name}
        if wifi is not None and wifi.device_id:
            dev = dr.async_get(hass).async_get(wifi.device_id)
            if dev is not None:
                strip |= {n for n in (dev.name_by_user, dev.name) if n}
        devices.append({
            "title": mgr.name, "device_id": mgr.device_id, "entry_id": entry_id, "ids": ids,
            "silent_after": str(mgr.entry.options.get("silent_after", "auto")),
            "firmware": _firmware_rows(ids.get("firmware"), entry_id),
            "wifi": wifi_ids, "configure": f"/config/integrations/integration/{DOMAIN}",
            "strip": sorted(strip, key=len, reverse=True),
        })
    return {"devices": devices}


async def async_update_panel(hass: HomeAssistant) -> None:
    """Зарегистрировать/обновить/убрать панель (по галочке и наличию датчиков)."""
    _remove_own(hass)
    if not hass.data.get(DOMAIN):   # v1.17.1: панель всегда (убрать из меню — «Изменить боковую панель» HA)
        return
    if not hass.data.get(_STALE_DONE):   # настройка «показывать панель» из убранного пункта «Вид» больше не нужна
        hass.data[_STALE_DONE] = True
        await _store(hass).async_remove()
    if not hass.data.get(_STATIC_DONE):
        from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415

        path = os.path.join(os.path.dirname(__file__), "frontend")
        await hass.http.async_register_static_paths([StaticPathConfig(STATIC_URL, path, False)])
        hass.data[_STATIC_DONE] = True
    from homeassistant.components import panel_custom  # noqa: PLC0415

    if PANEL_URL in hass.data.get(getattr(frontend, "DATA_PANELS", "frontend_panels"), {}):
        _LOGGER.warning("Panel /%s is taken by another panel — Move Sensor BLE panel not added", PANEL_URL)
        return
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL,
        webcomponent_name=PANEL_COMPONENT,
        sidebar_title="Move Sensor BLE",
        sidebar_icon="mdi:motion-sensor",
        module_url=f"{STATIC_URL}/panel.js?v=1.21.5",
        config=_config(hass),
        require_admin=False,
    )


def async_remove_panel_if_last(hass: HomeAssistant) -> None:
    if not hass.data.get(DOMAIN):
        _remove_own(hass)
