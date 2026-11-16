"""Скрытие дублей (v1.15) — как это делают встроенные помощники HA (Switch as X, Группа).

Основное устройство «Move Sensor BLE» показывает данные и настройки датчика из любого канала.
Те же значения в ESPHome-устройстве (Wi-Fi) и в BTHome-устройстве — копии: интеграция их СКРЫВАЕТ
(hidden_by = integration), но не выключает. Скрытая сущность живёт полностью: состояние,
автоматизации, история, логбук. Правила:
  * чужую сущность, уже скрытую кем-то (пользователем или владельцем), не трогаем;
  * свой список — по внутренним ID реестра (UUID, переживает переименование entity_id);
  * показал пользователь сам — больше не скрываем;
  * кнопка «Показать скрытые» (в «Настроить») и «Снова скрыть»;
  * возврат — только при удалении интеграции (async_remove_entry), и только того, что скрыто нами;
  * при отключении/перезапуске/обновлении ничего не возвращаем — скрытое и так работает;
  * голосовые помощники: при первом скрытии копий данных их настройки переносятся на сущность
    основного устройства, исходная у помощников выключается; при удалении — возвращаются.
Метка «Move Sensor BLE» на скрытых нами сущностях — найти их фильтром одним кликом.

Хранилище: .storage/move_sensor_ble_ota.hidden_<entry_id>, версия 1:
    {"show": bool,
     "entities": {"<RegistryEntry.id>": {"state": <STATE>, "expose": {<assistant>: bool} | None}}}
STATE:
    hidden_by_us       — скрыта интеграцией (hidden_by = INTEGRATION)
    shown_by_button    — показана кнопкой «Показать скрытые»
    shown_by_rollback  — показана при откате на 1.14.x (v1.14.2)
    shown_by_user      — пользователь показал сам (больше не скрываем)
v1.14.2 читает этот же формат (защита отката).
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
HIDDEN_BY_US = "hidden_by_us"
SHOWN_BY_BUTTON = "shown_by_button"
SHOWN_BY_ROLLBACK = "shown_by_rollback"
SHOWN_BY_USER = "shown_by_user"
LABEL_NAME = "Move Sensor BLE"
ASSISTANTS = ("conversation", "cloud.alexa", "cloud.google_assistant")
# копии данных -> ключ сущности основного устройства (голосовые помощники переезжают на неё)
_BTHOME_DATA = {"occupancy": "occupancy", "temperature": "temperature", "humidity": "humidity",
                "battery": "battery", "voltage": "voltage"}
_ESPHOME_DATA = {"binary_sensor/Occupancy": "occupancy", "sensor/Temperature": "temperature",
                 "sensor/Humidity": "humidity", "sensor/Battery": "battery", "sensor/Voltage": "voltage"}


def hidden_store(hass: HomeAssistant, entry_id: str) -> Store:
    return Store(hass, STORE_VERSION, f"{DOMAIN}.hidden_{entry_id}")


class DuplicateHider:
    """Скрывает копии в ESPHome- и BTHome-устройстве датчика и ведёт свой список."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, mgr) -> None:
        self.hass = hass
        self.entry = entry
        self.mgr = mgr
        self.store = hidden_store(hass, entry.entry_id)
        self.data: dict = {"show": False, "entities": {}}
        self._unsub: list[Callable] = []
        self._pending: Callable | None = None

    # ---------------- жизненный цикл ----------------
    async def async_start(self) -> None:
        loaded = await self.store.async_load()
        if isinstance(loaded, dict):
            self.data = {"show": bool(loaded.get("show")), "entities": loaded.get("entities") or {}}
        self._unsub.append(self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._reg_event))
        self._unsub.append(self.hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, self._reg_event))
        # полный проход при каждом запуске: события create, пришедшие до нас, потеряны
        self.schedule(0)

    @callback
    def stop(self) -> None:
        for u in self._unsub:
            u()
        self._unsub = []
        if self._pending is not None:
            self._pending()
            self._pending = None

    @callback
    def _reg_event(self, event: Event) -> None:
        if event.data.get("action") in ("create", "update"):
            self.schedule(2)

    @callback
    def schedule(self, delay: float) -> None:
        if self._pending is not None:
            return

        @callback
        def _run(_now=None) -> None:
            self._pending = None
            self.hass.async_create_task(self.async_apply())

        self._pending = async_call_later(self.hass, delay, _run)

    @property
    def show(self) -> bool:
        return bool(self.data.get("show"))

    # ---------------- какие сущности — копии ----------------
    def _targets(self) -> list[tuple[er.RegistryEntry, str | None]]:
        """[(запись реестра, ключ данных для голосовых помощников или None)]."""
        from .wifi import ESPHOME_VISIBLE, uid_suffix  # noqa: PLC0415

        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        out: list[tuple[er.RegistryEntry, str | None]] = []
        # ESPHome-устройство датчика: всё, кроме Wi-Fi и сервисного режима
        wifi = getattr(self.mgr, "wifi", None)
        if wifi is not None and wifi.device_id:
            for ent in er.async_entries_for_device(ent_reg, wifi.device_id, include_disabled_entities=True):
                if ent.platform != "esphome":
                    continue
                suf = uid_suffix(ent.unique_id or "")
                if suf in ESPHOME_VISIBLE:
                    continue
                out.append((ent, _ESPHOME_DATA.get(suf)))
        # BTHome-устройство того же датчика (и шаблоны, привязанные к нему)
        bt = dev_reg.async_get_device(identifiers={("bluetooth", self.mgr.address)})
        if bt is not None:
            for ent in er.async_entries_for_device(ent_reg, bt.id, include_disabled_entities=True):
                if ent.platform == DOMAIN:
                    continue
                key = None
                if ent.platform == "bthome":
                    key = _BTHOME_DATA.get((ent.unique_id or "").rsplit("-", 1)[-1])
                    # «-battery» есть и у binary_sensor (Battery low): совпадать должен и домен
                    if key is not None and (ent.domain == "binary_sensor") != (key == "occupancy"):
                        key = None
                out.append((ent, key))
        return out

    def _our_entity_id(self, key: str) -> str | None:
        return er.async_get(self.hass).async_get_entity_id(
            "binary_sensor" if key == "occupancy" else "sensor", DOMAIN, f"{self.mgr.address}_{key}")

    def _label_id(self) -> str | None:
        try:
            from homeassistant.helpers import label_registry as lr  # noqa: PLC0415

            reg = lr.async_get(self.hass)
            label = reg.async_get_label_by_name(LABEL_NAME)
            if label is None:
                label = reg.async_create(LABEL_NAME, icon="mdi:motion-sensor")
            return label.label_id
        except Exception:  # noqa: BLE001 — метка — удобство, не критично
            _LOGGER.debug("label registry unavailable", exc_info=True)
            return None

    # ---------------- голосовые помощники ----------------
    def _move_expose(self, src_entity_id: str, key: str | None) -> dict | None:
        if key is None:
            return None
        try:
            from homeassistant.components.homeassistant.exposed_entities import (  # noqa: PLC0415
                async_expose_entity,
                async_should_expose,
            )
        except ImportError:
            return None
        snap: dict = {}
        ours = self._our_entity_id(key)
        # псевдонимы (для голосовых помощников) — тоже на основную сущность, как Switch as X
        try:
            reg = er.async_get(self.hass)
            src = reg.async_get(src_entity_id)
            if ours and src is not None and src.aliases:
                dst = reg.async_get(ours)
                if dst is not None and not dst.aliases:
                    reg.async_update_entity(ours, aliases=set(src.aliases))
        except Exception:  # noqa: BLE001
            _LOGGER.debug("aliases copy failed for %s", src_entity_id, exc_info=True)
        try:
            for a in ASSISTANTS:
                snap[a] = bool(async_should_expose(self.hass, a, src_entity_id))
            for a, val in snap.items():
                if ours and val:
                    async_expose_entity(self.hass, a, ours, True)
                if val:
                    async_expose_entity(self.hass, a, src_entity_id, False)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("expose move failed for %s", src_entity_id, exc_info=True)
        return snap

    def _restore_expose(self, entity_id: str, snap: dict | None) -> None:
        if not snap:
            return
        try:
            from homeassistant.components.homeassistant.exposed_entities import async_expose_entity  # noqa: PLC0415

            for a, val in snap.items():
                if val:
                    async_expose_entity(self.hass, a, entity_id, True)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("expose restore failed for %s", entity_id, exc_info=True)

    # ---------------- основное ----------------
    async def async_apply(self) -> None:
        ent_reg = er.async_get(self.hass)
        ents: dict = self.data["entities"]
        label = None
        changed = 0
        for ent, key in self._targets():
            item = ents.get(ent.id)
            if item is None:
                if ent.hidden_by is not None or ent.disabled_by is not None or self.show:
                    continue   # скрыта/выключена не нами — не наша; в режиме «Показать» новые не скрываем
                if label is None:
                    label = self._label_id()
                item = {"state": HIDDEN_BY_US, "expose": self._move_expose(ent.entity_id, key)}
                ents[ent.id] = item
                ent_reg.async_update_entity(
                    ent.entity_id, hidden_by=er.RegistryEntryHider.INTEGRATION,
                    labels=(set(ent.labels) | {label}) if label else ent.labels)
                changed += 1
                continue
            st = item.get("state")
            if st == HIDDEN_BY_US:
                if ent.hidden_by is None:
                    item["state"] = SHOWN_BY_USER          # пользователь показал сам
                    changed += 1
                elif self.show and ent.hidden_by == er.RegistryEntryHider.INTEGRATION:
                    ent_reg.async_update_entity(ent.entity_id, hidden_by=None)
                    item["state"] = SHOWN_BY_BUTTON
                    changed += 1
            elif st in (SHOWN_BY_BUTTON, SHOWN_BY_ROLLBACK) and not self.show and ent.hidden_by is None:
                ent_reg.async_update_entity(ent.entity_id, hidden_by=er.RegistryEntryHider.INTEGRATION)
                item["state"] = HIDDEN_BY_US
                changed += 1
        if changed:
            _LOGGER.info("%s: duplicates %s: %d entities", self.mgr.name, "shown" if self.show else "hidden", changed)
            await self.store.async_save(self.data)

    async def async_set_show(self, show: bool) -> None:
        self.data["show"] = bool(show)
        await self.store.async_save(self.data)
        await self.async_apply()

    def counts(self) -> dict:
        out: dict = {}
        for item in self.data["entities"].values():
            out[item.get("state")] = out.get(item.get("state"), 0) + 1
        return out


async def async_unhide_on_remove(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Удаление интеграции: показать то, что скрыли МЫ (флаг всё ещё INTEGRATION), вернуть помощников."""
    store = hidden_store(hass, entry.entry_id)
    data = await store.async_load()
    if isinstance(data, dict):
        ent_reg = er.async_get(hass)
        hider = DuplicateHider.__new__(DuplicateHider)
        hider.hass = hass
        for reg_id, item in (data.get("entities") or {}).items():
            ent = ent_reg.entities.get_entry(reg_id)
            if ent is None or not isinstance(item, dict):
                continue
            if item.get("state") == HIDDEN_BY_US and ent.hidden_by == er.RegistryEntryHider.INTEGRATION:
                ent_reg.async_update_entity(ent.entity_id, hidden_by=None)
            DuplicateHider._restore_expose(hider, ent.entity_id, item.get("expose"))
    await store.async_remove()


async def async_rollback_unhide(hass: HomeAssistant, entry: ConfigEntry) -> int:
    """(v1.14.2) Защита отката: показать скрытое более новой версией. В 1.15 не вызывается."""
    store = hidden_store(hass, entry.entry_id)
    data = await store.async_load()
    if not data or not isinstance(data.get("entities"), dict):
        return 0
    reg = er.async_get(hass)
    shown = 0
    for reg_id, item in data["entities"].items():
        if not isinstance(item, dict) or item.get("state") != HIDDEN_BY_US:
            continue
        ent = reg.entities.get_entry(reg_id)
        if ent is None:
            continue
        if ent.hidden_by == er.RegistryEntryHider.INTEGRATION:
            reg.async_update_entity(ent.entity_id, hidden_by=None)
            shown += 1
        item["state"] = SHOWN_BY_ROLLBACK
    await store.async_save(data)
    return shown
