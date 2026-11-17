"""Числовые настройки датчика: задаются в HA, применяются в ближайшее окно пробуждения."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BATTERY_KEYS, DOMAIN, FLOAT_KEYS, OFFSET_KEYS, SETTINGS, WINDOW_KEYS
from .entity import OtaEntity
from .ota import OtaError


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    mgr = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SettingNumber(mgr, key) for key, spec in SETTINGS.items() if spec[1] is not None]
                       + [StuckThresholdNumber(mgr)])


class SettingNumber(OtaEntity, NumberEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(self, mgr, key: str) -> None:
        name, vmin, vmax, step, unit, icon = SETTINGS[key]
        super().__init__(mgr, f"setting_{key}", f"{name} (BLE)")
        self.key = key
        self._attr_native_min_value = vmin
        self._attr_native_max_value = vmax
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit

    @property
    def native_value(self):
        return self.mgr.value(self.key)

    @property
    def available(self) -> bool:
        # окна рекламы — только у прошивки, которая их умеет (иначе сущность «недоступна»)
        if self.key in WINDOW_KEYS:
            return self.mgr.window_supported
        if self.key in OFFSET_KEYS:
            return self.mgr.offsets_supported
        if self.key in BATTERY_KEYS:   # v1.19: прошивка 1.6.0+
            return self.mgr.battery_supported
        return super().available

    @property
    def extra_state_attributes(self) -> dict:
        attrs = {
            "applied_value": self.mgr.applied.get(self.key),
            "pending": self.key in self.mgr.pending(),
        }
        if self.key in WINDOW_KEYS:
            attrs |= self.mgr.window_attributes()
        return attrs

    async def async_set_native_value(self, value: float) -> None:
        if self.key in OFFSET_KEYS:
            value = round(float(value), 1)
        elif self.key not in FLOAT_KEYS:
            value = int(round(value))
        try:
            await self.mgr.async_set_setting(self.key, value)
        except OtaError as err:
            raise HomeAssistantError(str(err)) from err


class StuckThresholdNumber(OtaEntity, NumberEntity):
    """Порог для «PIR stuck», мин. Настройка самой интеграции (на датчик не уходит)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 5
    _attr_native_max_value = 240
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "min"

    def __init__(self, mgr) -> None:
        super().__init__(mgr, "pir_stuck_threshold", "PIR stuck after")

    @property
    def native_value(self) -> int:
        return self.mgr.stuck_threshold_min

    async def async_set_native_value(self, value: float) -> None:
        await self.mgr.async_set_stuck_threshold(int(round(value)))
