"""v1.29.1: поиск устройств в реестре без устаревших вызовов.

HA 2026.11 пометил устаревшими `device_registry.async_get_device` (идентификаторы и подключения
больше не уникальны между записями интеграций; уберут в 2027.8), `DeviceEntry.config_entries`
(у устройства теперь одна запись — `config_entry_id`) и обращение к `device_registry.devices`
как к словарю. Новые функции есть не во всех версиях HA — на старых берём прежние.
"""
from __future__ import annotations

from homeassistant.helpers import device_registry as dr


def find_device(
    dev_reg: dr.DeviceRegistry,
    *,
    identifiers: set[tuple[str, str]] | None = None,
    connections: set[tuple[str, str]] | None = None,
) -> dr.DeviceEntry | None:
    """Первое устройство с таким идентификатором или подключением (в любой записи интеграции)."""
    get_devices = getattr(dev_reg, "async_get_devices", None)
    if get_devices is not None:
        found = get_devices(identifiers=identifiers, connections=connections)
        return found[0] if found else None
    return dev_reg.async_get_device(identifiers=identifiers, connections=connections)
