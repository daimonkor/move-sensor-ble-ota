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

PANEL_URL = "move-sensor-ble"
PANEL_COMPONENT = "move-sensor-ble-panel"
STATIC_URL = f"/{DOMAIN}_static"
_STATIC_DONE = f"{DOMAIN}_panel_static"

# (платформа, ключ) — в порядке показа
SECTIONS = [
    ("Sensor", [("binary_sensor", "occupancy"), ("sensor", "temperature"), ("sensor", "humidity"),
                ("sensor", "battery"), ("sensor", "last_motion"),
                ("binary_sensor", "pir_stuck"), ("sensor", "battery_days_left")]),
    ("Settings", [("number", "setting_keep_time"), ("number", "setting_detect_delay"),
                  ("number", "setting_climate_interval"), ("switch", "setting_led"),
                  ("number", "setting_temp_offset"), ("number", "setting_hum_offset"),
                  ("number", "setting_voltage_factor"), ("number", "pir_stuck_threshold"),
                  ("sensor", "settings_status"), ("event", "settings_change"), ("button", "settings_read")]),
    ("BLE", [("sensor", "link"), ("sensor", "signal"), ("sensor", "signal_level"), ("sensor", "last_seen"),
             ("sensor", "tx_power"), ("sensor", "ota_key"), ("number", "setting_motion_window"),
             ("number", "setting_climate_window"), ("switch", "auto_window"), ("button", "identify"),
             ("button", "wifi_reset")]),
    ("Firmware", [("update", "firmware"), ("sensor", "device_id"), ("sensor", "voltage"),
                  ("sensor", "last_reset_reason"), ("sensor", "brownout_count"), ("sensor", "crash_count")]),
]


def _store(hass: HomeAssistant) -> Store:
    return Store(hass, 1, f"{DOMAIN}.panel")


async def async_panel_shown(hass: HomeAssistant) -> bool:
    data = await _store(hass).async_load()
    return True if not isinstance(data, dict) else bool(data.get("show", True))


async def async_set_panel_shown(hass: HomeAssistant, show: bool) -> None:
    await _store(hass).async_save({"show": bool(show)})
    await async_update_panel(hass)


def _config(hass: HomeAssistant) -> dict:
    from .wifi import ESPHOME_VISIBLE  # noqa: PLC0415

    reg = er.async_get(hass)
    devices = []
    for entry_id, mgr in (hass.data.get(DOMAIN) or {}).items():
        sections = []
        for title, items in SECTIONS:
            ids = []
            for platform, key in items:
                eid = reg.async_get_entity_id(platform, DOMAIN, f"{mgr.address}_{key}")
                if eid:
                    ids.append(eid)
            if title == "BLE":
                # «Настроить» — ключ OTA и папка прошивок, «Вид» (страница интеграции)
                ids.insert(6 if len(ids) > 6 else len(ids), {
                    "type": "button", "name": "Configure (OTA key, display)", "icon": "mdi:cog",
                    "action_name": "Open", "tap_action": {"action": "navigate",
                                                          "navigation_path": f"/config/integrations/integration/{DOMAIN}"}})
            if title == "Firmware":
                ids.append({"type": "button", "name": "Cancel firmware update", "icon": "mdi:cancel",
                            "action_name": "Cancel",
                            "tap_action": {"action": "perform-action",
                                           "perform_action": f"{DOMAIN}.cancel",
                                           "data": {"config_entry_id": entry_id}}})
            sections.append({"title": title, "entities": ids})
        wifi_ids = []
        wifi = getattr(mgr, "wifi", None)
        if wifi is not None and wifi.device_id:
            for reg_id, suf in wifi.all_entities.items():
                ent = reg.entities.get_entry(reg_id)
                if ent is not None and suf in ESPHOME_VISIBLE and not ent.disabled_by:
                    wifi_ids.append(ent.entity_id)
        # Wi-Fi — только когда связь по Wi-Fi (сервисный режим); в режиме BLE вместо карточки — подсказка
        link = reg.async_get_entity_id("sensor", DOMAIN, f"{mgr.address}_link")
        sections.insert(3, {"title": "Wi-Fi (service mode)", "entities": wifi_ids,
                            "show_if": {"entity": link, "state": "wifi"} if link and wifi_ids else None,
                            "hidden_note": "Wi-Fi is off — the sensor works over BLE. Turn on the WiFi switch "
                                           "on the sensor (service mode) to see Wi-Fi settings here.",
                            "note": None if wifi_ids else "No ESPHome device for this sensor."})
        # имена строк — без имени устройства («Move Sensor BLE Keep time» -> «Keep time»)
        from homeassistant.helpers import device_registry as dr  # noqa: PLC0415

        strip = {mgr.name}
        if wifi is not None and wifi.device_id:
            dev = dr.async_get(hass).async_get(wifi.device_id)
            if dev is not None:
                strip |= {n for n in (dev.name_by_user, dev.name) if n}
        devices.append({"title": mgr.name, "device_id": mgr.device_id, "sections": sections,
                        "strip": sorted(strip, key=len, reverse=True)})
    return {"devices": devices}


async def async_update_panel(hass: HomeAssistant) -> None:
    """Зарегистрировать/обновить/убрать панель (по галочке и наличию датчиков)."""
    frontend.async_remove_panel(hass, PANEL_URL, warn_if_unknown=False)
    if not hass.data.get(DOMAIN) or not await async_panel_shown(hass):
        return
    if not hass.data.get(_STATIC_DONE):
        from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415

        path = os.path.join(os.path.dirname(__file__), "frontend")
        await hass.http.async_register_static_paths([StaticPathConfig(STATIC_URL, path, False)])
        hass.data[_STATIC_DONE] = True
    from homeassistant.components import panel_custom  # noqa: PLC0415

    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL,
        webcomponent_name=PANEL_COMPONENT,
        sidebar_title="Move Sensor BLE",
        sidebar_icon="mdi:motion-sensor",
        module_url=f"{STATIC_URL}/panel.js?v=1.15.0",
        config=_config(hass),
        require_admin=False,
    )


def async_remove_panel_if_last(hass: HomeAssistant) -> None:
    if not hass.data.get(DOMAIN):
        frontend.async_remove_panel(hass, PANEL_URL, warn_if_unknown=False)
