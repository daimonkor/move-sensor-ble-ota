"""Константы интеграции Move Sensor BLE OTA."""

DOMAIN = "move_sensor_ble_ota"

CONF_ADDRESS = "address"
CONF_KEY = "key"
CONF_FIRMWARE_DIR = "firmware_dir"

DEFAULT_NAME = "Move Sensor BLE"
DEFAULT_FIRMWARE_DIR = "/config/esphome/ota"
# v1.16.2: сколько последних образов .bin хранить в папке прошивок (остальные удаляются)
CONF_KEEP_IMAGES = "keep_images"
# v1.17.0: пароль OTA (Wi-Fi) и ключ API — поля настройки; хранятся в secrets.yaml, не в записи интеграции
CONF_OTA_PASSWORD = "ota_password"
CONF_API_KEY = "api_key"
DEFAULT_KEEP_IMAGES = 3
# v1.21: после скольких минут без данных — уведомление «не выходит на связь» (auto = 3 × интервал климата, ≥ 15 мин)
CONF_SILENT_AFTER = "silent_after"
SILENT_AFTER_OPTIONS = ["auto", "10", "15", "30", "60", "120"]
# если ключ в настройках пустой — берём его отсюда (тот же, что в прошивке)
SECRETS_PATH = "/config/esphome/secrets.yaml"
SECRET_NAME = "move_sensor_ble_ota_key"

# GATT-сервис прошивки (components/bthome/ble_ota.h)
SVC_UUID = "6d730001-626c-6f74-4d6f-766553656e73"
CTRL_UUID = "6d730002-626c-6f74-4d6f-766553656e73"
DATA_UUID = "6d730003-626c-6f74-4d6f-766553656e73"

CMD_HELLO = 0x01
CMD_AUTH = 0x02
CMD_BEGIN = 0x03
CMD_END = 0x05
CMD_ABORT = 0x06
CMD_STATUS = 0x07
CMD_INFO = 0x08
CMD_DIAG = 0x09   # счётчики сбросов (2026-11-14), см. ble_ota.h
CMD_WIFI_RESET = 0x12  # сброс Wi-Fi на сеть из прошивки (2026-11-14)
CMD_TEST_MODE = 0x13   # быстрый тест: просыпаться раз в 30 с, выключится сам (2026-11-14)
CMD_IDENTIFY = 0x14    # мигать светодиодом N с — найти датчик физически (2026-11-15)
IDENTIFY_SECONDS = 10


def device_id(ble_address: str) -> str:
    """Device ID = хвост MAC Wi-Fi (как суффикс имени прошивки, SSID точки доступа): у ESP32-C3
    BT MAC = Wi-Fi MAC + 2, поэтому из BLE-адреса вычитаем 2. Пример: E8:F6:0A:14:8F:9A -> 148F98."""
    try:
        v = (int(ble_address.replace(":", "").replace("-", ""), 16) - 2) & 0xFFFFFFFFFFFF
    except ValueError:
        return ""
    return f"{v & 0xFFFFFF:06X}"
TEST_MODE_MIN = 30     # на сколько минут включать быстрый тест
CMD_GET_SETTINGS = 0x10
CMD_SET_SETTINGS = 0x11
CMD_GET_BATTERY = 0x15   # v1.19: ёмкость (мА·ч) и порог «низкого» заряда (%), ответ 6 байт
CMD_SET_BATTERY = 0x16
RSP_ACK = 0x84

# коды статуса датчика -> текст
DEVICE_STATUS = {
    0: "ok",
    1: "нет авторизации (неверный ключ)",
    2: "батарея ниже порога — OTA отклонена",
    3: "неверный размер образа",
    4: "ошибка esp_ota_begin",
    5: "ошибка записи во флеш",
    6: "чужой или битый образ (не move-sensor-ble)",
    7: "CRC не совпал",
    8: "образ не прошёл проверку (esp_ota_end)",
    9: "не удалось выбрать раздел загрузки",
    10: "неверное состояние/команда",
}

# состояния менеджера (сенсор «OTA status»)
ST_IDLE = "idle"
ST_BUILDING = "building"        # сборка прошивки в аддоне ESPHome (build_and_flash)
ST_WAITING = "waiting"          # ждём окно (пробуждение датчика)
ST_CONNECTING = "connecting"
ST_FLASHING = "flashing"
ST_SUCCESS = "success"
ST_ERROR = "error"
ST_CANCELLED = "cancelled"

SIGNAL_UPDATE = f"{DOMAIN}_update_{{}}"
SIGNAL_SETTINGS_EVENT = f"{DOMAIN}_settings_event_{{}}"   # v1.15: событие «Settings change»
SETTINGS_EVENT_TYPES = ["saved_wifi", "saved_ble", "changed_on_sensor", "restored"]

SERVICE_FLASH = "flash"
SERVICE_BUILD_FLASH = "build_and_flash"
ATTR_CONFIGURATION = "configuration"
ATTR_START = "start"
DEFAULT_ESPHOME_CONFIG = "move-sensor-ble.yaml"   # YAML в /config/esphome (старое общее имя — запасной вариант)
# 1.13.1: у каждого датчика свой YAML (общая прошивка в move_sensor_ble/base.yaml) — его имя по Device ID
ESPHOME_DIR = "/config/esphome"
DEVICE_ESPHOME_CONFIG = "move-sensor-ble-{device_id}.yaml"   # device_id строчными: 148f98
SERVICE_CANCEL = "cancel"
ATTR_FILE = "file"
ATTR_TIMEOUT = "timeout"
ATTR_ENTRY_ID = "config_entry_id"
DEFAULT_TIMEOUT_MIN = 180

# ---- настройки, которые передаются датчику в окно пробуждения
# key: (name, min, max, step, unit, icon); led — переключатель
SETTINGS = {
    "detect_delay": ("Detect delay", 0, 5000, 50, "ms", "mdi:timer-sand"),
    "keep_time": ("Keep time", 5, 600, 5, "s", "mdi:timer-outline"),
    "climate_interval": ("Climate report interval", 0, 240, 1, "min", "mdi:thermometer-auto"),
    "voltage_factor": ("Voltage factor", 1.5, 2.5, 0.001, None, "mdi:division"),
    "led": ("Motion tracking LED", None, None, None, None, "mdi:led-on"),
    # окно рекламы после пакета (2026-11-14): сколько датчик не спит, чтобы прокси услышал пакет
    # и интеграция успела подключиться. Авто — интеграция подбирает по RSSI и пропускам (AUTO_WINDOW_LEVELS)
    "motion_window": ("Motion window", 300, 5000, 100, "ms", "mdi:broadcast"),
    "climate_window": ("Climate window", 300, 5000, 100, "ms", "mdi:access-point"),
    # поправки SHT41 (2026-11-14): прибавляются на датчике к измерению, бывают и плюс, и минус.
    # Поправка температуры пересчитывает и влажность по точке росы (абсолютная влажность та же).
    "temp_offset": ("Temperature offset", -10, 10, 0.1, "°C", "mdi:thermometer-plus"),
    "hum_offset": ("Humidity offset", -20, 20, 0.1, "%", "mdi:water-plus"),
    # v1.19 / прошивка 1.6.0: батарея (li_battery) — отдельной BLE-командой 0x15/0x16
    "battery_capacity": ("Battery capacity", 100, 5000, 10, "mAh", "mdi:battery-plus-variant"),
    "battery_low": ("Battery low level", 5, 50, 1, "%", "mdi:battery-low"),
}
BATTERY_KEYS = ("battery_capacity", "battery_low")
WINDOW_KEYS = ("motion_window", "climate_window")
OFFSET_KEYS = ("temp_offset", "hum_offset")
FLOAT_KEYS = ("voltage_factor", "temp_offset", "hum_offset")
# Авто-окно: уровень по медиане RSSI последних пакетов -> (motion_ms, climate_ms).
# Замер 2026-11-14 (RSSI −56, прокси на настройках ESPHome по умолчанию): 0 потерь даже при 300 мс,
# подключение успевало при 600 мс; запас на слабый сигнал — уровни ниже.
AUTO_WINDOW_LEVELS = [(800, 1500), (1500, 2500), (3000, 4500)]
AUTO_WINDOW_RSSI = (-75, -85)   # >= -75 -> уровень 0, >= -85 -> 1, ниже -> 2
AUTO_WINDOW_HYST_DB = 3         # к лучшему уровню — только если сигнал лучше порога на 3 дБ
AUTO_WINDOW_SAMPLES = 10        # медиана по последним N пакетам (минимум 5)
AUTO_WINDOW_BOOST_H = 24        # пропущен климат-пакет -> уровень +1 на сутки
# статус настроек (сенсор «Settings status»)
SST_UNKNOWN = "not_read"      # ещё ни разу не читали с датчика
SST_PENDING = "waiting"      # есть изменения, ждут окна
SST_APPLIED = "applied"      # датчик подтвердил (держится SETTINGS_APPLIED_SHOW_MIN, потом -> idle)
SST_IDLE = "in_sync"            # ничего не ждёт: всё на датчике совпадает с HA
SETTINGS_APPLIED_SHOW_MIN = 5
SST_ERROR = "error"
SERVICE_READ_SETTINGS = "read_settings"
SERVICE_RESET_WIFI = "reset_wifi"
SERVICE_ROLLBACK = "rollback"   # v1.18: прошить последнюю резервную копию
SERVICE_FAST_TEST = "fast_test"   # отладка: просыпаться раз в 30 с N минут (0 = выкл), без UI

# ---- диагностика сбросов (DIAG). Порядок счётчиков — как в ответе 0x89.
DIAG_FIELDS = ("power_on", "brownout", "panic", "watchdog", "software", "other")
# esp_reset_reason_t (ESP-IDF 5.x) -> текст для «Last reset reason»
RESET_REASONS = {
    0: "unknown", 1: "power on", 2: "reset pin", 3: "software", 4: "panic",
    5: "watchdog (int)", 6: "watchdog (task)", 7: "watchdog", 8: "deep sleep",
    9: "brownout", 10: "sdio", 11: "usb", 12: "jtag", 13: "efuse",
    14: "power glitch", 15: "cpu lockup",
}
# BTHome: сервис-данные и Wake count (count_uint16, 0x3D) — по его сбросу видно перезагрузку
BTHOME_UUID_FULL = "0000fcd2-0000-1000-8000-00805f9b34fb"
BTHOME_WAKE_COUNT_ID = 0x3D
