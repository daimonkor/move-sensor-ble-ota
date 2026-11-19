"""Работа с датчиком через окно пробуждения: настройки и прошивка по BLE.

Один воркер на датчик. Пока есть работа (несохранённые настройки, первое чтение
настроек или прошивка), он ждёт рекламу датчика, подключается через Bluetooth-прокси
в окно пробуждения и за одно подключение: авторизация -> настройки -> прошивка.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import shutil
import struct
import time
import zlib
from dataclasses import dataclass
from datetime import datetime

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth, persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import fwnotes
from .const import (
    CMD_ABORT,
    CONF_SILENT_AFTER,
    CMD_AUTH,
    CMD_BEGIN,
    CMD_END,
    CMD_GET_SETTINGS,
    CMD_GET_BATTERY,
    CMD_SET_BATTERY,
    BATTERY_KEYS,
    CMD_HELLO,
    CMD_INFO,
    CMD_DIAG,
    CMD_WIFI_RESET,
    CMD_TEST_MODE,
    CMD_IDENTIFY,
    IDENTIFY_SECONDS,
    device_id,
    TEST_MODE_MIN,
    DIAG_FIELDS,
    RESET_REASONS,
    BTHOME_UUID_FULL,
    BTHOME_WAKE_COUNT_ID,
    CMD_SET_SETTINGS,
    CMD_STATUS,
    CONF_ADDRESS,
    CONF_FIRMWARE_DIR,
    CONF_KEEP_IMAGES,
    CONF_KEY,
    CTRL_UUID,
    DATA_UUID,
    DEFAULT_FIRMWARE_DIR,
    DEFAULT_KEEP_IMAGES,
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
    DEVICE_ESPHOME_CONFIG,
    ESPHOME_DIR,
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
# v1.19: батарея — своя команда (0x15/0x16) и своя маска; старый пакет настроек (20 байт) не меняется
BATTERY_MASK = {"battery_capacity": 0x01, "battery_low": 0x02}
SETTING_KEYS = set(SETTINGS_MASK) | set(BATTERY_MASK)


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
            out["wake"] = int.from_bytes(data[i + 1:i + 3], "little") & WAKE_MASK
        elif oid == 0x01:
            out["battery"] = data[i + 1]
        elif oid == 0x00:
            out["packet_id"] = data[i + 1]
        elif oid == 0x23:
            out["occupancy"] = bool(data[i + 1])
        elif oid == 0x0C:
            out["voltage"] = int.from_bytes(data[i + 1:i + 3], "little") / 1000
        elif oid == 0x02:
            out["temperature"] = int.from_bytes(data[i + 1:i + 3], "little", signed=True) / 100
        elif oid == 0x03:
            out["humidity"] = int.from_bytes(data[i + 1:i + 3], "little") / 100
        i += 1 + n
    return out


# Свой блок рекламы (прошивка с 2026-11-16, v1.14.2): Manufacturer Specific Data, company 0xFFFF,
# после данных BTHome. Данные: wake u16 LE, settings_rev u8 (0 = нет, появится в 1.15),
# motion_count u8 (свежие срабатывания PIR, по кругу). BTHome этот блок не видит.
CUSTOM_COMPANY_ID = 0xFFFF
# 2026-11-19 (прошивка 1.7.0): два старших бита поля wake — флаги, сам счётчик 14 бит (по кругу):
# bit15 — сервисный режим (Wi-Fi включён), bit14 — подключён к роутеру (к любой сети).
# Нужны, когда HA не достаёт датчик по Wi-Fi (другая сеть / только своя точка): по одному
# Bluetooth видно, что сервисный режим включён.
WAKE_MASK = 0x3FFF
FLAG_SERVICE = 0x8000
FLAG_ROUTER = 0x4000
SERVICE_FLAG_FRESH_S = 30.0   # пакет с флагами в сервисном режиме — раз в 5 с
OWN_FLASH_WINDOW_S = 900.0    # 15 мин после своей прошивки новая сборка на датчике — своя


def custom_parse(manufacturer_data: dict) -> dict:
    data = (manufacturer_data or {}).get(CUSTOM_COMPANY_ID)
    if not data or len(data) < 4:
        return {}
    raw = int.from_bytes(data[0:2], "little")
    return {"wake": raw & WAKE_MASK, "settings_rev": data[2], "motion_count": data[3],
            "service": bool(raw & FLAG_SERVICE), "router": bool(raw & FLAG_ROUTER)}


# v1.15: данные датчика, которые показывает основное устройство (из любого канала)
DATA_KEYS = ("occupancy", "temperature", "humidity", "battery", "voltage")
ISSUE_EMPTY_V = 3.55   # v1.20: молчит и последнее напряжение ниже этого — «батарея села», а не «пропал»
WIFI_ECHO_S = 5.0   # сколько ждём эхо своей записи по Wi-Fi


# v1.15: «YAML изменён» — хеш всего дерева, из которого собирается прошивка датчика
_CFG_EXT = (".yaml", ".yml", ".h", ".hpp", ".c", ".cpp", ".py", ".js", ".css", ".html")


def _config_tree_files(esphome_dir: str, device_yaml: str) -> list[str]:
    """Файлы сборки: YAML датчика + move_sensor_ble/ + components/bthome/ + move_sensor_rtc.h + secrets.yaml.
    Копии (.bak*), кэши, этикетки и папка прошивок не входят. Блокирующая."""
    files = [os.path.join(esphome_dir, device_yaml), os.path.join(esphome_dir, "move_sensor_rtc.h"),
             os.path.join(esphome_dir, "secrets.yaml")]
    for sub in ("move_sensor_ble", os.path.join("components", "bthome")):
        root = os.path.join(esphome_dir, sub)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in ("__pycache__", "labels") and not d.startswith("."))
            for fn in sorted(filenames):
                if ".bak" in fn or ".old_" in fn or fn.startswith("."):
                    continue
                if fn.lower().endswith(_CFG_EXT):
                    files.append(os.path.join(dirpath, fn))
    return files


def config_tree_hash(esphome_dir: str, device_yaml: str) -> str:
    """Хеш файлов сборки (блокирующая)."""
    h = hashlib.sha256()
    for path in _config_tree_files(esphome_dir, device_yaml):
        try:
            with open(path, "rb") as f:
                h.update(os.path.relpath(path, esphome_dir).encode())
                h.update(f.read())
        except OSError:
            continue
    return h.hexdigest()[:12]


def fw_fingerprint(esphome_dir: str, device_yaml: str) -> str:
    """2026-11-19: отпечаток файлов прошивки — ТОТ ЖЕ, что считает components/bthome/__init__.py
    при сборке (_tree_fingerprint) и прошивка отдаёт в версии («1.7.0 t1a2b3c4d v…»). Список и
    формат должны совпадать. Блокирующая."""
    rel = [device_yaml, "move_sensor_rtc.h"]
    msb = os.path.join(esphome_dir, "move_sensor_ble")
    if os.path.isdir(msb):
        rel += sorted("move_sensor_ble/" + f for f in os.listdir(msb)
                      if f.endswith(".yaml") and ".bak" not in f and not f.startswith("."))
    rel.append("move_sensor_ble/site_bundle.js")
    bt = os.path.join(esphome_dir, "components", "bthome")
    if os.path.isdir(bt):
        rel += sorted("components/bthome/" + f for f in os.listdir(bt)
                      if f.endswith((".py", ".h", ".cpp")) and ".bak" not in f and not f.startswith("."))
    h = hashlib.sha256()
    for r in rel:
        path = os.path.join(esphome_dir, r)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            h.update(r.encode() + b"\0" + fh.read())
    return h.hexdigest()[:8]


def version_fingerprint(version: str | None) -> str | None:
    """Отпечаток из версии прошивки («1.7.0 t1a2b3c4d v…» -> «1a2b3c4d»); у старых прошивок нет."""
    import re  # noqa: PLC0415

    m = re.search(r"(?:^|\s)t([0-9a-f]{8})(?:\s|$)", version or "")
    return m.group(1) if m else None


def config_tree_mtime(esphome_dir: str, device_yaml: str) -> float:
    """Время последней правки файлов сборки, epoch (блокирующая)."""
    m = 0.0
    for path in _config_tree_files(esphome_dir, device_yaml):
        try:
            m = max(m, os.path.getmtime(path))
        except OSError:
            continue
    return m


def ota_status_text(status: str | None, progress: int = 0, error: str | None = None,
                    last_build: dict | None = None) -> str:
    """v1.18.1: этап сборки/прошивки одной строкой для панели (имена сущностей и статусы — по-английски).
    В покое — итог последней сборки («Idle · built 12:06 · 1.2 MB»): сборка из кэша идёт секунды,
    и без этого кнопку «Build» не видно по панели."""
    if status == ST_BUILDING:
        return f"Building {progress} %"
    if status == ST_WAITING:
        return "Waiting for sensor wake (motion wakes it)"
    if status == ST_CONNECTING:
        return "Connecting"
    if status == ST_FLASHING:
        return f"Flashing {progress} %"
    if status == ST_SUCCESS:
        return "Success"
    if status == ST_CANCELLED:
        return "Cancelled"
    if status == ST_ERROR:
        return f"Error: {error}"[:200] if error else "Error"
    lb = last_build or {}
    if lb.get("built"):
        try:
            t = dt_util.as_local(dt_util.parse_datetime(lb["built"])).strftime("%H:%M")
        except (TypeError, ValueError, AttributeError):
            return "Idle"
        size = f" · {lb['size'] / 1048576:.1f} MB" if lb.get("size") else ""
        return f"Idle · built {t}{size}"
    return "Idle"


def fw_semver(version: str | None) -> str | None:
    """v1.17.0: версия прошивки из строки INFO датчика («1.4.0 v2026…» -> «1.4.0»); старые — None."""
    import re  # noqa: PLC0415

    m = re.match(r"\s*(\d+\.\d+\.\d+)(?:\s|$)", version or "")
    return m.group(1) if m else None


def build_time(version: str | None) -> float | None:
    """Время сборки из версии прошивки vYYYY.MM.DD.HHMMSS-<sha> (местное время HA), epoch."""
    import re  # noqa: PLC0415

    # v1.17.0: прошивка 1.4.0+ отдаёт «1.4.0 v2026.11.16.171328-<sha>» — метка сборки после версии
    m = re.search(r"(?:^|\s)v?(\d{4})\.(\d{2})\.(\d{2})\.(\d{2})(\d{2})(\d{2})", version or "")
    if not m:
        return None
    y, mo, d, hh, mi, ss = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d, hh, mi, ss, tzinfo=dt_util.DEFAULT_TIME_ZONE).timestamp()
    except ValueError:
        return None


def read_tx_power(esphome_dir: str) -> int | None:
    """Мощность BLE-рекламы из YAML прошивки (bthome: tx_power, дБм). Блокирующая."""
    import re  # noqa: PLC0415

    try:
        with open(os.path.join(esphome_dir, "move_sensor_ble", "base.yaml"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    m = re.search(r"^bthome:\n(?:[ \t].*\n|\n)*?[ \t]+tx_power:\s*(-?\d+)", text, re.M)
    return int(m.group(1)) if m else None


class OtaError(Exception):
    """Ошибка, после которой повторять в следующее окно бессмысленно."""


class OTAKeyMismatch(OtaError):
    """Ключ OTA в образе не равен ключу интеграции."""


class RetryLater(Exception):
    """Окно закрылось/связь оборвалась — ждём следующее пробуждение."""


# v1.14.1: ключ не подошёл (или ключа нет) — подключения к датчику на паузе, пока ключ тот же.
# Ключ проверяем заново раз в AUTH_KEY_CHECK_S (поле «Ключ OTA» может быть пустым — тогда
# действующий ключ из secrets.yaml), а раз в AUTH_RETRY_H часов пробуем ещё раз и с тем же.
AUTH_KEY_CHECK_S = 600
AUTH_RETRY_H = 6


@dataclass
class FirmwareImage:
    path: str
    data: bytes
    crc: int
    project: str
    version: str
    from_factory: bool
    elf_sha: str | None = None   # v1.15.3: первые 4 байта app_elf_sha256 — хвост версии на датчике


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
    elf_sha = desc[144:148].hex() if len(desc) >= 148 and desc[0:4] == b"\x32\x54\xcd\xab" else None
    return FirmwareImage(path, data, zlib.crc32(data) & 0xFFFFFFFF, project, version, from_factory, elf_sha)


def prune_firmware(directory: str, keep: int, protect: set[str] | None = None) -> list[str]:
    """v1.16.2: оставить в папке прошивок `keep` самых свежих .bin (по времени изменения), остальные удалить.
    Файлы из protect (прошивается, ждёт окна, последняя сборка) не удаляются никогда. Блокирующая."""
    keep = max(1, int(keep))
    prot = {os.path.realpath(p) for p in (protect or set()) if p}
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return []
    now = time.time()
    bins, removed = [], []
    for n in names:
        p = os.path.join(directory, n)
        if not os.path.isfile(p) or os.path.islink(p):
            continue
        low = n.lower()
        if low.endswith(".bin"):
            bins.append((os.path.getmtime(p), p))
        elif low.endswith(".bin.part") and now - os.path.getmtime(p) > 3600:   # недописанные
            try:
                os.remove(p)
                removed.append(p)
            except OSError:
                pass
    bins.sort(reverse=True)
    for _, p in bins[keep:]:
        if os.path.realpath(p) in prot:
            continue
        try:
            os.remove(p)
            removed.append(p)
        except OSError as err:
            _LOGGER.warning("cannot remove old firmware %s: %s", p, err)
    return removed


def firmware_stats(directory: str) -> tuple[int, int]:
    """(сколько .bin, сколько байт) в папке прошивок. Блокирующая."""
    n = size = 0
    try:
        for e in os.scandir(directory):
            if e.is_file() and e.name.lower().endswith(".bin"):
                n += 1
                size += e.stat().st_size
    except FileNotFoundError:
        pass
    return n, size


BACKUP_SUBDIR = "backup"   # v1.18: <папка прошивок>/backup/<время>/ — образ + meta.json + копия YAML
BACKUP_KEEP = 3
BACKUP_IMAGE = "firmware.ota.bin"


def image_sha_of(path: str) -> str | None:
    """elf_sha образа (хвост версии на датчике) или None. Блокирующая."""
    try:
        return load_firmware(path).elf_sha
    except Exception:  # noqa: BLE001 — битый/чужой .bin просто пропускаем
        return None


def list_backups(backup_dir: str) -> list[dict]:
    """Резервные копии прошивки, свежие первыми: meta.json + путь. Блокирующая."""
    out = []
    try:
        names = os.listdir(backup_dir)
    except FileNotFoundError:
        return []
    for n in names:
        d = os.path.join(backup_dir, n)
        img = os.path.join(d, BACKUP_IMAGE)
        if not os.path.isfile(img):
            continue
        try:
            with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = {}
        out.append({**meta, "dir": d, "image": img, "name": n})
    out.sort(key=lambda m: m.get("created") or 0, reverse=True)
    return out


def make_backup(firmware_dir: str, backup_dir: str, sha: str, meta: dict, snap_src: str | None) -> dict | None:
    """Сохранить образ с этим elf_sha (тот, что сейчас на датчике) в backup/<время>/.
    Уже есть такая копия — вернуть её. Образа нет в папке прошивок — None. Блокирующая."""
    for b in list_backups(backup_dir):
        if b.get("sha") == sha:
            return b
    src = None
    try:
        cands = sorted((os.path.join(firmware_dir, n) for n in os.listdir(firmware_dir)
                        if n.lower().endswith(".bin") and os.path.isfile(os.path.join(firmware_dir, n))),
                       key=os.path.getmtime, reverse=True)
    except FileNotFoundError:
        cands = []
    for c in cands:
        if image_sha_of(c) == sha:
            src = c
            break
    if src is None:
        return None
    name = time.strftime("%Y%m%d-%H%M%S") + "-" + sha
    dest = os.path.join(backup_dir, name)
    tmp = dest + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    shutil.copy2(src, os.path.join(tmp, BACKUP_IMAGE))
    if snap_src and os.path.isdir(snap_src):
        shutil.copytree(snap_src, os.path.join(tmp, "snapshot"))
    meta = {**meta, "sha": sha, "source": os.path.basename(src), "created": time.time()}
    with open(os.path.join(tmp, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    os.replace(tmp, dest)
    for old in list_backups(backup_dir)[BACKUP_KEEP:]:
        shutil.rmtree(old["dir"], ignore_errors=True)
    return {**meta, "dir": dest, "image": os.path.join(dest, BACKUP_IMAGE), "name": name}


def restore_snapshot(backup: dict, snap_root: str) -> None:
    """После отката: копия YAML из резервной копии -> прошитая (flashed). Блокирующая."""
    src = os.path.join(backup["dir"], "snapshot")
    dst = os.path.join(snap_root, "flashed")
    if not os.path.isdir(src):
        shutil.rmtree(dst, ignore_errors=True)
        return
    tmp = dst + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src, tmp)
    shutil.rmtree(dst, ignore_errors=True)
    os.replace(tmp, dst)


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


def read_secret(name: str) -> str | None:
    """v1.17.0: любой ключ из /config/esphome/secrets.yaml (блокирующая)."""
    try:
        with open(SECRETS_PATH, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith(name + ":"):
                    v = s.split(":", 1)[1].split("#", 1)[0].strip().strip("\"'")
                    return v or None
    except OSError:
        return None
    return None


OTA_PASSWORD_SECRET = "move_sensor_ota_password"   # пароль ESPHome OTA (Wi-Fi), прошивка 1.4.0+


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


def pack_battery(values: dict) -> bytes:
    """[маска, ёмкость u16 LE, порог u8] для CMD_SET_BATTERY; нет значения — 0xFFFF / 0xFF."""
    mask = 0
    for k in values:
        mask |= BATTERY_MASK.get(k, 0)
    cap = values.get("battery_capacity")
    low = values.get("battery_low")
    return bytes([mask]) + struct.pack(
        "<HB",
        0xFFFF if cap is None else max(0, min(0xFFFE, int(round(float(cap))))),
        0xFF if low is None else max(0, min(0xFE, int(round(float(low))))),
    )


def unpack_battery(rsp: bytes) -> tuple[dict, int | None]:
    """Ответ CMD_GET/SET_BATTERY: [cmd|0x80, статус, ёмкость u16, порог u8, номер правки]."""
    out: dict = {}
    if len(rsp) < 5:
        return out, None
    cap, low = struct.unpack_from("<HB", rsp, 2)
    if cap != 0xFFFF:
        out["battery_capacity"] = cap
    if low != 0xFF:
        out["battery_low"] = low
    return out, (rsp[5] if len(rsp) >= 6 else None)


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
        self.battery_unsupported = False   # v1.19: прошивка не знает команду батареи (до 1.6.0)
        self.applied: dict = {}   # что датчик подтвердил последним
        # v1.14.0: что должно стоять на датчике (последнее подтверждённое заданное из HA).
        # После перезагрузки датчика (прошивка по USB, стёртый флеш, смена прошивки) его
        # настройки могут вернуться к значениям из YAML — тогда HA отправит их заново.
        self.target: dict = {}
        self._resync = False      # перечитать и вернуть target после перезагрузки датчика
        # v1.15: номер правки настроек на датчике (растёт на каждую правку с любой стороны; 0 = свежий NVS)
        self.rev_seen: int | None = None   # последний виденный номер
        self.rev_own: int | None = None    # номер после последней записи из ЭТОГО HA
        self.rev_capable = False           # прошивка отдаёт номер правки (18-й байт ответа BLE)
        self._adopt = False                # прочитать настройки и ПРИНЯТЬ их (правка не из HA)
        # v1.15: данные датчика независимо от канала: ключ -> {"v": значение, "ts": iso, "src": "ble"|"wifi"}
        self.data: dict = {}
        self.ble_rssi: int | None = None
        self.ble_service: tuple[bool, bool, float] | None = None  # (service, router, monotonic)
        self._svc_unsub = None   # таймер устаревания флага
        self.wifi = None                   # WifiChannel (wifi.py), если есть ESPHome-устройство
        self._wifi_echo: dict = {}         # ключ -> (значение, monotonic срок) — своя запись по Wi-Fi
        self._wifi_own_until = 0.0         # до этого момента рост номера правки — от своей записи по Wi-Fi
        self._restore_keys: set = set()    # ключи, возвращаемые после сброса (событие restored уже было)
        self._wifi_ota_after = 0.0         # после обрыва Wi-Fi OTA не пробовать Wi-Fi до этого момента
        self._shutdown_job: dict | None = None   # задание прошивки на момент выгрузки (сохраняется)
        self.key_ok_at: datetime | None = None   # когда датчик последний раз принял ключ OTA
        self.tx_power_dbm: int | None = None     # мощность BLE-рекламы из YAML прошивки (bthome: tx_power)
        self._build_cancelled = False      # «Отмена» во время сборки — собранное не шить
        self.last_channel: str | None = None   # канал последней прошивки (BLE / Wi-Fi)
        # v1.15.3: итог последней прошивки: at, channel, seconds (сама прошивка, без ожидания окна),
        # speed_kbs, bytes, result (flashed / already_on_sensor)
        self.last_flash: dict | None = None
        self._flash_t0: float | None = None
        self._wifi_lock = asyncio.Lock()   # запись настроек по Wi-Fi и сеанс BLE не идут одновременно
        # v1.14.1: {"reason": "auth"|"no_key", "key_fp": ..., "since": iso} — подключения на паузе
        self.auth_block: dict | None = None
        self._auth_check_at = 0.0
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
        self.identify: dict = {}            # {"state": pending|sent|error, ...} — не сохраняется
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
        # --- свежие срабатывания PIR (прошивка со своим блоком, v1.14.2): Last motion
        self.last_motion: datetime | None = None
        self.motion_count: int | None = None      # последний виденный счётчик из своего блока
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
        # v1.15: сущность «Firmware»: хеш дерева YAML сейчас и того, что стоит на датчике
        self.config_hash: str | None = None
        self.config_mtime: float | None = None   # v1.15.4: когда последний раз правили файлы прошивки
        self.flashed_hash: str | None = None
        self._own_flash_at: float = -1e9   # monotonic: последняя прошивка самой интеграцией
        self._build_hash: str | None = None
        self._ticks = 0
        # v1.16: копии файлов сборки (build / flashed) — для release notes прошивки
        self.snap_root = hass.config.path(".storage", DOMAIN, entry.entry_id)
        self.fw_changes: dict | None = None        # diff с прошитой копией
        self.fw_changed_since: list | None = None  # без копии: файлы новее сборки на датчике
        self._changes_key: tuple | None = None
        self.fw_images: tuple[int, int] | None = None   # v1.16.2: (образов .bin, байт) в папке прошивок
        self.fw_sections: list[dict] = []          # v1.16.2: новые разделы CHANGELOG прошивки
        self.fw_version_flashed: str | None = None # версия из CHANGELOG прошитой копии
        self.fw_version_new: str | None = None     # версия из текущего CHANGELOG
        self.fw_current_section: list[dict] = []   # верхний раздел CHANGELOG прошитой копии
        self.backups: list[dict] = []              # v1.18: резервные копии прошивки (свежие первыми)
        self._rollback: dict | None = None         # v1.18: идёт откат на эту копию

    # ================= загрузка / сохранение =================
    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.desired = data.get("desired", {})
        self.firmware_version = data.get("firmware_version")
        ts_fw = data.get("firmware_seen")
        self.firmware_seen = dt_util.parse_datetime(ts_fw) if ts_fw else None
        self.applied = data.get("applied", {})
        self.target = data.get("target") or dict(self.applied)   # миграция со старых версий
        self.flashed_hash = data.get("flashed_hash")
        self.last_flash = data.get("last_flash") if isinstance(data.get("last_flash"), dict) else None
        ts_k = data.get("key_ok_at")
        self.key_ok_at = dt_util.parse_datetime(ts_k) if ts_k else None
        rv = data.get("rev") or {}
        self.rev_seen = rv.get("seen")
        self.rev_own = rv.get("own")
        self.rev_capable = bool(rv.get("capable"))
        self.data = data.get("data") or {}
        self.auth_block = data.get("auth_block")
        if self.auth_block is not None:   # сообщение в «Ремонте» не переживает перезапуск HA
            self._raise_auth_issue(self.auth_block.get("reason", "auth"))
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
        ts_lm = data.get("last_motion")
        self.last_motion = dt_util.parse_datetime(ts_lm) if ts_lm else None
        self.motion_count = data.get("motion_count")
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
        # v1.15: канал Wi-Fi — ESPHome-устройство того же датчика
        from .wifi import WifiChannel  # noqa: PLC0415

        self.wifi = WifiChannel(self.hass, self.address, self._on_wifi_state, self._on_wifi_link)
        self.wifi.start()
        if self.wifi.live:
            self._on_wifi_link(True)
        self.hass.async_create_task(self.async_refresh_config_hash())
        if data.get("job"):
            self.hass.async_create_task(self._resume_job(data["job"]))
        self._ensure_worker()

    def _save(self) -> None:
        self._store.async_delay_save(
            lambda: {
                "desired": self.desired,
                "applied": self.applied,
                "target": self.target,
                "rev": {"seen": self.rev_seen, "own": self.rev_own, "capable": self.rev_capable},
                "flashed_hash": self.flashed_hash,
                "last_flash": self.last_flash,
                "key_ok_at": self.key_ok_at.isoformat() if self.key_ok_at else None,
                # v1.15: задание прошивки переживает перезапуск HA (файл + срок ожидания окна)
                "job": self._job_snapshot(),
                "data": self.data,
                "auth_block": self.auth_block,
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
                "last_motion": self.last_motion.isoformat() if self.last_motion else None,
                "motion_count": self.motion_count,
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
        return {k: v for k, v in self.desired.items()
                if not same_value(k, v, self.applied.get(k))
                and not (k in BATTERY_MASK and self.battery_unsupported)}

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
        # v1.15: только устойчивые состояния; что произошло — событие «Settings change» (event)
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
            "channel": self.link,
            "settings_revision": self.rev_seen,
        }

    async def async_set_setting(self, key: str, value) -> None:
        if not (self.wifi_live and self.wifi.has(key)):
            # v1.15: только BLE-правка снимает паузу из-за ключа; по Wi-Fi ключ не нужен (п.10 плана)
            self._auth_unblock("setting changed")
        if key not in SETTINGS:
            raise OtaError(f"неизвестная настройка {key}")
        if key in WINDOW_KEYS and self.auto_window:
            # ручное окно — авто выключаем, иначе следующий пакет перезапишет выбор
            self.auto_window = False
            _LOGGER.info("%s: %s задано вручную — авто-окно выключено", self.name, key)
        self._queue_setting(key, value)
        if self.wifi_live and self.wifi.has(key):
            await self._wifi_flush()
        else:
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
    def battery_supported(self) -> bool:
        """v1.19: прошивка 1.6.0+ отвечает на команду батареи (прочитано с датчика)."""
        return "battery_capacity" in self.applied

    def battery_remaining_mah(self) -> float | None:
        """Остаток, мА·ч = ёмкость × процент — так же, как li_battery на датчике (remaining)."""
        cap = self.applied.get("battery_capacity")
        pct = self.data_value("battery")
        if cap is None or pct is None:
            return None
        return round(float(cap) * float(pct) / 100.0)

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
        self._auth_unblock("Read settings")
        self._need_read = True
        self.settings_error = None
        self._notify()
        self._ensure_worker()

    # ================= публичное: прошивка =================
    @property
    def busy(self) -> bool:
        return self._ota is not None

    @property
    def keep_images(self) -> int:
        return int(self.entry.options.get(CONF_KEEP_IMAGES) or self.entry.data.get(CONF_KEEP_IMAGES)
                   or DEFAULT_KEEP_IMAGES)

    async def async_prune_firmware(self, extra: set[str] | None = None) -> None:
        """v1.16.2: в папке прошивок — только последние keep_images образов."""
        protect = {self.file, (self.last_build or {}).get("file"), *(extra or set())}
        try:
            removed = await self.hass.async_add_executor_job(
                prune_firmware, self.firmware_dir, self.keep_images, protect)
            self.fw_images = await self.hass.async_add_executor_job(firmware_stats, self.firmware_dir)
        except Exception as err:  # noqa: BLE001 — уборка не должна ломать сборку/прошивку
            _LOGGER.warning("%s: firmware folder cleanup failed: %s", self.name, err)
            return
        if removed:
            _LOGGER.info("%s: removed %d old firmware image(s), keep %d: %s", self.name, len(removed),
                         self.keep_images, ", ".join(os.path.basename(p) for p in removed))
        self._notify()

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
            "last_flash": self.last_flash,
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
            "device_id": self.device_id,
            "auth_error": self.auth_block,
        }

    async def _key(self) -> str:
        key = self.entry.options.get(CONF_KEY) or self.entry.data.get(CONF_KEY)
        if not key:
            key = await self.hass.async_add_executor_job(read_secret_key)
        if not key:
            raise OtaError(f"нет ключа: задай его в настройках или {SECRET_NAME} в {SECRETS_PATH}")
        return key

    # ================= v1.18: резервная копия и откат прошивки =================
    @property
    def backup_dir(self) -> str:
        return os.path.join(self.firmware_dir, BACKUP_SUBDIR)

    async def async_refresh_backups(self) -> None:
        b = await self.hass.async_add_executor_job(list_backups, self.backup_dir)
        if [x["name"] for x in b] != [x["name"] for x in self.backups]:
            self.backups = b
            self._notify()

    async def async_backup_current(self) -> dict:
        """Перед «Установить» с галочкой «Резервная копия»: сохранить образ, что сейчас на датчике
        (ищется в папке прошивок по хешу из версии датчика), и копию его YAML — для отката."""
        fv = self.firmware_version or ""
        sha = fv.rsplit("-", 1)[-1] if "-" in fv else ""
        if len(sha) != 8:
            raise OtaError("версия прошивки на датчике ещё не известна (дождись пробуждения) — "
                           "резервную копию сделать не из чего")
        meta = {"firmware_version": fv, "flashed_hash": self.flashed_hash, "fw_version": self.fw_version_flashed}
        snap = os.path.join(self.snap_root, "flashed")
        b = await self.hass.async_add_executor_job(
            make_backup, self.firmware_dir, self.backup_dir, sha, meta, snap)
        if b is None:
            raise OtaError(f"образа прошивки, что сейчас на датчике ({sha}), нет в {self.firmware_dir} "
                           "(прошит из ESPHome Builder или удалён уборкой) — резервную копию сделать нельзя. "
                           "Сними галочку «Резервная копия» или собери эту версию заново")
        _LOGGER.info("%s: firmware backup %s (%s)", self.name, b["name"], fv)
        await self.async_refresh_backups()
        return b

    async def async_rollback(self, timeout_min: int = 180) -> dict:
        """Прошить последнюю резервную копию (тем же путём: Wi-Fi в сервисном режиме, иначе BLE)."""
        await self.async_refresh_backups()
        if not self.backups:
            raise OtaError("резервных копий прошивки нет — их делает «Установить» с галочкой «Резервная копия»")
        b = self.backups[0]
        await self.async_start(b["image"], timeout_min, rollback=b)
        return b

    async def async_build_and_flash(
        self, configuration: str | None = None, timeout_min: int = 180, start: bool = True
    ) -> str:
        """Собрать YAML в аддоне ESPHome, положить firmware.ota.bin в папку прошивок и
        (start) прошить по BLE в ближайшее окно. Возвращает путь к .bin."""
        from .builder import BuildError, build_firmware  # noqa: PLC0415

        if self._ota is not None or self.status == ST_BUILDING:
            raise OtaError("сборка или прошивка уже идёт")
        await self.async_auth_gate("build and flash")
        cfg = await self.hass.async_add_executor_job(self.resolve_config, configuration)
        self._build_hash = await self.hass.async_add_executor_job(config_tree_hash, ESPHOME_DIR, cfg)
        await self._snapshot_build(cfg, self._build_hash)
        self.last_build = {"configuration": cfg, "started": dt_util.now().isoformat(), "config_hash": self._build_hash}
        self._build_cancelled = False
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
        # v1.15: ключ OTA сборки = ключ интеграции? Ключ лежит в образе открытым текстом. Не совпал —
        # датчик после прошивки не пустит интеграцию (настройки и следующие прошивки по BLE)
        try:
            key = await self._key()
        except OtaError:
            key = None
        if key and key.encode() not in data:
            msg = ("ключ OTA в собранной прошивке не совпадает с ключом интеграции "
                   "(поле «Ключ OTA» или move_sensor_ble_ota_key в secrets.yaml) — прошивка не запущена")
            self.last_build["error"] = msg
            self._set(ST_ERROR, error=msg, finished=dt_util.now())
            raise OtaError(msg)
        name = f"{os.path.splitext(os.path.basename(cfg))[0]}-{dt_util.now().strftime('%Y%m%d-%H%M%S')}.ota.bin"
        path = os.path.join(self.firmware_dir, name)

        def _write() -> None:
            os.makedirs(self.firmware_dir, exist_ok=True)
            with open(path + ".part", "wb") as f:
                f.write(data)
            os.replace(path + ".part", path)

        await self.hass.async_add_executor_job(_write)
        self.last_build.update({"file": path, "size": len(data), "built": dt_util.now().isoformat()})
        await self.async_prune_firmware(extra={path})
        _LOGGER.info("%s: built %s (%d bytes)", self.name, path, len(data))
        if self._build_cancelled:
            self._build_cancelled = False
            _LOGGER.info("%s: build finished after cancel — not flashing", self.name)
            return path
        if start:
            self.status = ST_IDLE  # async_start сам выставит waiting
            await self.async_start(path, timeout_min)
        else:
            self._set(ST_IDLE, progress=0)
        return path

    async def async_start(self, file: str | None = None, timeout_min: int = 180,
                          rollback: dict | None = None) -> None:
        await self.async_auth_gate("firmware flash")
        if self._ota is not None:
            raise OtaError("прошивка уже идёт или ждёт окно")
        self._rollback = rollback   # v1.18: откат — после прошивки вернуть хеш и копию YAML той версии
        path = file
        if path and not os.path.isabs(path):
            path = os.path.join(self.firmware_dir, path)
        if not path:
            path = await self.hass.async_add_executor_job(newest_firmware, self.firmware_dir)
            if not path:
                raise OtaError(f"в {self.firmware_dir} нет .bin файлов")
        image = await self.hass.async_add_executor_job(load_firmware, path)
        key = await self._key()
        # v1.15: ключ OTA образа = ключ интеграции (ключ лежит в образе открытым текстом). Иначе после
        # прошивки (особенно по Wi-Fi, где ключ не нужен) интеграция потеряет доступ к датчику по BLE
        if key and key.encode() not in image.data:
            raise OTAKeyMismatch(
                "ключ OTA в этом .bin не совпадает с ключом интеграции (поле «Ключ OTA» или "
                "move_sensor_ble_ota_key в secrets.yaml) — прошивка не запущена")
        self.file = path
        self.new_version = image.version
        # v1.15.3: эта сборка уже на датчике (тот же хеш образа в версии) — не прошиваем зря
        sha = getattr(image, "elf_sha", None)
        if sha and self.firmware_version and self.firmware_version.endswith("-" + sha):
            now = dt_util.now()
            if self._build_hash and self.last_build and path == self.last_build.get("file"):
                self.flashed_hash = self._build_hash
                self._promote_snapshot()
            self._apply_rollback()
            prev = self.last_flash if (self.last_flash or {}).get("result") == "flashed" else None
            if prev is not None:
                # v1.15.5: запись о настоящей прошивке остаётся, проверка — отдельным полем
                self.last_flash = {**prev, "same_build_checked_at": now.isoformat()}
            else:
                self.last_flash = {"at": now.isoformat(), "channel": None, "seconds": 0, "speed_kbs": None,
                                   "bytes": len(image.data), "result": "already_on_sensor"}
            self.error = None
            self._set(ST_SUCCESS, progress=100, started=now, finished=now)
            self._save()
            _LOGGER.info("%s: %s is already on the sensor — nothing to flash", self.name, sha)
            return
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
        """Отменить прошивку (настройки продолжат ждать окно). v1.15: и на этапе сборки."""
        self._shutdown_job = None
        self._rollback = None
        if self._ota is None and self.status == ST_BUILDING:
            self._build_cancelled = True
            self._set(ST_CANCELLED, finished=dt_util.now())
            _LOGGER.info("%s: build cancelled — the image will not be flashed", self.name)
            return
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
        if self._svc_unsub is not None:
            self._svc_unsub()
            self._svc_unsub = None
        self._shutdown_job = self._job_snapshot()   # v1.15: продолжить после перезапуска HA
        self._save()
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
        if self.wifi is not None:
            self.wifi.stop()

    # ================= внутреннее =================
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.entry.entry_id))

    def _set(self, status: str | None = None, **kw) -> None:
        if status is not None:
            if status == ST_FLASHING and self.status != ST_FLASHING:
                self._flash_t0 = time.monotonic()
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
        if self.wifi_live:
            return False   # v1.15: по Wi-Fi настройки пишутся/читаются сразу, без BLE-подключения
        return (bool(self.pending()) or self._need_read) and self.settings_error is None

    def _has_work(self) -> bool:
        if self.auth_block is not None:
            return False   # v1.14.1: ключ не подошёл — не подключаемся, пока он не изменится
        return (self._ota is not None or self._settings_work()
                or (self._need_diag and not self.diag_unsupported)
                or self.wifi_reset.get("state") == "pending"
                or self.identify.get("state") == "pending"
                or self.test_mode.get("state") == "pending")

    # ================= v1.15: сущность «Firmware» =================
    async def async_refresh_config_hash(self) -> None:
        try:
            cfg = await self.hass.async_add_executor_job(self.resolve_config, None)
            h = await self.hass.async_add_executor_job(config_tree_hash, ESPHOME_DIR, cfg)
        except Exception:  # noqa: BLE001
            return
        tx = await self.hass.async_add_executor_job(read_tx_power, ESPHOME_DIR)
        if tx != self.tx_power_dbm:
            self.tx_power_dbm = tx
            self._notify()
        if self.flashed_hash is None:
            # первый запуск (обновление с 1.14): на датчике текущий YAML, если файлы не новее сборки
            # на датчике; новее (обновили прошивку в /config/esphome) — предложить «Установить»
            # версию датчика ещё не знаем — решим, когда прочитаем её по BLE
            built = build_time(self.firmware_version)
            if built is not None:
                mtime = await self.hass.async_add_executor_job(config_tree_mtime, ESPHOME_DIR, cfg)
                self.flashed_hash = "older" if mtime > built + 120 else h
                self._save()
                self._notify()
        mtime = await self.hass.async_add_executor_job(config_tree_mtime, ESPHOME_DIR, cfg)
        changed = h != self.config_hash or mtime != self.config_mtime
        if await self._adopt_external_flash(cfg, h, mtime):
            changed = True
        imgs = await self.hass.async_add_executor_job(firmware_stats, self.firmware_dir)   # v1.16.2
        await self.async_refresh_backups()   # v1.18
        if imgs != self.fw_images:
            self.fw_images, changed = imgs, True
        self.config_hash = h
        self.config_mtime = mtime
        if await self._refresh_changes(cfg) or changed:
            self._notify()

    # ================= v1.17.0: ключ API для ESPHome-устройства датчика =================
    async def async_sync_api_key(self) -> None:
        if self.wifi is None:
            return
        from . import secrets_store  # noqa: PLC0415

        sv = fw_semver(self.firmware_version)
        encrypted = bool(sv) and tuple(int(x) for x in sv.split(".")) >= (1, 4, 0)
        try:
            await secrets_store.async_sync_api_key(self.hass, self.wifi.entry_id, self.wifi.mac, encrypted)
        except Exception as err:  # noqa: BLE001 — не мешать остальной работе
            _LOGGER.debug("%s: API key sync failed: %s", self.name, err)

    # ================= v1.16: release notes прошивки =================
    async def _snapshot_build(self, cfg: str, tree_hash: str | None) -> None:
        def job() -> None:
            files = _config_tree_files(ESPHOME_DIR, cfg)
            # v1.16.2: CHANGELOG прошивки — в копию (для «что нового»), но не в хеш «YAML изменён»
            cl = os.path.join(ESPHOME_DIR, fwnotes.CHANGELOG_REL)
            if os.path.isfile(cl):
                files = [*files, cl]
            fwnotes.snapshot(files, ESPHOME_DIR, os.path.join(self.snap_root, "build"), tree_hash)
        try:
            await self.hass.async_add_executor_job(job)
        except Exception as err:  # noqa: BLE001 — без копии просто не будет diff
            _LOGGER.warning("%s: build snapshot failed: %s", self.name, err)

    def _apply_rollback(self) -> None:
        """v1.18: на датчике версия из резервной копии — хеш YAML и копия YAML той версии,
        чтобы «Firmware» снова предложила текущий YAML как обновление."""
        b, self._rollback = self._rollback, None
        if not b or self.file != b.get("image"):
            return
        self.flashed_hash = b.get("flashed_hash") or "older"

        async def job() -> None:
            try:
                await self.hass.async_add_executor_job(restore_snapshot, b, self.snap_root)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("%s: rollback snapshot restore failed: %s", self.name, err)
            self._changes_key = None
            await self.async_refresh_config_hash()
        self.hass.async_create_task(job())
        _LOGGER.info("%s: rolled back to %s", self.name, b.get("firmware_version") or b.get("name"))

    def _promote_snapshot(self) -> None:
        async def job() -> None:
            try:
                await self.hass.async_add_executor_job(fwnotes.promote, self.snap_root, self.flashed_hash)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("%s: snapshot promote failed: %s", self.name, err)
            self._changes_key = None
            await self.async_refresh_config_hash()
        self.hass.async_create_task(job())

    async def _refresh_changes(self, cfg: str) -> bool:
        """Пересчитать «что изменилось», когда сменился YAML или прошитая сборка. True — изменилось."""
        cl_cur = os.path.join(ESPHOME_DIR, fwnotes.CHANGELOG_REL)
        cl_old = os.path.join(self.snap_root, "flashed", fwnotes.CHANGELOG_REL)

        def mt(p: str) -> float | None:
            try:
                return os.path.getmtime(p)
            except OSError:
                return None
        cl_key = await self.hass.async_add_executor_job(lambda: (mt(cl_cur), mt(cl_old)))
        key = (self.config_hash, self.flashed_hash, self.firmware_version, cl_key)
        if key == self._changes_key:
            return False
        self._changes_key = key
        # v1.16.2: версии прошивки из CHANGELOG (прошитая копия / текущий файл)
        cur_text, old_text = await self.hass.async_add_executor_job(
            lambda: (fwnotes.read_file(cl_cur), fwnotes.read_file(cl_old)))
        self.fw_version_flashed = fwnotes.top_version(old_text)
        self.fw_current_section = fwnotes.parse_changelog(old_text)[:1]
        dev = fw_semver(self.firmware_version)   # v1.17.0: версию сообщает сама прошивка (1.4.0+)
        if dev:
            self.fw_version_flashed = dev
            self.fw_current_section = fwnotes.section_for(cur_text, dev) or self.fw_current_section
        self.fw_version_new = fwnotes.top_version(cur_text)
        if not (self.config_hash and self.flashed_hash and self.config_hash != self.flashed_hash):
            self.fw_changes = self.fw_changed_since = None
            self.fw_sections = []
            return True
        self.fw_sections = (fwnotes.sections_after(cur_text, dev) if dev
                            else fwnotes.new_sections(cur_text, old_text))
        built = build_time(self.firmware_version)

        def job():
            files = _config_tree_files(ESPHOME_DIR, cfg)
            ch = fwnotes.changes(files, ESPHOME_DIR, os.path.join(self.snap_root, "flashed"), self.flashed_hash)
            since = fwnotes.changed_since(files, ESPHOME_DIR, built) if ch is None and built else None
            return ch, since
        try:
            self.fw_changes, self.fw_changed_since = await self.hass.async_add_executor_job(job)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("%s: release notes diff failed: %s", self.name, err)
            self.fw_changes = self.fw_changed_since = None
        return True

    @property
    def changed_files_short(self) -> str | None:
        """«base.yaml, move-sensor-ble-148f98.yaml» — для краткого описания обновления."""
        items = (self.fw_changes or {}).get("files") or self.fw_changed_since or []
        if not items:
            return None
        names = [os.path.basename(i["file"]) for i in items]
        return ", ".join(names[:4]) + (f" и ещё {len(names) - 4}" if len(names) > 4 else "")

    @staticmethod
    def build_label(version: str | None) -> str | None:
        """v1.15.4: «build 16.11 13:48» из vYYYY.MM.DD.HHMMSS-<sha> (для людей; хеш — в примечаниях)."""
        t = build_time(version)
        if t is None:
            return version
        return "build " + dt_util.as_local(dt_util.utc_from_timestamp(t)).strftime("%d.%m %H:%M")

    @property
    def new_build_label(self) -> str | None:
        """v1.15.4: «new build · YAML changed 16.11 14:15» — есть правки, которых нет на датчике."""
        if not (self.config_hash and self.flashed_hash and self.config_hash != self.flashed_hash):
            return None
        if self.config_mtime:
            when = dt_util.as_local(dt_util.utc_from_timestamp(self.config_mtime)).strftime("%d.%m %H:%M")
            return f"new build · YAML changed {when}"
        return "new build · YAML changed"

    @property
    def last_flash_summary(self) -> str | None:
        """v1.15.3: «14:03 · Wi-Fi · 11 s · 118 kB/s» или «14:03 · already on the sensor»."""
        lf = self.last_flash
        if not lf:
            return None
        at = dt_util.parse_datetime(lf.get("at") or "")
        when = dt_util.as_local(at).strftime("%d.%m %H:%M") if at else "?"
        if lf.get("result") == "already_on_sensor":
            return f"{when} · already on the sensor, not flashed"
        parts = [when, lf.get("channel") or "?"]
        if lf.get("seconds") is not None:
            sec = lf["seconds"]
            parts.append(f"{sec:.0f} s" if sec < 120 else f"{sec / 60:.1f} min")
        if lf.get("speed_kbs"):
            parts.append(f"{lf['speed_kbs']:.0f} kB/s")
        chk = dt_util.parse_datetime(lf.get("same_build_checked_at") or "")
        if chk:
            parts.append(f"same build {dt_util.as_local(chk).strftime('%H:%M')} — not reflashed")
        return " · ".join(parts)

    @property
    def key_state(self) -> str:
        """Ключ OTA: accepted (датчик принял) / rejected (не принял) / missing (нет ключа) / unknown."""
        if self.auth_block is not None:
            return "missing" if self.auth_block.get("reason") == "no_key" else "rejected"
        return "accepted" if self.key_ok_at is not None else "unknown"

    @property
    def firmware_in_progress(self) -> bool:
        return self.status in (ST_BUILDING, ST_WAITING, ST_CONNECTING, ST_FLASHING)

    @property
    def latest_firmware(self) -> str | None:
        inst = self.firmware_version
        if inst is None:
            return None
        if self.config_hash and self.flashed_hash and self.config_hash != self.flashed_hash:
            return f"{inst} + YAML {self.config_hash[:6]}"
        return inst

    # ================= v1.15: каналы, данные, номер правки =================
    @property
    def wifi_live(self) -> bool:
        return self.wifi is not None and self.wifi.live

    @callback
    def _data_update(self, key: str, value, src: str, now: datetime | None = None) -> None:
        if value is None:
            return
        now = now or dt_util.utcnow()
        self.data[key] = {"v": value, "ts": now.isoformat(), "src": src}

    def data_value(self, key: str):
        d = self.data.get(key)
        return None if d is None else d.get("v")

    def data_info(self, key: str) -> dict:
        d = self.data.get(key) or {}
        return {"source": d.get("src"), "updated": d.get("ts")}

    def _fw_has_flags(self) -> bool:
        """Флаги сервисного режима в поле wake — прошивка 1.7.0+."""
        sv = fw_semver(self.firmware_version)
        try:
            return bool(sv) and tuple(int(x) for x in sv.split(".")) >= (1, 7, 0)
        except ValueError:
            return False

    @callback
    def _arm_service_expiry(self) -> None:
        """Флаг устаревает через SERVICE_FLAG_FRESH_S без новых пакетов — тогда перерисовать
        (иначе «On — on another network» висит до следующего пакета, ревью 2026-11-19)."""
        if self._svc_unsub is not None:
            self._svc_unsub()

        @callback
        def _expired(_now) -> None:
            self._svc_unsub = None
            self._notify()
        self._svc_unsub = async_call_later(self.hass, SERVICE_FLAG_FRESH_S + 1, _expired)

    @property
    def service_state(self) -> str:
        """Сервисный режим: wifi — HA на связи по Wi-Fi; away — Wi-Fi включён, датчик на роутере,
        но HA его не достаёт (другая сеть); own_ap — Wi-Fi включён, роутера нет (только своя точка);
        off — Wi-Fi выключен (или прошивка без флагов)."""
        if self.wifi_live:
            return "wifi"
        s = self.ble_service
        if s and s[0] and time.monotonic() - s[2] < SERVICE_FLAG_FRESH_S:
            return "away" if s[1] else "own_ap"
        return "off"

    @property
    def link(self) -> str | None:
        """Канал связи: wifi — ESPHome API подключён; ble — иначе (если датчик вообще слышен)."""
        if self.wifi_live:
            return "wifi"
        return "ble" if self.last_seen is not None else None

    @property
    def signal(self) -> int | None:
        if self.link == "wifi":
            v = self.wifi.current("wifi_rssi")
            return None if v is None else int(v)
        return self.ble_rssi

    @property
    def signal_percent(self) -> int | None:
        """Тот же сигнал в %, как в прошивке и остальных проектах: −90 дБм = 0 %, −30 дБм = 100 %."""
        s = self.signal
        if s is None:
            return None
        return int(round(max(0.0, min(100.0, (s + 90) * 100.0 / 60.0))))

    @callback
    def _settings_event(self, kind: str, changes: dict, channel: str) -> None:
        """v1.15: событие «Settings change» — что изменилось, откуда, номер правки."""
        changes = {k: v for k, v in changes.items() if not same_value(k, v[0], v[1])}
        if kind in ("saved_wifi", "saved_ble"):
            # возврат своих значений уже отмечен событием restored — не дублировать
            changes = {k: v for k, v in changes.items() if k not in self._restore_keys}
            for k in list(changes):
                self._restore_keys.discard(k)
        if not changes:
            return
        from homeassistant.helpers.dispatcher import async_dispatcher_send  # noqa: PLC0415
        from .const import SIGNAL_SETTINGS_EVENT  # noqa: PLC0415

        def fmt(v):   # v1.15.3: 32.0 -> 32 (ESPHome по Wi-Fi отдаёт float)
            return int(v) if isinstance(v, float) and v.is_integer() else v

        data = {"changes": {k: {"from": fmt(v[0]), "to": fmt(v[1])} for k, v in changes.items()},
                "summary": ", ".join(f"{k}: {fmt(v[0])} → {fmt(v[1])}" for k, v in changes.items()),
                "channel": channel, "settings_revision": self.rev_seen}

        @callback
        def send(_now=None) -> None:   # @callback: async_call_later иначе вызовет её в потоке
            data["settings_revision"] = self.rev_seen
            _LOGGER.info("%s: settings %s (%s): %s", self.name, kind, channel, data["summary"])
            async_dispatcher_send(self.hass, SIGNAL_SETTINGS_EVENT.format(self.entry.entry_id), kind, data)

        if kind == "saved_wifi":
            # v1.15.3: номер правки датчик присылает отдельной сущностью сразу после значения — дождаться,
            # чтобы в событии был номер ПОСЛЕ этой записи
            async_call_later(self.hass, 1.5, send)
        else:
            send()

    @callback
    def _on_rev(self, rev: int, via: str) -> None:
        """Номер правки с датчика изменился? Чужая правка — принять; 0 (стёрт флеш) — вернуть свои."""
        self.rev_capable = True
        if rev == self.rev_seen:
            return
        prev, self.rev_seen = self.rev_seen, rev
        if prev is None:
            pass   # первый раз видим номер — запоминаем
        elif rev == 0:
            _LOGGER.warning("%s: settings revision %s -> 0 (flash erased): restoring HA values", self.name, prev)
            self._resync = True
            self._need_read = True
        elif rev == self.rev_own or time.monotonic() < self._wifi_own_until:
            self.rev_own = rev   # своя запись (BLE-ответ или эхо по Wi-Fi)
        else:
            _LOGGER.info("%s: settings changed on the sensor (revision %s -> %s, via %s): adopting",
                         self.name, prev, rev, via)
            self._adopt = True
            self._need_read = True
        if self.wifi_live:
            self.hass.async_create_task(self._wifi_sync())
        else:
            self._ensure_worker()
        self._save()
        self._notify()

    def _after_adopt(self, old_target: dict, channel: str = "ble") -> None:
        """Приняли значения с датчика: окна, поменянные не из HA, выключают авто-окно (иначе откатит)."""
        if self._adopt and old_target:
            self._settings_event("changed_on_sensor",
                                 {k: (old_target.get(k), v) for k, v in self.target.items()
                                  if k in old_target and old_target.get(k) is not None}, channel)
        if self._adopt:
            for k in WINDOW_KEYS:
                if (self.auto_window and k in self.target and old_target.get(k) is not None
                        and not same_value(k, self.target[k], old_target[k])):
                    self.auto_window = False
                    _LOGGER.info("%s: %s changed on the sensor — auto window off", self.name, k)
        self._adopt = False

    @callback
    def _on_wifi_link(self, live: bool) -> None:
        _LOGGER.info("%s: Wi-Fi channel %s", self.name, "up" if live else "down")
        if live:
            self.hass.async_create_task(self._wifi_sync())
            self._wifi_build_version(self.wifi.current("build_version") if self.wifi else None)
            if self._ota is not None:
                self._adv_event.set()   # прошивка ждёт окно BLE — Wi-Fi появился, шить сразу
        else:
            self._ensure_worker()   # то, что не успели по Wi-Fi, уйдёт по BLE
        self._notify()

    @callback
    def _wifi_build_version(self, ver) -> None:
        """2026-11-19: версия прошивки по Wi-Fi (Build Version) — то же, что INFO по BLE."""
        if not ver or not fw_semver(ver) or ver == self.firmware_version:
            return
        _LOGGER.info("%s: firmware version over Wi-Fi: %s (was %s)", self.name, ver, self.firmware_version)
        self.firmware_version = ver
        self.firmware_seen = dt_util.now()
        self._save()
        self._notify()
        self.hass.async_create_task(self.async_refresh_config_hash())

    @callback
    def _on_wifi_state(self, key: str, value, first: bool) -> None:
        now = dt_util.utcnow()
        if key == "build_version":
            self._wifi_build_version(value)
            return
        if key in DATA_KEYS:
            self._data_update(key, value, "wifi", now)
            if key == "occupancy":
                self._account({"occupancy": bool(value)}, now)
        elif key == "settings_rev":
            if value is not None:
                self._on_rev(int(value), "wifi")
        elif key in SETTING_KEYS and value is not None:
            self.applied[key] = value
            echo = self._wifi_echo.get(key)
            if echo is not None and same_value(key, value, echo[0]):
                self._wifi_echo.pop(key, None)
                self._settings_event("saved_wifi", {key: (self.target.get(key), value)}, "wifi")
                self.target[key] = value
                if same_value(key, self.desired.get(key), value):
                    self.desired.pop(key, None)
                self.settings_applied_at = dt_util.now()
                self.settings_synced = self.settings_applied_at
            self._save()
        self._notify()

    async def _wifi_sync(self) -> None:
        """Wi-Fi на связи: прочитать настройки, принять/вернуть по номеру правки, отправить заданное."""
        if not self.wifi_live:
            return
        async with self._wifi_lock:
            actual = {}
            for k in SETTING_KEYS:
                if self.wifi.has(k):
                    v = self.wifi.current(k)
                    if v is not None:
                        actual[k] = v
            if not actual:
                return
            rev = self.wifi.current("settings_rev")
            if rev is not None:
                self.rev_capable = True
                rev = int(rev)
                if rev != self.rev_seen and rev not in (0, self.rev_own) and self.rev_seen is not None:
                    self._adopt = True
                self.rev_seen = rev
            old_target = dict(self.target)
            self.applied.update(actual)
            restore = bool(self.target) and (rev == 0 or (self._resync and rev is None))
            if restore:
                # стёрт флеш (номер 0) или старая прошивка после перезагрузки — вернуть значения HA
                drift = {}
                for k, v in self.target.items():
                    if k in actual and not same_value(k, v, actual[k]):
                        self.desired[k] = v
                        drift[k] = (actual[k], v)
                self._settings_event("restored", drift, "wifi")
                self._restore_keys |= set(drift)
                self._adopt = False
            elif self._adopt or self._resync or not self.target:
                # правка на датчике (сайт / ESPHome) или первое чтение — датчик источник правды
                self._adopt = True
                self.target.update({k: v for k, v in actual.items() if v is not None})
                self._after_adopt(old_target, "wifi")
            self._resync = False
            self._need_read = False
            for k in [k for k, v in self.desired.items() if same_value(k, v, actual.get(k))]:
                self.desired.pop(k, None)
            self.settings_synced = dt_util.now()
        await self._wifi_flush()
        self._save()
        self._notify()

    async def _wifi_flush(self) -> None:
        """Отправить заданное по Wi-Fi (сразу, без окна BLE). Не вышло — останется для BLE."""
        if not self.wifi_live:
            return
        async with self._wifi_lock:
            for k, v in list(self.pending().items()):
                if not self.wifi.has(k):
                    continue
                self._wifi_echo[k] = (v, time.monotonic() + WIFI_ECHO_S)
                self._wifi_own_until = time.monotonic() + WIFI_ECHO_S
                try:
                    await self.wifi.async_write(k, v)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("%s: Wi-Fi write %s failed: %s", self.name, k, err)
                    self._wifi_echo.pop(k, None)
        self._notify()

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
            # свой блок (v1.14.2): Wake count и счётчик срабатываний; у старой прошивки его нет —
            # Wake count остаётся из BTHome (0x3D)
            custom = custom_parse(service_info.manufacturer_data)
            if custom and not self._fw_has_flags():
                # прошивка до 1.7.0: поле wake — весь u16, флагов нет (ревью 2026-11-19)
                raw = int.from_bytes(((service_info.manufacturer_data or {}).get(CUSTOM_COMPANY_ID) or b"\0\0")[0:2], "little")
                custom["wake"] = raw
                custom.pop("service", None)
                custom.pop("router", None)
            parsed.update(custom)
            if "service" in custom:
                prev_state = self.service_state
                self.ble_service = (custom["service"], custom["router"], time.monotonic())
                self._arm_service_expiry()
                if self.service_state != prev_state:
                    self._notify()
            self._mark_seen(service_info, parsed)
            rev = parsed.get("settings_rev")
            # прошивка до 1.15 шлёт 0 всегда — номер учитываем, только если прошивка его умеет
            if rev is not None and (rev != 0 or self.rev_capable):
                self._on_rev(int(rev), "ble")
        wake = parsed.get("wake")
        if wake is not None:
            prev, self._last_wake = self._last_wake, wake
            # Wake count живёт в RTC-памяти: после любой загрузки «не из сна» начинается с 1.
            # Уменьшился (и это не переполнение u16) -> перезагрузка -> прочитать DIAG.
            if prev is not None and wake < prev and not (prev > WAKE_MASK - 100 and wake < 100):
                _LOGGER.info("%s: wake count %d -> %d, device rebooted", self.name, prev, wake)
                self._diag_deferred = True
                self._auth_unblock("device rebooted")
                if self.status == ST_SUCCESS:
                    self._rebooted_after_flash = True
        if self._diag_deferred:
            batt = parsed.get("battery")
            # на почти пустой банке (brownout-цикл) не добавляем подключение — прочитаем после зарядки
            if batt is None or batt > 10:
                self._diag_deferred = False
                self._need_diag = True
                # v1.14.0: настройки могли вернуться к значениям прошивки — перечитать и вернуть свои
                self._need_read = True
                self._resync = True
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
        for k in DATA_KEYS:
            if parsed.get(k) is not None:
                self._data_update(k, parsed[k], "ble", now)
        if service_info.rssi is not None and service_info.rssi > -127:
            self.ble_rssi = int(service_info.rssi)
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
            rebooted = prev is not None and wake < prev and not (prev > WAKE_MASK - 100 and wake < 100)
            if prev is None or wake != prev:
                if prev is not None:
                    # после перезагрузки счёт с 1; переход 14-битного счётчика через ноль — по модулю
                    delta = wake if rebooted else (wake - prev) & WAKE_MASK
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
                self.last_motion = now
            else:
                self.occupied_since = None
            self.occupied = occ
        # свежее срабатывание PIR: счётчик из своего блока изменился (v1.14.2)
        mc = parsed.get("motion_count")
        if mc is not None:
            if self.motion_count is not None and mc != self.motion_count:
                self.last_motion = now
            self.motion_count = mc
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
        # v1.14.2: прошивка шлёт свежие срабатывания (свой блок) — залипание = «занято» и свежих
        # срабатываний нет дольше порога (человек шевелится — не проблема). Старая прошивка —
        # как раньше: «занято» непрерывно дольше порога.
        ref = self.occupied_since
        if self.motion_count is not None and self.last_motion is not None and self.last_motion > ref:
            ref = self.last_motion
        return (dt_util.utcnow() - ref).total_seconds() >= self.stuck_threshold_min * 60

    async def async_set_stuck_threshold(self, minutes: int) -> None:
        self.stuck_threshold_min = int(minutes)
        self._save()
        self._notify()

    @callback
    def _tick(self, _now=None) -> None:
        if self.wifi is not None:
            self.wifi.check_link()
        if self._ticks % 5 == 0:   # раз в 5 мин: не изменился ли YAML датчика
            self.hass.async_create_task(self.async_refresh_config_hash())
        self.hass.async_create_task(self.async_sync_api_key())   # v1.17.0: ключ API -> ESPHome-устройство
        self._ticks += 1
        before = self.stats.get("date")
        self._roll_day()
        if self.test_mode.get("state") == "active" and not self.test_mode_on:
            self.test_mode.update(state="off", want=False)
            self._save()
            self._notify()
        self._check_missed_climate()
        if self.auth_block is not None and time.monotonic() >= self._auth_check_at:
            self._auth_check_at = time.monotonic() + AUTH_KEY_CHECK_S
            self.hass.async_create_task(self._auth_recheck())
        sst = self.settings_status
        if sst != self._last_sst:
            # applied -> idle через SETTINGS_APPLIED_SHOW_MIN: обновить сенсор
            self._last_sst = sst
            self._notify()
        self._check_issues()
        stuck = self.pir_stuck
        if stuck != getattr(self, "_stuck_reported", None) or before != self.stats.get("date"):
            if stuck and not getattr(self, "_stuck_reported", False):
                _LOGGER.warning("%s: PIR stuck — «занято» %s мин", self.name, self.stuck_threshold_min)
            self._stuck_reported = stuck
            self._save()
            self._notify()

    # ================= Repairs (v1.20): батарея и пропажа датчика =================
    ISSUES = ("battery_low", "battery_empty", "not_reporting", "pir_stuck")

    def _issue(self, kind: str) -> str:
        return f"{kind}_{self.entry.entry_id}"

    def _set_issue(self, kind: str, on: bool, severity=None, **placeholders) -> None:
        """Создать/обновить или убрать уведомление в «Ремонте». На новом — событие для автоматизаций."""
        active = self.__dict__.get("_issues_on")
        if active is None:
            # после перезапуска: уже висящие уведомления считаем своими (без повторного события)
            reg = ir.async_get(self.hass)
            active = {k for k in self.ISSUES if reg.async_get_issue(DOMAIN, self._issue(k)) is not None}
            self.__dict__["_issues_on"] = active
        if not on:
            if kind in active:
                active.discard(kind)
                ir.async_delete_issue(self.hass, DOMAIN, self._issue(kind))
            return
        ph = {"name": self.name} | {k: str(v) for k, v in placeholders.items()}
        if kind not in active or self.__dict__.get(f"_issue_ph_{kind}") != ph:
            ir.async_create_issue(
                self.hass, DOMAIN, self._issue(kind), is_fixable=False,
                is_persistent=kind in ("battery_empty", "not_reporting"),
                severity=severity or ir.IssueSeverity.WARNING, translation_key=kind, translation_placeholders=ph,
            )
            self.__dict__[f"_issue_ph_{kind}"] = ph
        if kind not in active:
            active.add(kind)
            self.hass.bus.async_fire(f"{DOMAIN}_issue", {"issue": kind, "name": self.name,
                                                         "address": self.address, **ph})

    def remove_issues(self) -> None:
        for kind in self.ISSUES:
            ir.async_delete_issue(self.hass, DOMAIN, self._issue(kind))
        self.__dict__["_issues_on"] = set()

    def silent_limit_min(self) -> float | None:
        """Через сколько минут без данных датчик считается пропавшим. None — не следим
        (климат выключен: без движения датчик молчит сколько угодно, это норма)."""
        ci = self.value("climate_interval")
        ci = 5.0 if ci is None else float(ci)
        if ci <= 0:
            return None
        opt = str(self.entry.options.get(CONF_SILENT_AFTER, "auto"))
        if not opt.isdigit():
            return max(15.0, 3 * ci)
        # не короче двух интервалов климата: между обычными посылками ложной тревоги не будет
        return max(float(opt), 2 * ci)

    def _check_issues(self) -> None:
        pct = self.data_value("battery")
        volt = self.data_value("voltage")
        low = self.value("battery_low")
        low = float(low) if low is not None else 10.0
        if pct is not None:
            days = (self.battery_forecast() or {}).get("days_left")
            if pct <= low:
                self._set_issue("battery_low", True, pct=round(pct), level=round(low),
                                days=f"{round(days)}" if days is not None else "?")
            elif pct >= low + 5:
                self._set_issue("battery_low", False)
        silent = False
        limit = self.silent_limit_min()
        if limit is not None and self.last_seen is not None and not self.test_mode_on \
                and self.status not in (ST_FLASHING, ST_CONNECTING):
            mins = (dt_util.utcnow() - self.last_seen).total_seconds() / 60
            silent = mins > limit
        empty = silent and ((volt is not None and float(volt) <= ISSUE_EMPTY_V) or (pct is not None and pct <= 3))
        when = dt_util.as_local(self.last_seen).strftime("%d.%m %H:%M") if self.last_seen else "?"
        self._set_issue("battery_empty", empty, ir.IssueSeverity.ERROR, last=when,
                        volt=f"{float(volt):.2f}" if volt is not None else "?")
        self._set_issue("not_reporting", silent and not empty, ir.IssueSeverity.ERROR, last=when,
                        pct=round(pct) if pct is not None else "?")
        # молчит — «занято» осталось от последней посылки, это не залипание датчика
        self._set_issue("pir_stuck", self.pir_stuck and not silent, minutes=self.stuck_threshold_min)

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

    # ================= авторизация (v1.14.1) =================
    @staticmethod
    def _key_fp(key: str | None) -> str:
        return hashlib.sha256(key.encode()).hexdigest()[:12] if key else "none"

    def _issue_id(self) -> str:
        return f"auth_{self.entry.entry_id}"

    def _auth_fail(self, reason: str, key: str | None) -> None:
        """Ключ не подошёл / ключа нет: больше не подключаемся, пока действующий ключ тот же."""
        self.auth_block = {"reason": reason, "key_fp": self._key_fp(key), "since": dt_util.now().isoformat()}
        self._auth_check_at = time.monotonic() + AUTH_KEY_CHECK_S
        _LOGGER.error(
            "%s: %s - BLE connections paused until the key changes (options or %s in %s), "
            "Read settings / flash, or %d h",
            self.name, "OTA key rejected by the sensor" if reason == "auth" else "no OTA key",
            SECRET_NAME, SECRETS_PATH, AUTH_RETRY_H,
        )
        self._raise_auth_issue(reason)
        self._save()
        self._notify()

    def _raise_auth_issue(self, reason: str) -> None:
        ir.async_create_issue(
            self.hass, DOMAIN, self._issue_id(), is_fixable=False, severity=ir.IssueSeverity.ERROR,
            translation_key="auth_failed" if reason == "auth" else "no_key",
            translation_placeholders={"name": self.name, "secret": SECRET_NAME, "path": SECRETS_PATH,
                                      "hours": str(AUTH_RETRY_H)},
        )

    def _auth_ok(self) -> None:
        if self.key_ok_at is None or (dt_util.utcnow() - self.key_ok_at).total_seconds() > 3600:
            self.key_ok_at = dt_util.utcnow()
            self._save()
        if self.auth_block is not None:
            _LOGGER.info("%s: OTA key accepted - BLE connections resumed", self.name)
            self.auth_block = None
            self._save()
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id())
        persistent_notification.async_dismiss(self.hass, self._notice_id())

    def _notice_id(self) -> str:
        return f"{DOMAIN}_auth_{self.entry.entry_id}"

    def _auth_text(self) -> str:
        return (
            f"Датчик «{self.name}» не принял ключ OTA — прошивка не запущена.\n\n"
            "Ключ в Home Assistant: поле «Ключ OTA» в параметрах интеграции, "
            f"а если оно пустое — `{SECRET_NAME}` в `{SECRETS_PATH}`. "
            "В датчике — ключ из secrets.yaml на момент сборки его прошивки.\n\n"
            "Исправить: сделать ключи одинаковыми (вписать верный ключ в параметры) "
            "или прошить датчик по USB с текущим secrets.yaml, затем нажать прошивку ещё раз."
        )

    def _flash_auth_notice(self) -> None:
        """Прошивка ждала окно, а датчик отклонил ключ — показать уведомление (кнопку уже не вернуть)."""
        persistent_notification.async_create(
            self.hass, self._auth_text(), title=f"{self.name}: прошивка не началась",
            notification_id=self._notice_id(),
        )

    async def async_auth_gate(self, why: str) -> None:
        """Перед прошивкой: если датчик уже отклонил этот же ключ — сразу ошибка (всплывёт в UI),
        не ставим прошивку в очередь. Ключ сменился — пробуем заново."""
        blk = self.auth_block
        if blk is None:
            return
        try:
            key = await self._key()
        except OtaError:
            key = None
        if key is not None and self._key_fp(key) == blk.get("key_fp") and blk.get("reason") == "auth":
            raise OtaError(
                f"Датчик не принял ключ OTA — прошивка не запущена. Проверь «Ключ OTA» "
                f"в параметрах или {SECRET_NAME} в secrets.yaml (подробно — в «Ремонт»)"
            )
        self._auth_unblock(why)

    def _auth_unblock(self, why: str) -> None:
        if self.auth_block is None:
            return
        _LOGGER.info("%s: retrying BLE auth (%s)", self.name, why)
        self.auth_block = None
        self.settings_error = None
        self._save()
        self._notify()
        self._ensure_worker()

    async def _auth_recheck(self) -> None:
        """Действующий ключ сменился (поле может быть пустым — тогда secrets.yaml) или прошло
        AUTH_RETRY_H часов — снять паузу и попробовать ещё раз."""
        from datetime import timedelta  # noqa: PLC0415
        blk = self.auth_block
        if blk is None:
            return
        try:
            key = await self._key()
        except OtaError:
            key = None
        since = dt_util.parse_datetime(blk.get("since") or "")
        if self._key_fp(key) != blk.get("key_fp"):
            self._auth_unblock("key changed")
        elif since is None or dt_util.now() - since >= timedelta(hours=AUTH_RETRY_H):
            self._auth_unblock(f"{AUTH_RETRY_H} h passed")

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
                    # v1.15: канал выбирается в момент прошивки: Wi-Fi на связи — сразу по Wi-Fi
                    if self.wifi_live and time.monotonic() >= self._wifi_ota_after:
                        if await self._wifi_flash():
                            continue
                    if self.wifi_live:
                        timeout = min(timeout, 30.0)   # Wi-Fi есть, но пауза после обрыва — проверить ещё раз
                self._adv_event.clear()
                try:
                    await asyncio.wait_for(self._adv_event.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    continue
                if self._ota is not None and self.wifi_live and time.monotonic() >= self._wifi_ota_after:
                    continue   # разбудил Wi-Fi — прошивка по Wi-Fi в начале цикла
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

    def _job_snapshot(self) -> dict | None:
        if self._ota is None or not self.file:
            return self._shutdown_job   # выгрузка/остановка HA: задание сохранено до очистки

        left = max(0.0, self._ota.deadline - time.monotonic())
        return {"file": self.file, "deadline": (dt_util.utcnow().timestamp() + left), "build_hash": self._build_hash,
                "rollback": self._rollback}

    async def _resume_job(self, job: dict) -> None:
        """После перезапуска HA продолжить ожидание/прошивку тем же файлом (с начала)."""
        left_min = (float(job.get("deadline") or 0) - dt_util.utcnow().timestamp()) / 60
        path = job.get("file")
        if not path or left_min <= 1 or not await self.hass.async_add_executor_job(os.path.isfile, path):
            self._set(ST_CANCELLED, error="прошивка отменена перезапуском Home Assistant", finished=dt_util.now())
            return
        try:
            self._build_hash = job.get("build_hash")
            await self.async_start(path, int(left_min), rollback=job.get("rollback"))
            _LOGGER.info("%s: firmware job resumed after HA restart (%s)", self.name, os.path.basename(path))
        except OtaError as err:
            self._set(ST_ERROR, error=f"после перезапуска HA: {err}", finished=dt_util.now())

    async def _adopt_external_flash(self, cfg: str, h: str, mtime: float) -> bool:
        """2026-11-19: на датчике сборка новее файлов прошивки, а «прошит» — другой хеш: датчик
        прошили мимо интеграции (ESPHome Builder, USB) из текущих файлов. Принять её как прошитую
        (хеш и копия файлов для «что нового»), иначе «Firmware» предлагает «new build». True — принята."""
        if self._ota is not None or self.status == ST_FLASHING:
            return False
        dev_fp = version_fingerprint(self.firmware_version)
        if dev_fp is not None:
            # прошивка 1.7.0+ сама говорит, из каких файлов собрана — без догадок по времени
            try:
                cur_fp = await self.hass.async_add_executor_job(fw_fingerprint, ESPHOME_DIR, cfg)
            except Exception:  # noqa: BLE001
                return False
            if dev_fp != cur_fp:
                if self.flashed_hash == h:   # считали текущей, а на датчике собрано из других файлов
                    self.flashed_hash = "older"
                    self._changes_key = None
                    self._save()
                    return True
                return False
            if self.flashed_hash == h:
                return False
        else:
            # старые прошивки: по времени сборки (не позже правки файлов, 5 с на разницу часов)
            built = build_time(self.firmware_version)
            if (built is None or self.flashed_hash == h or built + 5 < mtime
                    or time.monotonic() - self._own_flash_at < OWN_FLASH_WINDOW_S):
                return False

        def job() -> None:
            files = _config_tree_files(ESPHOME_DIR, cfg)
            cl = os.path.join(ESPHOME_DIR, fwnotes.CHANGELOG_REL)
            if os.path.isfile(cl):
                files = [*files, cl]
            fwnotes.snapshot(files, ESPHOME_DIR, os.path.join(self.snap_root, "flashed"), h)
        try:
            await self.hass.async_add_executor_job(job)
        except Exception as err:  # noqa: BLE001 — без копии просто не будет «что нового»
            _LOGGER.warning("%s: flashed snapshot failed: %s", self.name, err)
        _LOGGER.info("%s: firmware %s was installed outside the integration from the current files — "
                     "taken as flashed", self.name, self.firmware_version)
        self.flashed_hash = h
        self._changes_key = None
        self._save()
        return True

    def _flash_success(self, channel: str) -> None:
        self._own_flash_at = time.monotonic()
        size = len(self._ota.image.data) if self._ota is not None else None
        self._ota = None
        # при следующем пробуждении перечитать версию и настройки — видно, что встала новая сборка
        self._need_read = True
        self._resync = True   # v1.14.0: новая прошивка могла сбросить настройки
        self._rebooted_after_flash = False
        if self._rollback is None and self._build_hash and self.file and self.last_build \
                and self.file == self.last_build.get("file"):
            self.flashed_hash = self._build_hash   # v1.15: на датчике — эта сборка
            self._promote_snapshot()
        self._apply_rollback()
        self.last_channel = channel
        now = dt_util.now()
        secs = round(time.monotonic() - self._flash_t0, 1) if self._flash_t0 is not None else None
        self.last_flash = {"at": now.isoformat(), "channel": channel, "seconds": secs,
                           "speed_kbs": self.speed_kbs, "bytes": size, "result": "flashed"}
        # v1.15.5: ошибки промежуточных попыток (не успели подключиться) — не итог прошивки
        self._set(ST_SUCCESS, progress=100, finished=now, error=None)
        self._save()
        _LOGGER.info("%s: OTA done over %s, device reboots", self.name, channel)

    async def _wifi_flash(self) -> bool:
        """v1.15: датчик на связи по Wi-Fi (сервисный режим) — прошить штатным OTA ESPHome сразу.
        True — задание закончено (успех или окончательная ошибка); False — связь оборвалась, ждём окно BLE."""
        from .espota import EspOtaCommitted, EspOtaError, EspOtaNetworkError, upload  # noqa: PLC0415

        host = self.wifi.host if self.wifi is not None else None
        if not host or self._ota is None:
            return False
        image = self._ota.image
        self.attempts += 1
        self._set(ST_FLASHING, progress=0, error=None)
        _LOGGER.info("%s: flashing over Wi-Fi (%s, %d bytes)", self.name, host, len(image.data))
        loop = self.hass.loop

        def prog(p: int) -> None:
            def _upd() -> None:
                if self.status == ST_FLASHING and p != self.progress:
                    self.progress = p
                    self._notify()
            loop.call_soon_threadsafe(_upd)

        try:
            pw = await self.hass.async_add_executor_job(read_secret, OTA_PASSWORD_SECRET)
            self.speed_kbs = round(await self.hass.async_add_executor_job(
                lambda: upload(host, image.data, prog, password=pw)), 1)
        except EspOtaCommitted as err:
            _LOGGER.warning("%s: Wi-Fi OTA: image sent, final answer lost (%s) - assuming success", self.name, err)
        except EspOtaNetworkError as err:
            # выключили WiFi посреди прошивки / адрес недоступен — дальше по BLE, в ближайшее окно
            _LOGGER.warning("%s: Wi-Fi OTA interrupted: %s - continuing over BLE", self.name, err)
            self._wifi_ota_after = time.monotonic() + 120
            self._set(ST_WAITING, progress=0, error=f"Wi-Fi: {err} — ждём окно BLE")
            return False
        except EspOtaError as err:
            self._ota = None
            self._set(ST_ERROR, error=f"Wi-Fi OTA: {err}", finished=dt_util.now())
            return True
        self._flash_success("Wi-Fi")
        return True

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
                self._auth_fail("no_key", None)
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
                    self._flash_auth_notice()
                self._auth_fail("auth", key)
                return
            self._auth_ok()
            info = await command(bytes([CMD_INFO]))
            ver = info[2:].decode(errors="replace")
            if self._ota is not None and self.status != ST_FLASHING:
                self.device_version = ver  # «было» для этой прошивки
            ver_changed = ver != self.firmware_version
            self.firmware_version = ver
            self.firmware_seen = dt_util.now()
            self._save()
            self._notify()
            # первый запуск — теперь можно сравнить YAML со сборкой; v1.18.1: датчик сообщил другую версию
            # (новая прошивка встала) — сразу пересчитать «Firmware», а не через 5 мин по таймеру
            if self.flashed_hash is None or ver_changed:
                self.hass.async_create_task(self.async_refresh_config_hash())
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

            # ---- Identify: помигать светодиодом (найти датчик)
            if self.identify.get("state") == "pending":
                await self._send_identify(command)

            # ---- быстрый тест (вкл/выкл)
            if self.test_mode.get("state") == "pending":
                await self._send_test_mode(command)

            # ---- настройки (в каждом подключении, если есть что: прошлая ошибка не мешает —
            # датчик могли перепрошить)
            if (self.pending() or self._need_read) and not self.wifi_live:
                async with self._wifi_lock:
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
                self._flash_success("BLE")
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
        self._auth_unblock("fast test")
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

    @property
    def device_id(self) -> str:
        return device_id(self.address)

    def resolve_config(self, configuration: str | None = None) -> str:
        """YAML для сборки (блокирующая). Явно указанный — как есть; пусто или старое общее имя
        (move-sensor-ble.yaml) — файл этого датчика move-sensor-ble-<device id>.yaml, если он есть,
        иначе старое общее имя (1.13.1)."""
        if configuration and configuration != DEFAULT_ESPHOME_CONFIG:
            return configuration
        own = DEVICE_ESPHOME_CONFIG.format(device_id=self.device_id.lower())
        for cfg in (own, DEFAULT_ESPHOME_CONFIG):
            if os.path.isfile(os.path.join(ESPHOME_DIR, cfg)):
                return cfg
        raise OtaError(f"нет YAML датчика: {own} (или {DEFAULT_ESPHOME_CONFIG}) в {ESPHOME_DIR}")

    async def async_request_identify(self) -> None:
        self._auth_unblock("Identify")
        self.identify = {"state": "pending", "requested": dt_util.now().isoformat()}
        self._notify()
        self._ensure_worker()

    async def _send_identify(self, command) -> None:
        try:
            rsp = await command(bytes([CMD_IDENTIFY, IDENTIFY_SECONDS]), timeout=3.0)
        except asyncio.TimeoutError:
            return  # повтор в следующее окно
        if rsp[1] == 0:
            self.identify.update(state="sent", done=dt_util.now().isoformat(), error=None)
            _LOGGER.info("%s: identify — LED blinks %d s", self.name, IDENTIFY_SECONDS)
        else:
            msg = "прошивка без Identify" if rsp[1] == 10 else DEVICE_STATUS.get(rsp[1], f"status {rsp[1]}")
            self.identify.update(state="error", error=msg)
        self._notify()

    async def async_request_wifi_reset(self) -> None:
        self._auth_unblock("Wi-Fi reset")
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

    async def _sync_battery(self, command, bpend: dict) -> tuple[dict, int | None] | None:
        """v1.19: ёмкость и порог батареи — своя команда (ответ 6 байт, влезает в любой MTU).
        Прошивка до 1.6.0 отвечает «неверная команда» — запоминаем и больше не спрашиваем."""
        if self.battery_unsupported:
            return None
        req = bytes([CMD_SET_BATTERY]) + pack_battery(bpend) if bpend else bytes([CMD_GET_BATTERY])
        rsp = await command(req)
        if len(rsp) < 6 or rsp[1] != 0:
            if len(rsp) >= 2 and rsp[1] == 10:   # ST_STATE: команда незнакома (старая прошивка)
                self.battery_unsupported = True
                _LOGGER.info("%s: firmware has no battery settings (before 1.6.0)", self.name)
            return None
        return unpack_battery(rsp)

    async def _sync_settings(self, command) -> None:
        pend_all = self.pending()
        pend = {k: v for k, v in pend_all.items() if k in SETTINGS_MASK}
        bpend = {k: v for k, v in pend_all.items() if k in BATTERY_MASK}
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
        # v1.15: 18-й байт — номер правки на датчике после этой команды
        rev = rsp[2 + 17] if len(rsp) >= 2 + 18 else None
        # v1.19: батарея — отдельной командой в том же сеансе; её номер правки — самый свежий
        bat = await self._sync_battery(command, bpend)
        if bat is not None:
            actual.update(bat[0])
            if bat[1] is not None:
                rev = bat[1]
            pend = {**pend, **bpend}
        else:
            # сбой только этой команды — прежние значения батареи не теряем
            actual.update({k: self.applied[k] for k in BATTERY_MASK if k in self.applied})
        if rev is not None:
            self.rev_capable = True
            if pend:
                self.rev_own = rev          # эту правку сделал этот HA
            self.rev_seen = rev
        old_target = dict(self.target)
        self.applied = actual
        self._need_read = False
        if not pend and self._resync and rev:
            # v1.15: датчик перезагрузился/перепрошит, но номер правки не 0 — настройки во флеше его
            # собственные (возможно, поменяли на сайте): ПРИНИМАЕМ их, а не возвращаем свои
            self._resync = False
            self._adopt = True
        if not pend:
            if self._resync and self.target:
                # v1.14.0: после перезагрузки датчика вернуть заданные из HA значения
                drift = {k: v for k, v in self.target.items()
                         if k in SETTING_KEYS and v is not None and k in actual
                         and not same_value(k, v, actual.get(k))}
                self._resync = False
                if drift:
                    _LOGGER.warning("%s: settings reset on the sensor after reboot, restoring: %s",
                                    self.name, {k: (actual.get(k), v) for k, v in drift.items()})
                    self._settings_event("restored", {k: (actual.get(k), v) for k, v in drift.items()}, "ble")
                    self._restore_keys |= set(drift)
                    for k, v in drift.items():
                        self.desired[k] = v
                    await self._sync_settings(command)   # тот же сеанс: SET
                    return
            else:
                # прочитано по запросу (Read settings / первый запуск): датчик — источник правды
                # (например, настройку поменяли на сайте датчика по Wi-Fi)
                self._resync = False
                self.target.update({k: v for k, v in actual.items() if k in SETTING_KEYS and v is not None})
                self._after_adopt(old_target)
        else:
            self._settings_event("saved_ble", {k: (old_target.get(k), actual.get(k)) for k in pend
                                               if actual.get(k) is not None}, "ble")
            for k in pend:
                if actual.get(k) is not None:
                    self.target[k] = actual.get(k)   # что датчик реально принял
            if self._adopt:
                # правку на датчике сделали не из HA — остальные ключи принимаем как есть
                self.target.update({k: v for k, v in actual.items()
                                    if k in SETTING_KEYS and k not in pend and v is not None})
                self._after_adopt(old_target)
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
