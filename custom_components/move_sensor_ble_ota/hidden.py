"""Список чужих сущностей, которые скрывает интеграция (формат зафиксирован в v1.14.2).

Скрывать начнёт v1.15 (дубли в ESPHome- и BTHome-устройстве датчика). v1.14.2 сама ничего не
скрывает — только «защита отката»: если после 1.15 вернулись на 1.14.x, показать обратно то,
что скрыла 1.15, и вернуть голосовым помощникам исходные сущности.

Хранилище: .storage/move_sensor_ble_ota.hidden_<entry_id>, версия 1:
    {"entities": {"<RegistryEntry.id (UUID)>": {"state": <STATE>, "expose": {<assistant>: bool} | None}}}
STATE:
    hidden_by_us       — скрыта интеграцией (hidden_by = INTEGRATION)
    shown_by_button    — показана кнопкой «Показать» в «Настроить»
    shown_by_rollback  — показана при откате на 1.14.x (этот модуль)
    shown_by_user      — пользователь показал сам (больше не скрываем)
expose — исходные настройки голосовых помощников, которые 1.15 сняла с этой сущности
(None — не меняли).
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
HIDDEN_BY_US = "hidden_by_us"
SHOWN_BY_BUTTON = "shown_by_button"
SHOWN_BY_ROLLBACK = "shown_by_rollback"
SHOWN_BY_USER = "shown_by_user"


def hidden_store(hass: HomeAssistant, entry_id: str) -> Store:
    return Store(hass, STORE_VERSION, f"{DOMAIN}.hidden_{entry_id}")


async def async_rollback_unhide(hass: HomeAssistant, entry: ConfigEntry) -> int:
    """Показать сущности, скрытые более новой версией. Возвращает, сколько показано."""
    store = hidden_store(hass, entry.entry_id)
    data = await store.async_load()
    if not data or not isinstance(data.get("entities"), dict):
        return 0
    reg = er.async_get(hass)
    shown = 0
    changed = False
    for reg_id, item in data["entities"].items():
        if not isinstance(item, dict) or item.get("state") != HIDDEN_BY_US:
            continue
        ent = reg.entities.get_entry(reg_id)
        if ent is None:
            continue  # сущности нет — пропускаем, запись оставляем (вернётся — 1.15 разберётся)
        if ent.hidden_by == er.RegistryEntryHider.INTEGRATION:
            reg.async_update_entity(ent.entity_id, hidden_by=None)
            shown += 1
        item["state"] = SHOWN_BY_ROLLBACK
        changed = True
        expose = item.get("expose")
        if isinstance(expose, dict) and expose:
            try:
                from homeassistant.components.homeassistant.exposed_entities import (  # noqa: PLC0415
                    async_expose_entity,
                )

                for assistant, should in expose.items():
                    async_expose_entity(hass, assistant, ent.entity_id, bool(should))
            except Exception:  # noqa: BLE001 — голосовые настройки не критичны
                _LOGGER.warning("%s: could not restore assistant settings", ent.entity_id, exc_info=True)
    if changed:
        await store.async_save(data)
    if shown:
        _LOGGER.warning(
            "%d entities hidden by a newer version of %s were shown again (rollback)", shown, DOMAIN
        )
    return shown
