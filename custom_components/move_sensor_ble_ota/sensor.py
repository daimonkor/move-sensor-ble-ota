"""Сенсоры: статус OTA (с атрибутами) и прогресс, %."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    SST_APPLIED,
    SST_ERROR,
    SST_IDLE,
    SST_PENDING,
    SST_UNKNOWN,
    ST_CANCELLED,
    ST_CONNECTING,
    ST_BUILDING,
    ST_ERROR,
    ST_FLASHING,
    ST_IDLE,
    ST_SUCCESS,
    ST_WAITING,
)
from .entity import OtaEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    mgr = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        SettingsStatusSensor(mgr),   # v1.15: OTA status / progress / Firmware version -> сущность «Firmware»
        BrownoutCountSensor(mgr), CrashCountSensor(mgr), LastResetReasonSensor(mgr), LastSeenSensor(mgr),
        BatteryDaysLeftSensor(mgr), BatteryRemainingSensor(mgr), MotionEventsTodaySensor(mgr), WakesTodaySensor(mgr), DeviceIdSensor(mgr),
        LastMotionSensor(mgr),
        DataSensor(mgr, "temperature", "Temperature", SensorDeviceClass.TEMPERATURE, "°C", 1),
        DataSensor(mgr, "humidity", "Humidity", SensorDeviceClass.HUMIDITY, PERCENTAGE, 1),
        DataSensor(mgr, "battery", "Battery", SensorDeviceClass.BATTERY, PERCENTAGE, 0),
        DataSensor(mgr, "voltage", "Voltage", SensorDeviceClass.VOLTAGE, "V", 2, EntityCategory.DIAGNOSTIC),
        LinkSensor(mgr), SignalSensor(mgr), SignalPercentSensor(mgr), TxPowerSensor(mgr), OtaKeySensor(mgr),
    ])


class DeviceIdSensor(OtaEntity, SensorEntity):
    """Device ID — хвост MAC Wi-Fi (148F98): тот же, что в имени прошивки (move-sensor-ble-148f98),
    SSID точки доступа (MoveSensorBLE-148F98) и на сайте датчика. Различать несколько датчиков (2026-11-15)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:identifier"

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "device_id", "Device ID")

    @property
    def native_value(self) -> str:
        return self.mgr.device_id

    @property
    def extra_state_attributes(self) -> dict:
        return {"ble_address": self.mgr.address, "hostname": f"move-sensor-ble-{self.mgr.device_id.lower()}",
                "ap_ssid": f"MoveSensorBLE-{self.mgr.device_id}"}


class OtaStatusSensor(OtaEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC  # сервисное — в «Диагностику»
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [ST_IDLE, ST_BUILDING, ST_WAITING, ST_CONNECTING, ST_FLASHING, ST_SUCCESS, ST_ERROR, ST_CANCELLED]
    _attr_icon = "mdi:bluetooth-transfer"

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "ota_status", "OTA status")

    @property
    def native_value(self) -> str:
        return self.mgr.status

    @property
    def extra_state_attributes(self) -> dict:
        return self.mgr.attributes()


class OtaProgressSensor(OtaEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC  # сервисное — в «Диагностику»
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:progress-upload"

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "ota_progress", "OTA progress")

    @property
    def native_value(self) -> int:
        return self.mgr.progress


class SettingsStatusSensor(OtaEntity, SensorEntity):
    """Настройки (v1.15, только устойчивые состояния): in_sync / waiting / not_read / error.
    Что произошло (записано по Wi-Fi/BLE, принято с датчика, возвращено) — сущность-событие «Settings change»."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [SST_IDLE, SST_PENDING, SST_UNKNOWN, SST_ERROR]
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:cog-sync"

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "settings_status", "Settings status")

    @property
    def native_value(self) -> str:
        return self.mgr.settings_status

    @property
    def extra_state_attributes(self) -> dict:
        return self.mgr.settings_attributes()


class FirmwareVersionSensor(OtaEntity, SensorEntity):
    """Версия сборки на датчике (как «Build Version»), прочитанная по BLE при последнем подключении."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:wrench"

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "firmware_version", "Firmware version (BLE)")

    @property
    def native_value(self) -> str | None:
        return self.mgr.firmware_version

    @property
    def extra_state_attributes(self) -> dict:
        return {"read_at": self.mgr.firmware_seen.isoformat() if self.mgr.firmware_seen else None}


# ---- диагностика сбросов (DIAG, 2026-11-14): читается по BLE при перезагрузке датчика
def _diag_attrs(mgr) -> dict:
    d = dict(mgr.diag or {})
    d.pop("last_reason", None)
    return {
        **d,
        "read_at": mgr.diag_seen.isoformat() if mgr.diag_seen else None,
        "firmware_supports_diag": not mgr.diag_unsupported,
    }


class BrownoutCountSensor(OtaEntity, SensorEntity):
    """Сколько раз датчик перезагрузился по просадке питания (конец разряда / просадка под радио)."""
    _attr_entity_registry_enabled_default = False   # v1.15: техническая, выключена по умолчанию

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:flash-alert"

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "brownout_count", "Brownout count")

    @property
    def native_value(self) -> int | None:
        return None if self.mgr.diag is None else self.mgr.diag.get("brownout", 0)

    @property
    def extra_state_attributes(self) -> dict:
        return _diag_attrs(self.mgr)


class CrashCountSensor(OtaEntity, SensorEntity):
    """Сбои прошивки: panic + watchdog."""
    _attr_entity_registry_enabled_default = False   # v1.15: техническая, выключена по умолчанию

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:alert-octagon-outline"

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "crash_count", "Crash count")

    @property
    def native_value(self) -> int | None:
        return self.mgr.crash_count

    @property
    def extra_state_attributes(self) -> dict:
        return _diag_attrs(self.mgr)


class LastResetReasonSensor(OtaEntity, SensorEntity):
    """Причина последней загрузки «не из сна» (power on, brownout, panic, watchdog, software...)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:restart-alert"

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "last_reset_reason", "Last reset reason")

    @property
    def native_value(self) -> str | None:
        return self.mgr.last_reset_reason

    @property
    def extra_state_attributes(self) -> dict:
        return _diag_attrs(self.mgr)


class LastSeenSensor(OtaEntity, SensorEntity):
    """Когда пришёл последний пакет BTHome (любое пробуждение: движение или климат).
    Норма — не реже Climate report interval; дольше двух интервалов — разряд, сбой или нет связи."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC   # v1.20
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-check-outline"

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "last_seen", "Last seen")

    @property
    def native_value(self):
        return self.mgr.last_seen

    @property
    def extra_state_attributes(self) -> dict:
        ci = (self.mgr.applied or {}).get("climate_interval")
        return {**self.mgr.last_seen_info, "expected_interval_min": ci}


class DataSensor(OtaEntity, SensorEntity):
    """v1.15: данные датчика из любого канала (BLE или Wi-Fi) — самое свежее значение."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, mgr, key: str, name: str, device_class, unit, precision: int,
                 category: EntityCategory | None = None) -> None:
        super().__init__(mgr, key, name)
        self.key = key
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_suggested_display_precision = precision
        self._attr_entity_category = category

    @property
    def native_value(self):
        return self.mgr.data_value(self.key)

    @property
    def extra_state_attributes(self) -> dict:
        return self.mgr.data_info(self.key)


class LinkSensor(OtaEntity, SensorEntity):
    """v1.15: по какому каналу сейчас связь: wifi (ESPHome API) или ble."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["ble", "wifi"]
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:transit-connection-variant"

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "link", "Link")

    @property
    def native_value(self):
        return self.mgr.link

    @property
    def extra_state_attributes(self) -> dict:
        w = self.mgr.wifi
        return {"esphome_device": bool(w and w.device_id), "wifi_mac": w.mac if w else None}


class SignalSensor(OtaEntity, SensorEntity):
    """v1.15: уровень сигнала текущего канала (RSSI Wi-Fi или BLE у прокси), дБм."""

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = "dBm"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False   # v1.20: сигнал в dBm — шумный, по умолчанию выключен (есть Signal level, %)

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "signal", "Signal")

    @property
    def native_value(self):
        return self.mgr.signal

    @property
    def extra_state_attributes(self) -> dict:
        return {"channel": self.mgr.link, "ble_rssi": self.mgr.ble_rssi,
                "ble_proxy": (self.mgr.last_seen_info or {}).get("source")}


class SignalPercentSensor(OtaEntity, SensorEntity):
    """v1.15: сигнал текущего канала в %: −90 дБм = 0 %, −30 дБм = 100 % (как WiFi Signal / BLE Signal в прошивке)."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:signal"

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "signal_level", "Signal level")

    @property
    def native_value(self):
        return self.mgr.signal_percent

    @property
    def extra_state_attributes(self) -> dict:
        return {"channel": self.mgr.link, "dbm": self.mgr.signal}


class TxPowerSensor(OtaEntity, SensorEntity):
    """v1.15: мощность BLE-рекламы датчика, дБм — из YAML прошивки (bthome: tx_power), только чтение."""

    _attr_native_unit_of_measurement = "dBm"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False   # v1.20: редко нужен — выключен по умолчанию
    _attr_icon = "mdi:antenna"

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "tx_power", "BLE TX power")

    @property
    def native_value(self):
        return self.mgr.tx_power_dbm

    @property
    def extra_state_attributes(self) -> dict:
        return {"source": "firmware YAML (move_sensor_ble/base.yaml, bthome: tx_power)"}


class OtaKeySensor(OtaEntity, SensorEntity):
    """v1.15: ключ OTA — принят ли датчиком (accepted / rejected / missing / unknown). Поменять — «Настроить»."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["accepted", "rejected", "missing", "unknown"]
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:key-variant"

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "ota_key", "OTA key")

    @property
    def native_value(self):
        return self.mgr.key_state

    @property
    def extra_state_attributes(self) -> dict:
        return {"last_accepted": self.mgr.key_ok_at.isoformat() if self.mgr.key_ok_at else None,
                "blocked_since": (self.mgr.auth_block or {}).get("since")}


class LastMotionSensor(OtaEntity, SensorEntity):
    """Последнее свежее срабатывание PIR (v1.14.2). Пока «занято», датчик шлёт пакет на новое
    движение не чаще раза в 2 мин — по этому времени видно, что человек шевелится, а не PIR залип.
    Старая прошивка (без своего блока): время последнего перехода в «занято»."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:motion-sensor"

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "last_motion", "Last motion")

    @property
    def native_value(self):
        return self.mgr.last_motion

    @property
    def extra_state_attributes(self) -> dict:
        return {"motion_count": self.mgr.motion_count, "fresh_motion_supported": self.mgr.motion_count is not None}


class BatteryDaysLeftSensor(OtaEntity, SensorEntity):
    """Прогноз: сколько суток до разряда по тренду Battery % с последней зарядки."""

    _attr_native_unit_of_measurement = "d"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_icon = "mdi:battery-clock-outline"
    _attr_suggested_display_precision = 0

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "battery_days_left", "Battery days left")

    @property
    def native_value(self):
        return self.mgr.battery_forecast().get("days_left")

    @property
    def extra_state_attributes(self) -> dict:
        f = self.mgr.battery_forecast()
        f.pop("days_left", None)
        return f


class BatteryRemainingSensor(OtaEntity, SensorEntity):
    """v1.19: остаток в мА·ч = ёмкость (настройка датчика) × процент из пакета BTHome —
    то же, что считает li_battery на датчике; по BLE не передаётся, пакет рекламы не меняется."""

    _attr_native_unit_of_measurement = "mAh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:battery-heart-variant"
    _attr_suggested_display_precision = 0

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "battery_remaining", "Battery remaining")

    @property
    def available(self) -> bool:
        return self.mgr.battery_supported and super().available

    @property
    def native_value(self):
        return self.mgr.battery_remaining_mah()

    @property
    def extra_state_attributes(self) -> dict:
        return {"capacity_mah": self.mgr.applied.get("battery_capacity"),
                "battery_percent": self.mgr.data_value("battery")}


class MotionEventsTodaySensor(OtaEntity, SensorEntity):
    """Сколько раз за сегодня было «занято» (переходы в ON)."""
    _attr_entity_registry_enabled_default = False   # v1.15: техническая, выключена по умолчанию

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:motion-sensor"

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "motion_events_today", "Motion events today")

    @property
    def native_value(self) -> int:
        return self.mgr.stats.get("motion_events", 0)

    @property
    def extra_state_attributes(self) -> dict:
        y = self.mgr.stats.get("yesterday") or {}
        return {"date": self.mgr.stats.get("date"), "yesterday": y.get("motion_events")}


class WakesTodaySensor(OtaEntity, SensorEntity):
    _attr_entity_registry_enabled_default = False   # v1.15: техническая, выключена по умолчанию
    _attr_entity_category = EntityCategory.DIAGNOSTIC  # сервисное — в «Диагностику»
    """Пробуждения за сегодня: всего (по Wake count), с отправкой пакета и тихие (без радио)."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:sleep-off"

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "wakes_today", "Wakes today")

    @property
    def native_value(self) -> int:
        return self.mgr.stats.get("wakes", 0)

    @property
    def extra_state_attributes(self) -> dict:
        y = self.mgr.stats.get("yesterday") or {}
        return {
            "date": self.mgr.stats.get("date"),
            "with_packet": self.mgr.stats.get("tx_wakes", 0),
            "silent": self.mgr.silent_wakes,
            "yesterday_wakes": y.get("wakes"),
            "yesterday_with_packet": y.get("tx_wakes"),
        }
