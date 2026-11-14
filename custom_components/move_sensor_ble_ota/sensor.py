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
        OtaStatusSensor(mgr), OtaProgressSensor(mgr), SettingsStatusSensor(mgr), FirmwareVersionSensor(mgr),
        BrownoutCountSensor(mgr), CrashCountSensor(mgr), LastResetReasonSensor(mgr), LastSeenSensor(mgr),
        BatteryDaysLeftSensor(mgr), MotionEventsTodaySensor(mgr), WakesTodaySensor(mgr),
    ])


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
    """Настройки: idle (ничего не ждёт) / pending (ждут пробуждения) / applied (только что, 5 мин) / unknown / error."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [SST_IDLE, SST_PENDING, SST_APPLIED, SST_UNKNOWN, SST_ERROR]
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


class MotionEventsTodaySensor(OtaEntity, SensorEntity):
    """Сколько раз за сегодня было «занято» (переходы в ON)."""

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
