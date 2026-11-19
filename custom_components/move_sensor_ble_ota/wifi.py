"""Канал Wi-Fi (v1.15): ESPHome-устройство того же датчика как второй канал данных и настроек.

Своё ESPHome-устройство ищем только среди записей ESPHome: MAC Wi-Fi = MAC BLE − 2 (ESP32-C3;
вычитаем в последнем байте без переноса — так считает ESP-IDF). Тот же MAC бывает у устройства
роутера (keenetic и т. п.) — поэтому фильтр по записи ESPHome обязателен.

«На связи» = запись ESPHome загружена и её API подключён (runtime_data.available). Доступность
отдельных сущностей ESPHome для этого не годится: у датчика с deep_sleep после ожидаемого
отключения они остаются «доступными» со старыми значениями.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

_LOGGER = logging.getLogger(__name__)

# сущности ESPHome-устройства датчика по «домен/имя» из unique_id (MAC/0/<домен>/<имя>)
DATA_MAP = {
    "binary_sensor/Occupancy": "occupancy",
    "sensor/Temperature": "temperature",
    "sensor/Humidity": "humidity",
    "sensor/Battery": "battery",
    "sensor/Voltage": "voltage",
    "sensor/WiFi RSSI": "wifi_rssi",     # прошивка до 1.6.1
    "sensor/Wi-Fi RSSI": "wifi_rssi",    # прошивка 1.6.1+: имена по правилам HA
    "sensor/Settings revision": "settings_rev",
    # 2026-11-19: версия прошивки по Wi-Fi («1.7.0 t<отпечаток> v…») — в сервисном режиме интеграция
    # не подключается по BLE и иначе не знает, что датчик прошили (фальшивое «new build»)
    "text_sensor/Build Version": "build_version",
}
TEXT_KEYS = {"build_version"}
SETTING_MAP = {
    "number/Detect delay": "detect_delay",
    "number/Keep time": "keep_time",
    "number/Climate report interval": "climate_interval",
    "number/Voltage factor": "voltage_factor",
    "number/Motion window": "motion_window",
    "number/Climate window": "climate_window",
    "number/Temperature offset": "temp_offset",
    "number/Humidity offset": "hum_offset",
    "number/Battery capacity": "battery_capacity",     # v1.19, прошивка 1.6.0
    "number/Battery low level": "battery_low",
    "switch/Motion tracking LED": "led",
}
# остаются на виду в ESPHome-устройстве (Wi-Fi и сервисный режим); остальное — копии, скрываются
ESPHOME_VISIBLE = {
    "text_sensor/Wi-Fi SSID", "text_sensor/AP password", "text_sensor/AP password status",
    "button/Reconnect to router", "button/Scan Wi-Fi networks", "button/Save AP password",
    "button/Reset Wi-Fi to firmware defaults",
    "text_info/New Wi-Fi SSID", "text/New Wi-Fi SSID",
    # прошивка до 1.6.1
    "text_sensor/WiFi IP", "sensor/WiFi Signal", "text_sensor/Wi-Fi Scan Results",
    "text_info/New Wi-Fi Password", "text_info/New AP Password", "text/New Wi-Fi Password", "text/New AP Password",
    # прошивка 1.6.1+: те же сущности, имена по правилам HA (Wi-Fi, заглавная только в начале)
    "text_sensor/Wi-Fi IP", "sensor/Wi-Fi signal", "text_sensor/Wi-Fi scan results",
    "text_info/New Wi-Fi password", "text_info/New AP password", "text/New Wi-Fi password", "text/New AP password",
}


def wifi_mac(ble_address: str) -> str:
    """BLE E8:F6:0A:14:8F:9A -> Wi-Fi e8:f6:0a:14:8f:98 (последний байт − 2, без переноса)."""
    parts = ble_address.lower().replace("-", ":").split(":")
    if len(parts) != 6:
        return ""
    parts[5] = f"{(int(parts[5], 16) - 2) & 0xFF:02x}"
    return ":".join(parts)


def uid_suffix(unique_id: str) -> str:
    """'E8:F6:0A:14:8F:98/0/sensor/Voltage' -> 'sensor/Voltage'."""
    parts = unique_id.split("/")
    return "/".join(parts[2:]) if len(parts) >= 4 else ""


class WifiChannel:
    """Следит за ESPHome-устройством датчика и отдаёт менеджеру состояния / пишет настройки."""

    def __init__(self, hass: HomeAssistant, ble_address: str, on_state: Callable[[str, object], None],
                 on_link: Callable[[bool], None]) -> None:
        self.hass = hass
        self.mac = wifi_mac(ble_address)
        self._on_state = on_state
        self._on_link = on_link
        self.device_id: str | None = None
        self.entry_id: str | None = None
        self.entities: dict[str, str] = {}       # ключ -> entity_id (данные и настройки)
        self.all_entities: dict[str, str] = {}   # registry id -> suffix (все сущности устройства)
        self._unsub_states: Callable | None = None
        self._unsubs: list[Callable] = []
        self._live = False

    # ---------------- поиск устройства ----------------
    @callback
    def resolve(self) -> None:
        dev_reg = dr.async_get(self.hass)
        ent_reg = er.async_get(self.hass)
        esp_entries = {e.entry_id for e in self.hass.config_entries.async_entries("esphome")}
        found = None
        for dev in dev_reg.devices.values():
            if not esp_entries.intersection(dev.config_entries):
                continue
            if any(c[0] == dr.CONNECTION_NETWORK_MAC and c[1].lower() == self.mac for c in dev.connections):
                found = dev
                break
        old = (self.device_id, tuple(sorted(self.entities.items())))
        if found is None:
            self.device_id = None
            self.entry_id = None
            self.entities = {}
            self.all_entities = {}
        else:
            self.device_id = found.id
            self.entry_id = next(iter(esp_entries.intersection(found.config_entries)))
            self.entities = {}
            self.all_entities = {}
            for ent in er.async_entries_for_device(ent_reg, found.id, include_disabled_entities=True):
                if ent.platform != "esphome":
                    continue
                suf = uid_suffix(ent.unique_id or "")
                self.all_entities[ent.id] = suf
                key = DATA_MAP.get(suf) or SETTING_MAP.get(suf)
                if key:
                    self.entities[key] = ent.entity_id
                if key == "build_version" and ent.disabled_by is not None:
                    # нужна интеграции (версия по Wi-Fi); остаётся скрытой в списке устройства
                    ent_reg.async_update_entity(ent.entity_id, disabled_by=None)
                    _LOGGER.info("%s enabled: the firmware version is read over Wi-Fi", ent.entity_id)
        if old != (self.device_id, tuple(sorted(self.entities.items()))):
            _LOGGER.debug("ESPHome device for %s: %s, %d mapped entities", self.mac, self.device_id, len(self.entities))
            self._subscribe()

    def _subscribe(self) -> None:
        if self._unsub_states is not None:
            self._unsub_states()
            self._unsub_states = None
        ids = list(self.entities.values())
        if ids:
            self._unsub_states = async_track_state_change_event(self.hass, ids, self._state_event)

    # ---------------- жизненный цикл ----------------
    @callback
    def start(self) -> None:
        self.resolve()
        self._unsubs.append(self.hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, self._reg_event))
        self._unsubs.append(self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._reg_event))
        self._live = self.live

    @callback
    def stop(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs = []
        if self._unsub_states is not None:
            self._unsub_states()
            self._unsub_states = None

    @callback
    def _reg_event(self, _event: Event) -> None:
        self.resolve()

    # ---------------- связь ----------------
    @property
    def live(self) -> bool:
        if self.entry_id is None:
            return False
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if entry is None or entry.state is not ConfigEntryState.LOADED:
            return False
        data = getattr(entry, "runtime_data", None)
        return bool(getattr(data, "available", False))

    @property
    def host(self) -> str | None:
        """Адрес датчика в сети (из записи ESPHome) — для прошивки по Wi-Fi (порт 3232)."""
        if self.entry_id is None:
            return None
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        return None if entry is None else entry.data.get("host")

    @callback
    def check_link(self) -> bool:
        """Вызывается периодически и на событиях: сообщить менеджеру о смене связи."""
        now = self.live
        if now != self._live:
            self._live = now
            self._on_link(now)
        return now

    def key_of(self, entity_id: str) -> str | None:
        for k, eid in self.entities.items():
            if eid == entity_id:
                return k
        return None

    @callback
    def _state_event(self, event: Event) -> None:
        self.check_link()
        if not self._live:
            return
        new = event.data.get("new_state")
        old = event.data.get("old_state")
        if new is None or new.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        key = self.key_of(event.data["entity_id"])
        if key is None:
            return
        # переход из unavailable/unknown (подключение API) — первое значение, не правка
        first = old is None or old.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)
        self._on_state(key, self.parse(key, new.state), first)

    @staticmethod
    def parse(key: str, state: str):
        if key in TEXT_KEYS:
            return state
        if key in ("occupancy", "led"):
            return state == "on"
        try:
            return float(state)
        except (TypeError, ValueError):
            return None

    def current(self, key: str):
        eid = self.entities.get(key)
        if eid is None:
            return None
        st = self.hass.states.get(eid)
        if st is None or st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        return self.parse(key, st.state)

    def has(self, key: str) -> bool:
        return key in self.entities

    # ---------------- запись настройки ----------------
    async def async_write(self, key: str, value) -> None:
        eid = self.entities[key]
        if key == "led":
            await self.hass.services.async_call("switch", "turn_on" if value else "turn_off",
                                                {"entity_id": eid}, blocking=True)
        else:
            await self.hass.services.async_call("number", "set_value", {"entity_id": eid, "value": value},
                                                blocking=True)
