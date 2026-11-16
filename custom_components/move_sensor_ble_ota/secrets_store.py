"""v1.17.0: пароль OTA (Wi-Fi) и ключ API датчика — в /config/esphome/secrets.yaml.

* Ввод в настройке интеграции: значение пишется в secrets.yaml (ESPHome берёт его при сборке).
  Пусто — остаётся текущее, а если его нет — генерируется случайное и сохраняется.
* Ключ API сам попадает в ESPHome-устройство датчика в HA (noise_psk записи ESPHome) — без
  «Подтвердить» после прошивки. Только когда прошивка его требует: на датчике 1.4.0+ или ESPHome
  уже просит авторизацию (иначе HA с ключом не подключился бы к прошивке без шифрования).
Значения никуда не выводятся (ни в лог, ни в атрибуты).
"""
from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import secrets
import shutil
import time

from homeassistant.config_entries import ConfigEntry, SOURCE_REAUTH
from homeassistant.core import HomeAssistant

from .const import SECRETS_PATH

_LOGGER = logging.getLogger(__name__)

OTA_PASSWORD = "move_sensor_ota_password"
API_KEY = "move_sensor_api_key"
NOISE_PSK = "noise_psk"   # ключ в записи интеграции ESPHome


def gen_ota_password() -> str:
    return secrets.token_hex(16)


def gen_api_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


def valid_api_key(value: str) -> bool:
    """Ключ API ESPHome: 32 байта в base64."""
    try:
        return len(base64.b64decode(value, validate=True)) == 32
    except (binascii.Error, ValueError):
        return False


GENERATORS = {OTA_PASSWORD: gen_ota_password, API_KEY: gen_api_key}
_LINE = re.compile(r"^(?P<name>[A-Za-z0-9_]+)\s*:\s*(?P<value>.*?)\s*(?:#.*)?$")


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def get(name: str, path: str = SECRETS_PATH) -> str | None:
    for line in _read(path).splitlines():
        m = _LINE.match(line.strip())
        if m and m.group("name") == name:
            return m.group("value").strip("\"'") or None
    return None


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def put(name: str, value: str, path: str = SECRETS_PATH) -> None:
    """Записать (заменить или дописать) значение; копия файла — рядом (.bak_before_<имя>_<время>)."""
    text = _read(path)
    lines = text.splitlines()
    new_line = f"{name}: {_quote(value)}"
    for i, line in enumerate(lines):
        m = _LINE.match(line.strip())
        if m and m.group("name") == name:
            lines[i] = new_line
            break
    else:
        if not lines or lines[-1].strip():
            lines.append("")
        lines += ["# Move Sensor BLE: пароль OTA по Wi-Fi / ключ шифрования API (интеграция Move Sensor BLE OTA)",
                  new_line]
    if os.path.exists(path):
        shutil.copy2(path, f"{path}.bak_before_{name}_{time.strftime('%Y%m%d_%H%M%S')}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def ensure(name: str, value: str | None = None, path: str = SECRETS_PATH) -> str:
    """Ввели значение — сохранить его; пусто — оставить текущее, нет текущего — сгенерировать и сохранить.
    Возвращает: "set" | "kept" | "generated" (без самого значения). Блокирующая."""
    value = (value or "").strip()
    cur = get(name, path)
    if value:
        if value != cur:
            put(name, value, path)
            return "set"
        return "kept"
    if cur:
        return "kept"
    put(name, GENERATORS[name](), path)
    return "generated"


# ---------------- ключ API -> ESPHome-устройство датчика ----------------
def _esphome_entry(hass: HomeAssistant, entry_id: str | None, mac: str) -> ConfigEntry | None:
    if entry_id:
        e = hass.config_entries.async_get_entry(entry_id)
        if e is not None and e.domain == "esphome":
            return e
    for e in hass.config_entries.async_entries("esphome"):
        if (e.unique_id or "").lower() == mac.lower():
            return e
    return None


def _reauth_flows(hass: HomeAssistant, entry_id: str) -> list[str]:
    return [f["flow_id"] for f in hass.config_entries.flow.async_progress_by_handler("esphome")
            if f.get("context", {}).get("source") == SOURCE_REAUTH and f.get("context", {}).get("entry_id") == entry_id]


async def async_sync_api_key(hass: HomeAssistant, entry_id: str | None, mac: str, device_encrypted: bool) -> str | None:
    """Ключ из secrets.yaml -> запись ESPHome датчика. device_encrypted — на датчике прошивка 1.4.0+.
    Возвращает, что сделано ("updated"), или None."""
    entry = _esphome_entry(hass, entry_id, mac)
    if entry is None:
        return None
    flows = _reauth_flows(hass, entry.entry_id)
    if not (device_encrypted or flows):
        return None
    key = await hass.async_add_executor_job(get, API_KEY)
    if not key or not valid_api_key(key):
        return None
    if entry.data.get(NOISE_PSK) == key and not flows:
        return None
    if entry.data.get(NOISE_PSK) != key:
        hass.config_entries.async_update_entry(entry, data={**entry.data, NOISE_PSK: key})
    for fid in flows:   # уведомление «требуется авторизация» больше не нужно
        try:
            hass.config_entries.flow.async_abort(fid)
        except Exception:  # noqa: BLE001 — поток мог закончиться сам
            pass
    hass.config_entries.async_schedule_reload(entry.entry_id)
    _LOGGER.info("ESPHome device %s: API encryption key set from secrets.yaml (%s)", mac,
                 "reauth closed" if flows else "firmware 1.4.0+")
    return "updated"
