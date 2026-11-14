"""Работа с датчиком через окно пробуждения: настройки и прошивка по BLE.

Один воркер на датчик. Пока есть работа (несохранённые настройки, первое чтение
настроек или прошивка), он ждёт рекламу датчика, подключается через Bluetooth-прокси
в окно пробуждения и за одно подключение: авторизация -> настройки -> прошивка.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import struct
import time
import zlib
from dataclasses import dataclass
from datetime import datetime

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CMD_ABORT,
    CMD_AUTH,
    CMD_BEGIN,
    CMD_END,
    CMD_GET_SETTINGS,
    CMD_HELLO,
    CMD_INFO,
    CMD_DIAG,
    CMD_WIFI_RESET,
    CMD_TEST_MODE,
    TEST_MODE_MIN,
    DIAG_FIELDS,
    RESET_REASONS,
    BTHOME_UUID_FULL,
    BTHOME_WAKE_COUNT_ID,
    CMD_SET_SETTINGS,
    CMD_STATUS,
    CONF_ADDRESS,
    CONF_FIRMWARE_DIR,
    CONF_KEY,
    CTRL_UUID,
    DATA_UUID,
    DEFAULT_FIRMWARE_DIR,
    DEVICE_STATUS,
    DOMAIN,
    RSP_ACK,
    SECRET_NAME,
    SECRETS_PATH,
    SETTINGS,
    SIGNAL_UPDATE,
    SST_APPLIED,
    SST_IDLE,
    SETTINGS_APPLIED_SHOW_MIN,
    SST_ERROR,
    SST_PENDING,
    SST_UNKNOWN,
    ST_CANCELLED,
    ST_BUILDING,
    ST_CONNECTING,
    DEFAULT_ESPHOME_CONFIG,
    ST_ERROR,
    ST_FLASHING,
    ST_IDLE,
    ST_SUCCESS,
    ST_WAITING,
    WINDOW_KEYS,
    AUTO_WINDOW_LEVELS,
    AUTO_WINDOW_RSSI,
    AUTO_WINDOW_HYST_DB,
    AUTO_WINDOW_SAMPLES,
    AUTO_WINDOW_BOOST_H,
)

_LOGGER = logging.getLogger(__name__)

APP_PARTITION_MAX = 0x1E0000  # app0/app1 в partitions_c3_4mb.csv
ACK_TIMEOUT = 8.0
CMD_TIMEOUT = 6.0
BLOCK_RETRIES = 6
MAX_CHUNK = 240
CHUNKS_PER_SYNC = 4
STORE_VERSION = 1
RESULT_HOLD_S = 120  # сколько показывать success/error/cancelled, потом idle и 0 %

# порядок полей настроек в протоколе: detect_ms, keep_s, climate_min (u16), led (u8), vfactor*1000 (u16)
SETTINGS_FMT = "<HHHBH"
SETTINGS_FMT2 = "<HHHBHHH"   # + motion_window_ms, climate_window_ms (прошивка с 2026-11-14)
SETTINGS_FMT3 = "<HHHBHHHhh"  # + temp_offset*100, hum_offset*10 (int16, 0x7FFF = нет; прошивка с 2026-11-14 23:xx)
OFFSET_NONE = 0x7FFF
SETTINGS_MASK = {"detect_delay": 0x01, "keep_time": 0x02, "climate_interval": 0x04, "led": 0x08, "voltage_factor": 0x10,
                 "motion_window": 0x20, "climate_window": 0x40,
                 "temp_offset": 0x80, "hum_offset": 0x80}  # поправки — общий бит, отсутствующая = 0x7FFF


# длины объектов BTHome v2 (байт данных) — только то, что шлёт датчик и соседние типы;
# на неизвестном объекте разбор останавливается
_BTHOME_LEN = {0x00: 1, 0x01: 1, 0x02: 2, 0x03: 2, 0x04: 3, 0x05: 3, 0x08: 2, 0x09: 1,
               0x0C: 2, 0x2E: 1, 0x3D: 2, 0x3E: 4, 0x45: 2, 0x4A: 2}
_BTHOME_BINARY = range(0x0F, 0x2E)  # бинарные сенсоры: 1 байт


def bthome_parse(data: bytes) -> dict:
    """Wake count (0x3D, u16) и Battery (0x01, %) из сервис-данных BTHome v2 (без шифрования)."""
    out: dict = {}
    if not data or data[0] & 0x01:  # bit0 — шифрование
        return out
    i = 1
    while i < len(data):
        oid = data[i]
        n = _BTHOME_LEN.get(oid, 1 if oid in _BTHOME_BINARY else None)
        if n is None or i + 1 + n > len(data):
            break
        if oid == BTHOME_WAKE_COUNT_ID:
            out["wake"] = int.from_bytes(data[i + 1:i + 3], "little")
        elif oid == 0x01:
            out["battery"] = data[i + 1]
        elif oid == 0x00:
            out["packet_id"] = data[i + 1]
        elif oid == 0x23:
            out["occupancy"] = bool(data[i + 1])
        elif oid == 0x0C:
            out["voltage"] = int.from_bytes(data[i + 1:i + 3], "little") / 1000
        i += 1 + n
    return out


class OtaError(Exception):
    """Ошибка, после которой повторять в следующее окно бессмысленно."""


class RetryLater(Exception):
    """Окно закрылось/связь оборвалась — ждём следующее пробуждение."""


@dataclass
class FirmwareImage:
    path: str
    data: bytes
    crc: int
    project: str
    version: str
    from_factory: bool


@dataclass
class OtaJob:
    image: FirmwareImage
    key: str
    deadline: float  # time.monotonic()


def load_firmware(path: str) -> FirmwareImage:
    """Читает .bin; factory.bin режется до образа приложения (блокирующая, в executor)."""
    with open(path, "rb") as f:
        raw = f.read()
    if len(raw) < 1024 or raw[0] != 0xE9:
        raise OtaError(f"{os.path.basename(path)}: не похоже на прошивку ESP32")
    data = raw
    from_factory = False
    # factory: загрузчик с 0x0, таблица разделов на 0x8000 (записи по 32 байта, магия AA 50)
    if len(raw) > 0x8000 + 32 and raw[0x8000:0x8002] == b"\xaa\x50":
        app_off = None
        for i in range(0x8000, 0x8000 + 0xC00, 32):
            ent = raw[i : i + 32]
            if ent[0:2] != b"\xaa\x50":
                break
            ptype, off = ent[2], struct.unpack_from("<I", ent, 4)[0]
            if ptype == 0x00:  # app
                app_off = off
                break
        if app_off is None or app_off >= len(raw):
            raise OtaError("factory.bin: не нашёл раздел приложения")
        data = raw[app_off:]
        from_factory = True
        if data[0] != 0xE9:
            raise OtaError("factory.bin: по смещению приложения нет образа ESP32")
    if len(data) > APP_PARTITION_MAX:
        raise OtaError(f"образ {len(data)} байт больше раздела app ({APP_PARTITION_MAX})")
    desc = data[32 : 32 + 256]
    project = version = "?"
    if len(desc) >= 80 and struct.unpack_from("<I", desc, 0)[0] == 0xABCD5432:
        version = desc[16:48].split(b"\0", 1)[0].decode(errors="replace")
        project = desc[48:80].split(b"\0", 1)[0].decode(errors="replace")
    return FirmwareImage(path, data, zlib.crc32(data) & 0xFFFFFFFF, project, version, from_factory)


def newest_firmware(directory: str) -> str | None:
    """Самый свежий *.bin в папке (блокирующая)."""
    try:
        files = [
            os.path.join(directory, n)
            for n in os.listdir(directory)
            if n.lower().endswith(".bin") and os.path.isfile(os.path.join(directory, n))
        ]
    except FileNotFoundError:
        return None
    return max(files, key=os.path.getmtime) if files else None


def read_secret_key() -> str | None:
    """Ключ из /config/esphome/secrets.yaml (блокирующая)."""
    try:
        with open(SECRETS_PATH, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith(SECRET_NAME + ":"):
                    v = s.split(":", 1)[1].split("#", 1)[0].strip().strip("\"'")
                    return v or None
    except OSError:
        return None
    return None


def unpack_settings(payload: bytes) -> dict:
    d, k, ci, led, vf = struct.unpack_from(SETTINGS_FMT, payload, 0)
    out: dict = {}
    if d != 0xFFFF:
        out["detect_delay"] = d
    if k != 0xFFFF:
        out["keep_time"] = k
    if ci != 0xFFFF:
        out["climate_interval"] = ci
    if led <= 1:
        out["led"] = bool(led)
    if vf != 0xFFFF:
        out["voltage_factor"] = round(vf / 1000.0, 3)
    if len(payload) >= struct.calcsize(SETTINGS_FMT2):  # прошивка с окнами рекламы
        mw, cw = struct.unpack_from("<HH", payload, struct.calcsize(SETTINGS_FMT))
        if mw != 0xFFFF:
            out["motion_window"] = mw
        if cw != 0xFFFF:
            out["climate_window"] = cw
    if len(payload) >= struct.calcsize(SETTINGS_FMT3):  # прошивка с поправками SHT41
        to, ho = struct.unpack_from("<hh", payload, struct.calcsize(SETTINGS_FMT2))
        if to != OFFSET_NONE:
            out["temp_offset"] = round(to / 100.0, 2)
        if ho != OFFSET_NONE:
            out["hum_offset"] = round(ho / 10.0, 1)
    return out


def pack_settings(values: dict) -> bytes:
    def u16(key: str, scale: float = 1.0) -> int:
        v = values.get(key)
        return 0xFFFF if v is None else max(0, min(0xFFFE, int(round(float(v) * scale))))

    def i16(key: str, scale: float) -> int:
        v = values.get(key)
        return OFFSET_NONE if v is None else max(-32767, min(32766, int(round(float(v) * scale))))

    led = values.get("led")
    # старая прошивка читает первые 9/13 байт и не знает старших битов маски — хвост ей не мешает
    return struct.pack(
        SETTINGS_FMT3,
        u16("detect_delay"),
        u16("keep_time"),
        u16("climate_interval"),
        255 if led is None else (1 if led else 0),
        u16("voltage_factor", 1000.0),
        u16("motion_window"),
        u16("climate_window"),
        i16("temp_offset", 100.0),
        i16("hum_offset", 10.0),
    )


def same_value(key: str, a, b) -> bool:
    if a is None or b is None:
        return False
    if key == "led":
        return bool(a) == bool(b)
    if key == "voltage_factor":
        return abs(float(a) - float(b)) < 0.0005
    if key in ("temp_offset", "hum_offset"):
        return abs(float(a) - float(b)) < 0.05
    return int(round(float(a))) == int(round(float(b)))


class OtaManager:
    """Настройки + прошивка одного датчика."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS].upper()
        self.name: str = entry.title
        # --- прошивка
        self.status: str = ST_IDLE
        self.progress: int = 0
        self.error: str | None = None
        self.file: str | None = None
        self.new_version: str | None = None
        self.device_version: str | None = None
        self.device_battery: int | None = None
        self.started: datetime | None = None
        self.finished: datetime | None = None
        self.attempts: int = 0
        self.speed_kbs: float | None = None
        self._ota: OtaJob | None = None
        self.last_result: str | None = None  # итог последней прошивки (остаётся в атрибутах)
        self._unsub_reset = None
        # --- настройки
        self.desired: dict = {}   # что задано в HA и ещё не подтверждено датчиком
        self.applied: dict = {}   # что датчик подтвердил последним
        self.settings_synced: datetime | None = None
        self.settings_applied_at: datetime | None = None  # когда датчик подтвердил последние изменения
        self._last_sst: str | None = None
        self.firmware_version: str | None = None  # версия сборки на датчике (по INFO)
        self.firmware_seen: datetime | None = None
        self.settings_error: str | None = None
        self.settings_note: str | None = None
        self._need_read = False
        # --- диагностика сбросов (DIAG): читается при перезагрузке датчика и в каждом подключении
        self.diag: dict | None = None      # {"last_reason": int, "power_on": n, "brownout": n, ...}
        self.diag_seen: datetime | None = None
        self.diag_unsupported = False      # прошивка без DIAG (старая)
        self._need_diag = False
        self._last_wake: int | None = None
        self._diag_deferred = False        # перезагрузка была на почти пустой батарее — прочитать позже
        self._rebooted_after_flash = False  # после success видели сброс Wake count
        # --- сброс Wi-Fi на сеть из прошивки (команда 0x12), ждёт окна как настройки
        self.wifi_reset: dict = {}          # {"state": pending|sent|error, "requested", "done", "error"}
        # --- быстрый тест (0x13): {"want": bool, "state": pending|active|off|error, "until", "error"}
        self.test_mode: dict = {}
        # --- Last seen (2026-11-14): последний пакет BTHome от датчика
        self.last_seen: datetime | None = None
        self.last_seen_info: dict = {}
        self._last_pid: int | None = None
        # --- статистика за сутки (2026-11-14): ON-события, пробуждения (с отправкой и тихие)
        self.stats: dict = {}
        self._stats_wake: int | None = None
        # --- прогноз батареи: история % раз в 30 мин с последней зарядки
        self.batt_hist: list[list] = []      # [[unix_ts, pct], ...]
        # --- PIR stuck: «занято» непрерывно дольше порога
        self.occupied: bool | None = None
        self.occupied_since: datetime | None = None
        self.stuck_threshold_min: int = 30
        # --- авто-окно рекламы (2026-11-14): по RSSI и пропущенным климат-пакетам
        self.auto_window: bool = True
        self.rssi_hist: list[int] = []            # RSSI последних пакетов (новый packet id)
        self.window_level: int | None = None
        self.window_boost_until: datetime | None = None
        self.missed_climate: int = 0              # пропущенные климат-пакеты (всего)
        self._missed_for: str | None = None       # last_seen, за который пропуск уже посчитан
        self._unsub_stuck = None
        self._unsub_passive = None
        self._store: Store = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        # --- воркер
        self._worker: asyncio.Task | None = None
        self._adv_event = asyncio.Event()
        self._unsub_adv = None
        self.window_state: str = "idle"  # idle / waiting / connected — для атрибутов
        self.last_build: dict | None = None  # итог последней сборки (build_and_flash)

    # ================= загрузка / сохранение =================
    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.desired = data.get("desired", {})
        self.firmware_version = data.get("firmware_version")
        ts_fw = data.get("firmware_seen")
        self.firmware_seen = dt_util.parse_datetime(ts_fw) if ts_fw else None
        self.applied = data.get("applied", {})
        ts = data.get("synced")
        self.settings_synced = dt_util.parse_datetime(ts) if ts else None
        # первый запуск — прочитать настройки с датчика при ближайшем пробуждении
        self._need_read = not self.applied
        self.diag = data.get("diag")
        ts_d = data.get("diag_seen")
        self.diag_seen = dt_util.parse_datetime(ts_d) if ts_d else None
        self._need_diag = self.diag is None
        self.wifi_reset = data.get("wifi_reset") or {}
        self.test_mode = data.get("test_mode") or {}
        self.stats = data.get("stats") or {}
        self.batt_hist = data.get("batt_hist") or []
        self.occupied = data.get("occupied")
        ts_os = data.get("occupied_since")
        self.occupied_since = dt_util.parse_datetime(ts_os) if ts_os else None
        self.stuck_threshold_min = int(data.get("stuck_threshold_min") or 30)
        aw = data.get("auto_window") or {}
        self.auto_window = aw.get("on", True)
        self.rssi_hist = aw.get("rssi") or []
        self.window_level = aw.get("level")
        self.window_boost_until = dt_util.parse_datetime(aw["boost_until"]) if aw.get("boost_until") else None
        self.missed_climate = int(aw.get("missed") or 0)
        self._roll_day()
        from homeassistant.helpers.event import async_track_time_interval  # noqa: PLC0415
        from datetime import timedelta  # noqa: PLC0415

        # без пакетов (PIR держит HIGH — датчик молча спит) «залипание» тоже надо увидеть
        self._unsub_stuck = async_track_time_interval(self.hass, self._tick, timedelta(minutes=1))
        ts_ls = data.get("last_seen")
        self.last_seen = dt_util.parse_datetime(ts_ls) if ts_ls else None
        self.last_seen_info = data.get("last_seen_info") or {}
        # пассивно слушаем рекламу BTHome: сброс Wake count = датчик перезагрузился -> прочитать DIAG
        self._unsub_passive = bluetooth.async_register_callback(
            self.hass,
            self._on_bthome,
            bluetooth.BluetoothCallbackMatcher(address=self.address, connectable=False),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
        self._ensure_worker()

    def _save(self) -> None:
        self._store.async_delay_save(
            lambda: {
                "desired": self.desired,
                "applied": self.applied,
                "synced": self.settings_synced.isoformat() if self.settings_synced else None,
                "firmware_version": self.firmware_version,
                "firmware_seen": self.firmware_seen.isoformat() if self.firmware_seen else None,
                "diag": self.diag,
                "diag_seen": self.diag_seen.isoformat() if self.diag_seen else None,
                "last_seen": self.last_seen.isoformat() if self.last_seen else None,
                "last_seen_info": self.last_seen_info,
                "stats": self.stats,
                "wifi_reset": self.wifi_reset,
                "test_mode": self.test_mode,
                "batt_hist": self.batt_hist,
                "occupied": self.occupied,
                "occupied_since": self.occupied_since.isoformat() if self.occupied_since else None,
                "stuck_threshold_min": self.stuck_threshold_min,
                "auto_window": {
                    "on": self.auto_window,
                    "rssi": self.rssi_hist,
                    "level": self.window_level,
                    "boost_until": self.window_boost_until.isoformat() if self.window_boost_until else None,
                    "missed": self.missed_climate,
                },
            },
            10.0,
        )

    # ================= публичное: настройки =================
    def pending(self) -> dict:
        return {k: v for k, v in self.desired.items() if not same_value(k, v, self.applied.get(k))}

    def value(self, key: str):
        """Значение для сущности: заданное (если ждёт применения), иначе подтверждённое датчиком."""
        if key in self.desired:
            return self.desired[key]
        return self.applied.get(key)

    @property
    def settings_status(self) -> str:
        if self.settings_error and (self.pending() or self._need_read):
            return SST_ERROR
        if self.pending():
            return SST_PENDING
        if not self.applied:
            return SST_UNKNOWN
        if self.settings_applied_at is not None:
            from datetime import timedelta  # noqa: PLC0415
            if dt_util.now() - self.settings_applied_at < timedelta(minutes=SETTINGS_APPLIED_SHOW_MIN):
                return SST_APPLIED
        return SST_IDLE

    def settings_attributes(self) -> dict:
        return {
            "pending": self.pending(),
            "applied": self.applied,
            "last_sync": self.settings_synced.isoformat() if self.settings_synced else None,
            "last_applied": self.settings_applied_at.isoformat() if self.settings_applied_at else None,
            "error": self.settings_error,
            "note": self.settings_note,
            "window": self.window_state,
        }

    async def async_set_setting(self, key: str, value) -> None:
        if key not in SETTINGS:
            raise OtaError(f"неизвестная настройка {key}")
        if key in WINDOW_KEYS and self.auto_window:
            # ручное окно — авто выключаем, иначе следующий пакет перезапишет выбор
            self.auto_window = False
            _LOGGER.info("%s: %s задано вручную — авто-окно выключено", self.name, key)
        self._queue_setting(key, value)
        self._ensure_worker()

    def _queue_setting(self, key: str, value) -> None:
        if same_value(key, value, self.applied.get(key)):
            self.desired.pop(key, None)  # вернули как было — ничего слать не нужно
        else:
            self.desired[key] = value
        self.settings_error = None
        self.settings_note = None
        self._save()
        self._notify()

    # ================= авто-окно рекламы =================
    @property
    def window_supported(self) -> bool:
        """Прошивка умеет окна (прочитаны с датчика)."""
        return "motion_window" in self.applied

    @property
    def offsets_supported(self) -> bool:
        """Прошивка умеет поправки SHT41 (прочитаны с датчика)."""
        return "temp_offset" in self.applied

    def rssi_median(self) -> float | None:
        h = self.rssi_hist[-AUTO_WINDOW_SAMPLES:]
        if len(h) < 5:
            return None
        s = sorted(h)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

    def _window_target_level(self) -> int | None:
        med = self.rssi_median()
        if med is None:
            return None
        hi, lo = AUTO_WINDOW_RSSI
        raw = 0 if med >= hi else (1 if med >= lo else 2)
        cur = self.window_level
        if cur is not None and raw < cur:
            # к лучшему уровню — только с запасом (гистерезис), чтобы не дёргать датчик
            thr = hi if raw == 0 else lo
            if med < thr + AUTO_WINDOW_HYST_DB:
                raw = cur
        if self.window_boost_until and self.window_boost_until > dt_util.utcnow():
            raw = min(raw + 1, len(AUTO_WINDOW_LEVELS) - 1)
        return raw

    def _auto_window(self) -> None:
        if not self.auto_window or not self.window_supported:
            return
        lvl = self._window_target_level()
        if lvl is None:
            return
        motion, climate = AUTO_WINDOW_LEVELS[lvl]
        changed = False
        for key, val in (("motion_window", motion), ("climate_window", climate)):
            if not same_value(key, val, self.value(key)):
                self._queue_setting(key, val)
                changed = True
        if lvl != self.window_level:
            _LOGGER.info("%s: авто-окно уровень %s (RSSI медиана %s дБм) -> %s/%s мс",
                         self.name, lvl, self.rssi_median(), motion, climate)
            self.window_level = lvl
            self._save()
        if changed:
            self._ensure_worker()

    def window_attributes(self) -> dict:
        return {
            "auto": self.auto_window,
            "level": self.window_level,
            "rssi_median": self.rssi_median(),
            "boost_until": self.window_boost_until.isoformat() if self.window_boost_until else None,
            "missed_climate_packets": self.missed_climate,
            "supported_by_firmware": self.window_supported,
        }

    async def async_set_auto_window(self, on: bool) -> None:
        self.auto_window = on
        self._save()
        self._notify()
        if on:
            self._auto_window()

    async def async_discard_pending(self) -> None:
        self.desired = {}
        self.settings_error = None
        self._save()
        self._notify()

    async def async_request_read(self) -> None:
        self._need_read = True
        self.settings_error = None
        self._notify()
        self._ensure_worker()

    # ================= публичное: прошивка =================
    @property
    def busy(self) -> bool:
        return self._ota is not None

    @property
    def firmware_dir(self) -> str:
        return self.entry.options.get(CONF_FIRMWARE_DIR) or self.entry.data.get(
            CONF_FIRMWARE_DIR, DEFAULT_FIRMWARE_DIR
        )

    def attributes(self) -> dict:
        return {
            "file": self.file,
            "new_version": self.new_version,
            "last_result": self.last_result,
            "device_version_before": self.device_version,
            "device_version_now": self.firmware_version,
            "device_battery": self.device_battery,
            "error": self.error,
            "attempts": self.attempts,
            "speed_kbs": self.speed_kbs,
            "started": self.started.isoformat() if self.started else None,
            "finished": self.finished.isoformat() if self.finished else None,
            "address": self.address,
            "firmware_dir": self.firmware_dir,
            "last_build": self.last_build,
            "wifi_reset": self.wifi_reset or None,
        }

    async def _key(self) -> str:
        key = self.entry.options.get(CONF_KEY) or self.entry.data.get(CONF_KEY)
        if not key:
            key = await self.hass.async_add_executor_job(read_secret_key)
        if not key:
            raise OtaError(f"нет ключа: задай его в настройках или {SECRET_NAME} в {SECRETS_PATH}")
        return key

    async def async_build_and_flash(
        self, configuration: str | None = None, timeout_min: int = 180, start: bool = True
    ) -> str:
        """Собрать YAML в аддоне ESPHome, положить firmware.ota.bin в папку прошивок и
        (start) прошить по BLE в ближайшее окно. Возвращает путь к .bin."""
        from .builder import BuildError, build_firmware  # noqa: PLC0415

        if self._ota is not None or self.status == ST_BUILDING:
            raise OtaError("сборка или прошивка уже идёт")
        cfg = configuration or DEFAULT_ESPHOME_CONFIG
        self.last_build = {"configuration": cfg, "started": dt_util.now().isoformat()}
        self._set(ST_BUILDING, progress=0, error=None, finished=None, file=None, new_version=None)

        def on_progress(p: int) -> None:
            if self.status == ST_BUILDING and p != self.progress:
                self.progress = max(0, min(100, p))
                self._notify()

        try:
            data = await build_firmware(self.hass, cfg, on_progress)
        except BuildError as err:
            self.last_build["error"] = str(err)
            self._set(ST_ERROR, error=f"сборка: {err}", finished=dt_util.now())
            raise OtaError(f"сборка: {err}") from err
        name = f"{os.path.splitext(os.path.basename(cfg))[0]}-{dt_util.now().strftime('%Y%m%d-%H%M%S')}.ota.bin"
        path = os.path.join(self.firmware_dir, name)

        def _write() -> None:
            os.makedirs(self.firmware_dir, exist_ok=True)
            with open(path + ".part", "wb") as f:
                f.write(data)
            os.replace(path + ".part", path)

        await self.hass.async_add_executor_job(_write)
        self.last_build.update({"file": path, "size": len(data), "built": dt_util.now().isoformat()})
        _LOGGER.info("%s: built %s (%d bytes)", self.name, path, len(data))
        if start:
            self.status = ST_IDLE  # async_start сам выставит waiting
            await self.async_start(path, timeout_min)
        else:
            self._set(ST_IDLE, progress=0)
        return path

    async def async_start(self, file: str | None = None, timeout_min: int = 180) -> None:
        if self._ota is not None:
            raise OtaError("прошивка уже идёт или ждёт окно")
        path = file
        if path and not os.path.isabs(path):
            path = os.path.join(self.firmware_dir, path)
        if not path:
            path = await self.hass.async_add_executor_job(newest_firmware, self.firmware_dir)
            if not path:
                raise OtaError(f"в {self.firmware_dir} нет .bin файлов")
        image = await self.hass.async_add_executor_job(load_firmware, path)
        key = await self._key()
        self.file = path
        self.new_version = image.version
        self.error = None
        self.progress = 0
        self.attempts = 0
        self.speed_kbs = None
        self.started = dt_util.now()
        self.finished = None
        self._ota = OtaJob(image, key, time.monotonic() + timeout_min * 60)
        _LOGGER.info(
            "%s: OTA %s (%d bytes, project %s, version %s%s), waiting for wake window",
            self.name, os.path.basename(path), len(image.data), image.project, image.version,
            ", cut from factory.bin" if image.from_factory else "",
        )
        self._set(ST_WAITING)
        self._ensure_worker()

    async def async_cancel(self) -> None:
        """Отменить прошивку (настройки продолжат ждать окно)."""
        if self._ota is None:
            return
        self._ota = None
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        self._set(ST_CANCELLED, finished=dt_util.now())
        self._ensure_worker()

    async def async_shutdown(self) -> None:
        self._ota = None
        if self._unsub_reset is not None:
            self._unsub_reset()
            self._unsub_reset = None
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
        self._stop_adv_watch()
        if self._unsub_passive is not None:
            self._unsub_passive()
            self._unsub_passive = None
        if self._unsub_stuck is not None:
            self._unsub_stuck()
            self._unsub_stuck = None

    # ================= внутреннее =================
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.entry.entry_id))

    def _set(self, status: str | None = None, **kw) -> None:
        if status is not None:
            self.status = status
            if self._unsub_reset is not None:
                self._unsub_reset()
                self._unsub_reset = None
            if status in (ST_SUCCESS, ST_ERROR, ST_CANCELLED):
                self.last_result = status
                # итог виден RESULT_HOLD_S секунд, потом статус idle и прогресс 0 %
                self._unsub_reset = async_call_later(self.hass, RESULT_HOLD_S, self._reset_result)
        for k, v in kw.items():
            setattr(self, k, v)
        self._notify()

    @callback
    def _reset_result(self, _now=None) -> None:
        self._unsub_reset = None
        if self._ota is None and self.status in (ST_SUCCESS, ST_ERROR, ST_CANCELLED):
            self.status = ST_IDLE
            self.progress = 0
            self._notify()

    def _settings_work(self) -> bool:
        return (bool(self.pending()) or self._need_read) and self.settings_error is None

    def _has_work(self) -> bool:
        return (self._ota is not None or self._settings_work()
                or (self._need_diag and not self.diag_unsupported)
                or self.wifi_reset.get("state") == "pending"
                or self.test_mode.get("state") == "pending")

    # ================= диагностика сбросов =================
    @property
    def crash_count(self) -> int | None:
        return None if self.diag is None else self.diag.get("panic", 0) + self.diag.get("watchdog", 0)

    @property
    def last_reset_reason(self) -> str | None:
        if self.diag is None:
            return None
        code = self.diag.get("last_reason")
        return RESET_REASONS.get(code, f"code {code}")

    @callback
    def _on_bthome(self, service_info: bluetooth.BluetoothServiceInfoBleak, change) -> None:
        data = service_info.service_data.get(BTHOME_UUID_FULL, b"")
        parsed = bthome_parse(data)
        if data:
            self._mark_seen(service_info, parsed)
        wake = parsed.get("wake")
        if wake is not None:
            prev, self._last_wake = self._last_wake, wake
            # Wake count живёт в RTC-памяти: после любой загрузки «не из сна» начинается с 1.
            # Уменьшился (и это не переполнение u16) -> перезагрузка -> прочитать DIAG.
            if prev is not None and wake < prev and not (prev > 65000 and wake < 1000):
                _LOGGER.info("%s: wake count %d -> %d, device rebooted", self.name, prev, wake)
                self._diag_deferred = True
                if self.status == ST_SUCCESS:
                    self._rebooted_after_flash = True
        if self._diag_deferred:
            batt = parsed.get("battery")
            # на почти пустой банке (brownout-цикл) не добавляем подключение — прочитаем после зарядки
            if batt is None or batt > 10:
                self._diag_deferred = False
                self._need_diag = True
                self._ensure_worker()

    def _mark_seen(self, service_info: bluetooth.BluetoothServiceInfoBleak, parsed: dict) -> None:
        """Last seen: за одно пробуждение датчик шлёт ~150 копий пакета (реклама каждые 20-30 мс),
        поэтому состояние пишем только на новом packet id или если прошло > 30 с."""
        now = dt_util.utcnow()
        pid = parsed.get("packet_id")
        if (
            self.last_seen is not None
            and pid == self._last_pid
            and (now - self.last_seen).total_seconds() < 30
        ):
            return
        self._last_pid = pid
        self._account(parsed, now)
        if service_info.rssi is not None and service_info.rssi > -127:
            self.rssi_hist = (self.rssi_hist + [int(service_info.rssi)])[-AUTO_WINDOW_SAMPLES * 2:]
            self._auto_window()
        self.last_seen = now
        self.last_seen_info = {
            "packet_id": pid,
            "wake_count": parsed.get("wake"),
            "battery": parsed.get("battery"),
            "rssi": service_info.rssi,
            "source": service_info.source,
        }
        self._save()
        self._notify()

    # ================= статистика, батарея, PIR stuck (2026-11-14) =================
    def _roll_day(self) -> None:
        today = dt_util.now().date().isoformat()
        if self.stats.get("date") != today:
            if self.stats.get("date"):
                self.stats = {"date": today, "yesterday": {k: v for k, v in self.stats.items()
                                                           if k not in ("date", "yesterday")} | {"date": self.stats["date"]}}
            else:
                self.stats = {"date": today}
            for k in ("motion_events", "wakes", "tx_wakes"):
                self.stats.setdefault(k, 0)

    def _account(self, parsed: dict, now: datetime) -> None:
        self._roll_day()
        # пробуждения: Wake count растёт на каждое (и тихое тоже); новое значение = пробуждение с отправкой
        wake = parsed.get("wake")
        rebooted = False
        if wake is not None:
            prev = self._stats_wake if self._stats_wake is not None else self.stats.get("last_wake")
            # счётчик пробуждений сбросился — датчик перезагрузился (питание, OTA, сбой)
            rebooted = prev is not None and wake < prev
            if prev is None or wake != prev:
                if prev is not None:
                    delta = wake - prev if wake > prev else wake  # после перезагрузки счёт с 1
                    if 0 < delta < 5000:
                        self.stats["wakes"] += delta
                        self.stats["tx_wakes"] += 1
                self._stats_wake = wake
                self.stats["last_wake"] = wake
        # ON-события и время начала «занято»
        occ = parsed.get("occupancy")
        if occ is not None and rebooted and occ and self.occupied:
            # «занято» до перезагрузки и «занято» после — это новое событие: отсчёт PIR stuck
            # заново с этого пакета, а не с момента до выключения (2026-11-15)
            self.stats["motion_events"] += 1
            self.occupied_since = now
            _LOGGER.info("%s: reboot while occupied — occupied timer restarted", self.name)
        elif occ is not None and occ != self.occupied:
            if occ:
                self.stats["motion_events"] += 1
                self.occupied_since = now
            else:
                self.occupied_since = None
            self.occupied = occ
        # история батареи: не чаще раза в 30 мин; рост > 5 % — зарядили, история заново
        pct = parsed.get("battery")
        if pct is not None:
            ts = now.timestamp()
            if self.batt_hist and pct > self.batt_hist[-1][1] + 5:
                self.batt_hist = []
            if not self.batt_hist or ts - self.batt_hist[-1][0] >= 1800:
                self.batt_hist.append([round(ts), pct])
                self.batt_hist = self.batt_hist[-1500:]   # ~30 дней

    @property
    def silent_wakes(self) -> int:
        return max(0, self.stats.get("wakes", 0) - self.stats.get("tx_wakes", 0))

    def battery_forecast(self) -> dict:
        """Прогноз по тренду % с последней зарядки (МНК). Нужно >= 1 сутки истории и падение >= 2 %."""
        h = self.batt_hist
        info = {"samples": len(h), "since": dt_util.utc_from_timestamp(h[0][0]).isoformat() if h else None}
        if len(h) < 6:
            return {**info, "days_left": None, "rate_pct_per_day": None, "note": "собираю данные"}
        t0 = h[0][0]
        xs = [(t - t0) / 86400 for t, _ in h]
        ys = [p for _, p in h]
        span = xs[-1] - xs[0]
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        den = sum((x - mx) ** 2 for x in xs)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0
        info["rate_pct_per_day"] = round(slope, 2)
        if span < 1.0 or (ys[0] - ys[-1]) < 2:
            return {**info, "days_left": None, "note": f"мало данных: {span:.1f} сут, падение {ys[0] - ys[-1]} %"}
        if slope >= -0.05:
            return {**info, "days_left": None, "note": "разряд не заметен"}
        return {**info, "days_left": round(ys[-1] / -slope, 1), "note": None}

    @property
    def pir_stuck(self) -> bool:
        if not self.occupied or self.occupied_since is None:
            return False
        return (dt_util.utcnow() - self.occupied_since).total_seconds() >= self.stuck_threshold_min * 60

    async def async_set_stuck_threshold(self, minutes: int) -> None:
        self.stuck_threshold_min = int(minutes)
        self._save()
        self._notify()

    @callback
    def _tick(self, _now=None) -> None:
        before = self.stats.get("date")
        self._roll_day()
        if self.test_mode.get("state") == "active" and not self.test_mode_on:
            self.test_mode.update(state="off", want=False)
            self._save()
            self._notify()
        self._check_missed_climate()
        sst = self.settings_status
        if sst != self._last_sst:
            # applied -> idle через SETTINGS_APPLIED_SHOW_MIN: обновить сенсор
            self._last_sst = sst
            self._notify()
        stuck = self.pir_stuck
        if stuck != getattr(self, "_stuck_reported", None) or before != self.stats.get("date"):
            if stuck and not getattr(self, "_stuck_reported", False):
                _LOGGER.warning("%s: PIR stuck — «занято» %s мин", self.name, self.stuck_threshold_min)
            self._stuck_reported = stuck
            self._save()
            self._notify()

    def _check_missed_climate(self) -> None:
        """Нет пакета дольше 1,8 x Climate report interval (+1 мин) при «пусто» — климат-пакет потерян:
        окно рекламы на сутки на уровень длиннее (если авто)."""
        ci = self.value("climate_interval")
        if not ci or self.last_seen is None or self.occupied or self.test_mode_on:
            return
        key = self.last_seen.isoformat()
        if key == self._missed_for:
            return
        from datetime import timedelta  # noqa: PLC0415

        if dt_util.utcnow() - self.last_seen > timedelta(minutes=float(ci) * 1.8 + 1):
            self._missed_for = key
            self.missed_climate += 1
            self.window_boost_until = dt_util.utcnow() + timedelta(hours=AUTO_WINDOW_BOOST_H)
            _LOGGER.warning("%s: пропущен климат-пакет (нет пакетов %s мин) — окно длиннее на сутки",
                            self.name, round((dt_util.utcnow() - self.last_seen).total_seconds() / 60))
            self._save()
            self._notify()
            self._auto_window()

    def _ensure_worker(self) -> None:
        if not self._has_work():
            return
        if self._worker is None or self._worker.done():
            self._worker = self.hass.async_create_background_task(
                self._run_worker(), f"{self.entry.entry_id}_ble_window"
            )

    @callback
    def _on_adv(self, service_info: bluetooth.BluetoothServiceInfoBleak, change) -> None:
        self._adv_event.set()

    def _start_adv_watch(self) -> None:
        if self._unsub_adv is None:
            self._unsub_adv = bluetooth.async_register_callback(
                self.hass,
                self._on_adv,
                bluetooth.BluetoothCallbackMatcher(address=self.address, connectable=True),
                bluetooth.BluetoothScanningMode.ACTIVE,
            )

    def _stop_adv_watch(self) -> None:
        if self._unsub_adv is not None:
            self._unsub_adv()
            self._unsub_adv = None

    async def _run_worker(self) -> None:
        self._start_adv_watch()
        self.window_state = "waiting"
        self._notify()
        try:
            while self._has_work():
                timeout = None
                if self._ota is not None:
                    timeout = self._ota.deadline - time.monotonic()
                    if timeout <= 0:
                        self._ota = None
                        self._set(ST_ERROR, error="датчик не проснулся за отведённое время", finished=dt_util.now())
                        continue
                self._adv_event.clear()
                try:
                    await asyncio.wait_for(self._adv_event.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    continue
                if self._ota is not None:
                    self.attempts += 1
                try:
                    await self._session()
                except RetryLater as err:
                    _LOGGER.warning("%s: window missed: %s - waiting for next wake", self.name, err)
                    if self._ota is not None:
                        self._set(ST_WAITING, error=f"попытка {self.attempts}: {err}")
                finally:
                    self.window_state = "waiting"
                    self._notify()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("%s: BLE worker failed", self.name)
            if self._ota is not None:
                self._ota = None
                self._set(ST_ERROR, error=f"{type(err).__name__}: {err}", finished=dt_util.now())
        finally:
            self._stop_adv_watch()
            self.window_state = "idle"
            self._notify()

    async def _session(self) -> None:
        """Одно подключение в окно: авторизация -> настройки -> прошивка."""
        ble_device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
        if ble_device is None:
            raise RetryLater("нет подключаемого прокси, который слышит датчик")
        key = self._ota.key if self._ota is not None else None
        if key is None:
            try:
                key = await self._key()
            except OtaError as err:
                self.settings_error = str(err)
                self._notify()
                return
        if self._ota is not None:
            self._set(ST_CONNECTING)
        try:
            client = await establish_connection(BleakClientWithServiceCache, ble_device, self.name, max_attempts=1)
        except (BleakError, asyncio.TimeoutError, OSError) as err:
            raise RetryLater(f"не успели подключиться ({err})") from err
        self.window_state = "connected"
        self._notify()

        queue: asyncio.Queue[bytes] = asyncio.Queue()

        def on_notify(_char, data: bytearray) -> None:
            queue.put_nowait(bytes(data))

        async def wait_for(code: int, timeout: float) -> bytes:
            end = time.monotonic() + timeout
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    raise asyncio.TimeoutError
                msg = await asyncio.wait_for(queue.get(), timeout=left)
                if msg and msg[0] == code:
                    return msg

        async def command(payload: bytes, timeout: float = CMD_TIMEOUT) -> bytes:
            await client.write_gatt_char(CTRL_UUID, payload, response=True)
            return await wait_for(payload[0] | 0x80, timeout)

        flash_done = False
        try:
            await client.start_notify(CTRL_UUID, on_notify)
            hello = await command(bytes([CMD_HELLO]))
            nonce = hello[2:18]
            self.device_battery = hello[18] if len(hello) > 18 and hello[18] != 255 else None
            mac = hmac.new(key.encode(), nonce, hashlib.sha256).digest()[:16]
            auth = await command(bytes([CMD_AUTH]) + mac)
            if auth[1] != 0:
                msg = DEVICE_STATUS.get(auth[1], f"auth status {auth[1]}")
                self.settings_error = msg
                if self._ota is not None:
                    self._ota = None
                    self._set(ST_ERROR, error=msg, finished=dt_util.now())
                return
            info = await command(bytes([CMD_INFO]))
            ver = info[2:].decode(errors="replace")
            if self._ota is not None and self.status != ST_FLASHING:
                self.device_version = ver  # «было» для этой прошивки
            self.firmware_version = ver
            self.firmware_seen = dt_util.now()
            self._save()
            self._notify()
            # после успешной прошивки датчик перезагрузился и ответил — итог подтверждён:
            # сразу idle / 0 % (не ждать RESULT_HOLD_S); итог остаётся в атрибуте last_result
            # подтверждение — только после перезагрузки датчика (сброс Wake count) и с новой версией;
            # подключение в те ~1,5 с до перезагрузки ещё отдаёт старую версию — не считается
            if (self._ota is None and self.status == ST_SUCCESS and self._rebooted_after_flash
                    and ver != self.device_version):
                _LOGGER.info("%s: new firmware confirmed by device (%s)", self.name, ver)
                if self._unsub_reset is not None:
                    self._unsub_reset()
                    self._unsub_reset = None
                self._reset_result()

            # ---- диагностика сбросов (одна короткая команда, в каждом подключении)
            await self._read_diag(command)

            # ---- сброс Wi-Fi на сеть из прошивки (если заказан)
            if self.wifi_reset.get("state") == "pending":
                await self._send_wifi_reset(command)

            # ---- быстрый тест (вкл/выкл)
            if self.test_mode.get("state") == "pending":
                await self._send_test_mode(command)

            # ---- настройки (в каждом подключении, если есть что: прошлая ошибка не мешает —
            # датчик могли перепрошить)
            if self.pending() or self._need_read:
                await self._sync_settings(command)

            # ---- прошивка
            if self._ota is not None:
                try:
                    await self._flash(client, command, wait_for, self._ota.image)
                except OtaError as err:
                    _LOGGER.error("%s: OTA failed: %s", self.name, err)
                    self._ota = None
                    self._set(ST_ERROR, error=str(err), finished=dt_util.now())
                    return
                flash_done = True
                self._ota = None
                # при следующем пробуждении перечитать версию и настройки — видно, что встала новая сборка
                self._need_read = True
                self._rebooted_after_flash = False
                self._set(ST_SUCCESS, progress=100, finished=dt_util.now())
                _LOGGER.info("%s: OTA done, device reboots", self.name)
        except (BleakError, asyncio.TimeoutError, EOFError) as err:
            raise RetryLater(f"связь оборвалась ({type(err).__name__}: {err})") from err
        finally:
            if self._ota is not None and not flash_done:
                try:
                    if client.is_connected:
                        await client.write_gatt_char(CTRL_UUID, bytes([CMD_ABORT]), response=True)
                except Exception:  # noqa: BLE001
                    pass
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    @property
    def test_mode_on(self) -> bool:
        t = self.test_mode
        if not t.get("want"):
            return False
        if t.get("state") == "pending":
            return True
        until = dt_util.parse_datetime(t["until"]) if t.get("until") else None
        return t.get("state") == "active" and until is not None and until > dt_util.utcnow()

    async def async_set_test_mode(self, on: bool, minutes: int = TEST_MODE_MIN, tx_window_ms: int = 0) -> None:
        """Быстрый тест (только для отладки, без сущности в UI): просыпаться раз в 30 с.

        tx_window_ms — окно рекламы после пакета на время теста (0 = как в прошивке); старая
        прошивка лишние байты команды игнорирует."""
        self.test_mode = {"want": on, "minutes": minutes if on else 0, "state": "pending",
                          "tx_window_ms": tx_window_ms if on else 0,
                          "requested": dt_util.now().isoformat()}
        self._save()
        self._notify()
        self._ensure_worker()

    async def _send_test_mode(self, command) -> None:
        minutes = int(self.test_mode.get("minutes", TEST_MODE_MIN)) if self.test_mode.get("want") else 0
        try:
            tx_ms = int(self.test_mode.get("tx_window_ms") or 0) if minutes else 0
            rsp = await command(bytes([CMD_TEST_MODE, minutes]) + tx_ms.to_bytes(2, "little"), timeout=3.0)
        except asyncio.TimeoutError:
            return
        if rsp[1] == 0:
            now = dt_util.utcnow()
            from datetime import timedelta  # noqa: PLC0415
            self.test_mode.update(state="active" if minutes else "off",
                                  until=(now + timedelta(minutes=minutes)).isoformat() if minutes else None,
                                  error=None)
            _LOGGER.info("%s: fast test mode %s", self.name,
                         f"ON for {minutes} min, tx_window {tx_ms or 'default'} ms" if minutes else "OFF")
        else:
            msg = "прошивка без быстрого теста" if rsp[1] == 10 else DEVICE_STATUS.get(rsp[1], f"status {rsp[1]}")
            self.test_mode.update(state="error", error=msg, want=False)
        self._save()
        self._notify()

    async def async_request_wifi_reset(self) -> None:
        self.wifi_reset = {"state": "pending", "requested": dt_util.now().isoformat()}
        self._save()
        self._notify()
        self._ensure_worker()

    async def _send_wifi_reset(self, command) -> None:
        try:
            rsp = await command(bytes([CMD_WIFI_RESET]), timeout=3.0)
        except asyncio.TimeoutError:
            return  # повтор в следующее окно
        if rsp[1] == 0:
            self.wifi_reset.update(state="sent", done=dt_util.now().isoformat(), error=None)
            _LOGGER.info("%s: Wi-Fi reset accepted by device", self.name)
        else:
            msg = "прошивка без команды сброса Wi-Fi" if rsp[1] == 10 else DEVICE_STATUS.get(rsp[1], f"status {rsp[1]}")
            self.wifi_reset.update(state="error", error=msg)
        self._save()
        self._notify()

    async def _read_diag(self, command) -> None:
        try:
            rsp = await command(bytes([CMD_DIAG]), timeout=3.0)
        except asyncio.TimeoutError:
            # не повторяем на каждом пробуждении — прочитаем при следующей перезагрузке/подключении
            _LOGGER.debug("%s: DIAG timeout", self.name)
            self._need_diag = False
            return
        if rsp[1] == 10:  # неизвестная команда — прошивка до 2026-11-14
            self.diag_unsupported = True
            self._need_diag = False
            return
        if rsp[1] != 0 or len(rsp) < 3 + 2 * len(DIAG_FIELDS):
            _LOGGER.warning("%s: DIAG status %d (%d bytes)", self.name, rsp[1], len(rsp))
            self._need_diag = False
            return
        vals = struct.unpack_from("<" + "H" * len(DIAG_FIELDS), rsp, 3)
        diag = {"last_reason": rsp[2], **dict(zip(DIAG_FIELDS, vals))}
        if self.diag and diag.get("brownout", 0) > self.diag.get("brownout", 0):
            _LOGGER.warning("%s: brownout reset (total %d)", self.name, diag["brownout"])
        self.diag = diag
        self.diag_seen = dt_util.now()
        self.diag_unsupported = False
        self._need_diag = False
        self._save()
        self._notify()
        _LOGGER.info("%s: reset diagnostics: %s", self.name, diag)

    async def _sync_settings(self, command) -> None:
        pend = self.pending()
        if pend:
            mask = 0
            for k in pend:
                mask |= SETTINGS_MASK[k]
            rsp = await command(bytes([CMD_SET_SETTINGS, mask]) + pack_settings(pend))
        else:
            rsp = await command(bytes([CMD_GET_SETTINGS]))
        if rsp[1] != 0:
            self.settings_error = DEVICE_STATUS.get(rsp[1], f"settings status {rsp[1]}")
            self._notify()
            return
        actual = unpack_settings(rsp[2:])
        self.applied = actual
        self._need_read = False
        notes = []
        for k, want in list(pend.items()):
            got = actual.get(k)
            if not same_value(k, want, got):
                # датчик не принял значение (вне min/max прошивки) — показываем фактическое
                notes.append(f"{k}: задано {want}, датчик оставил {got}")
            self.desired.pop(k, None)
        # заданное, совпавшее с фактическим, больше не ждёт
        for k in [k for k, v in self.desired.items() if same_value(k, v, actual.get(k))]:
            self.desired.pop(k, None)
        self.settings_note = "; ".join(notes) or None
        self.settings_error = None
        self.settings_synced = dt_util.now()
        if pend:
            self.settings_applied_at = self.settings_synced
        self._save()
        self._notify()
        _LOGGER.info("%s: settings %s: %s%s", self.name, "applied" if pend else "read", actual,
                     f" ({self.settings_note})" if self.settings_note else "")

    async def _flash(self, client, command, wait_for, image: FirmwareImage) -> None:
        size = len(image.data)
        begin = await command(bytes([CMD_BEGIN]) + struct.pack("<II", size, image.crc), timeout=10)
        if begin[1] != 0:
            raise OtaError(DEVICE_STATUS.get(begin[1], f"begin status {begin[1]}"))
        block = struct.unpack_from("<H", begin, 2)[0] or 4096
        mtu = getattr(client, "mtu_size", 23) or 23
        # <= 240 байт данных: ATT-пакет целиком влезает в один радиокадр (DLE 251), без сборки
        # на стороне датчика; каждый CHUNKS_PER_SYNC-й пакет — с подтверждением (backpressure),
        # иначе поток через прокси переполняет буферы NimBLE и датчик рвёт связь (0x13).
        chunk = max(16, min(mtu - 3 - 4, MAX_CHUNK))
        _LOGGER.info(
            "%s: flashing %d bytes (device %s -> %s, battery %s%%, mtu %d, chunk %d)",
            self.name, size, self.device_version, image.version, self.device_battery, mtu, chunk,
        )
        self._set(ST_FLASHING, progress=0)
        t0 = time.monotonic()
        offset = 0
        last_pct = -1
        while offset < size:
            block_end = min((offset // block + 1) * block, size)
            retries = 0
            while True:
                pos = offset
                k = 0
                while pos < block_end:
                    n = min(chunk, block_end - pos)
                    k += 1
                    await client.write_gatt_char(
                        DATA_UUID,
                        struct.pack("<I", pos) + image.data[pos : pos + n],
                        response=(k % CHUNKS_PER_SYNC == 0),
                    )
                    pos += n
                try:
                    ack = await wait_for(RSP_ACK, ACK_TIMEOUT)
                    if ack[1] != 0:
                        raise OtaError(DEVICE_STATUS.get(ack[1], f"write status {ack[1]}"))
                    written = struct.unpack_from("<I", ack, 2)[0]
                    offset = written
                    if written >= block_end:
                        break
                except asyncio.TimeoutError:
                    retries += 1
                    if retries > BLOCK_RETRIES:
                        raise RetryLater(f"нет подтверждения блока на {offset}")
                    st = await command(bytes([CMD_STATUS]))
                    received = struct.unpack_from("<I", st, 2)[0]
                    _LOGGER.debug("%s: ack timeout at %d, device has %d", self.name, offset, received)
                    if received >= block_end:
                        ack = await wait_for(RSP_ACK, ACK_TIMEOUT)
                        if ack[1] != 0:
                            raise OtaError(DEVICE_STATUS.get(ack[1], f"write status {ack[1]}"))
                        offset = struct.unpack_from("<I", ack, 2)[0]
                        break
                    offset = max(received, (offset // block) * block)
            pct = int(offset * 100 / size)
            if pct != last_pct:
                last_pct = pct
                dt = time.monotonic() - t0
                self._set(progress=pct, speed_kbs=round(offset / 1024 / dt, 1) if dt > 0 else None)

        end = await command(bytes([CMD_END]), timeout=15)
        if end[1] != 0:
            raise OtaError(DEVICE_STATUS.get(end[1], f"end status {end[1]}"))
        dt = time.monotonic() - t0
        self.speed_kbs = round(size / 1024 / dt, 1) if dt > 0 else None
        _LOGGER.info("%s: %d bytes in %.0f s (%.1f KB/s)", self.name, size, dt, self.speed_kbs or 0)
