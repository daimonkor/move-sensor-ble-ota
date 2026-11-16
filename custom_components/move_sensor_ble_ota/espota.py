"""Прошивка по Wi-Fi штатным OTA ESPHome (v1.15, сервисный режим датчика).

Минимальный клиент протокола ESPHome OTA (порт 3232), по мотивам esphome/espota2.py (MIT).
v1.17.0: пароль OTA (прошивка датчика 1.4.0+, `ota: password: !secret move_sensor_ota_password`) —
вызов-ответ SHA256 (или MD5 у старых прошивок): hash(пароль + nonce + cnonce). Шифрования Noise нет.
Блокирующий — вызывать в executor.
"""
from __future__ import annotations

import gzip
import hashlib
import secrets
import socket
import time
from collections.abc import Callable

OTA_PORT = 3232
MAGIC_BYTES = bytes([0x6C, 0x26, 0xF7, 0x5C, 0x45])
RESPONSE_OK = 0x00
RESPONSE_REQUEST_AUTH = 0x01
RESPONSE_REQUEST_SHA256_AUTH = 0x02
RESPONSE_AUTH_OK = 0x41
RESPONSE_UPDATE_PREPARE_OK = 0x42
RESPONSE_BIN_MD5_OK = 0x43
RESPONSE_RECEIVE_OK = 0x44
RESPONSE_UPDATE_END_OK = 0x45
RESPONSE_SUPPORTS_COMPRESSION = 0x46
RESPONSE_CHUNK_OK = 0x47
RESPONSE_FEATURE_FLAGS = 0x48
CLIENT_FEATURE_SUPPORTS_COMPRESSION = 0x01
CLIENT_FEATURE_SUPPORTS_SHA256_AUTH = 0x02
_AUTH = {RESPONSE_REQUEST_SHA256_AUTH: (hashlib.sha256, 64), RESPONSE_REQUEST_AUTH: (hashlib.md5, 32)}
OTA_VERSION_2_0 = 2
BLOCK = 8192
_ERRORS = {
    0x80: "неверные магические байты", 0x81: "ошибка подготовки обновления", 0x82: "неверный пароль OTA",
    0x83: "ошибка записи во флеш", 0x84: "ошибка завершения обновления", 0x85: "неверная загрузка",
    0x89: "не хватает места во флеше", 0x8A: "нет раздела для обновления", 0x8B: "MD5 не совпал",
    0x8D: "подпись образа неверна", 0x94: "датчик требует шифрование OTA", 0xFF: "неизвестная ошибка",
}


class EspOtaError(Exception):
    """Датчик отказал — повторять бессмысленно."""


class EspOtaNetworkError(Exception):
    """Связь оборвалась (выключили WiFi и т. п.) — можно продолжить по BLE."""


class EspOtaCommitted(Exception):
    """Образ передан целиком, ответ потерян — датчик, скорее всего, уже перезагружается в новую прошивку."""


def _recv(sock: socket.socket, n: int, what: str, expect: int | tuple | None) -> bytes:
    try:
        data = sock.recv(1)
    except OSError as err:
        raise EspOtaNetworkError(f"{what}: {err}") from err
    if not data:
        raise EspOtaNetworkError(f"{what}: датчик закрыл соединение")
    code = data[0]
    if code in _ERRORS:
        raise EspOtaError(f"{what}: {_ERRORS[code]} (0x{code:02X})")
    if expect is not None and code not in (expect if isinstance(expect, tuple) else (expect,)):
        raise EspOtaError(f"{what}: неожиданный ответ 0x{code:02X}")
    while len(data) < n:
        try:
            more = sock.recv(n - len(data))
        except OSError as err:
            raise EspOtaNetworkError(f"{what}: {err}") from err
        if not more:
            raise EspOtaNetworkError(f"{what}: датчик закрыл соединение")
        data += more
    return data


def _send(sock: socket.socket, data: bytes, what: str) -> None:
    try:
        sock.sendall(data)
    except OSError as err:
        raise EspOtaNetworkError(f"{what}: {err}") from err


def upload(host: str, image: bytes, on_progress: Callable[[int], None] | None = None,
           port: int = OTA_PORT, timeout: float = 20.0, password: str | None = None) -> float:
    """Залить образ приложения (firmware.ota.bin). Возвращает скорость, кБ/с."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as err:
        raise EspOtaNetworkError(f"подключение к {host}:{port}: {err}") from err
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        _send(sock, MAGIC_BYTES, "magic")
        version = _recv(sock, 2, "версия OTA", RESPONSE_OK)[1]
        _send(sock, bytes([CLIENT_FEATURE_SUPPORTS_COMPRESSION | CLIENT_FEATURE_SUPPORTS_SHA256_AUTH]), "features")
        feat = _recv(sock, 1, "features", None)[0]
        if feat == RESPONSE_FEATURE_FLAGS:
            feat = _recv(sock, 1, "feature flags", None)[0]
            gz = bool(feat & 0x01)
        else:
            gz = feat == RESPONSE_SUPPORTS_COMPRESSION
        payload = gzip.compress(image, compresslevel=9) if gz else image
        auth = _recv(sock, 1, "авторизация", (RESPONSE_AUTH_OK, RESPONSE_REQUEST_AUTH,
                                               RESPONSE_REQUEST_SHA256_AUTH))[0]
        if auth != RESPONSE_AUTH_OK:
            if not password:
                raise EspOtaError("датчик требует пароль OTA ESPHome — нет move_sensor_ota_password в "
                                  "/config/esphome/secrets.yaml")
            hash_func, n = _AUTH[auth]
            nonce = _recv(sock, n, "nonce", None)
            cnonce = secrets.token_hex(n // 2)
            _send(sock, cnonce.encode(), "cnonce")
            h = hash_func()
            for part in (password.encode("utf-8"), nonce, cnonce.encode()):
                h.update(part)
            _send(sock, h.hexdigest().encode(), "ответ авторизации")
            _recv(sock, 1, "авторизация", RESPONSE_AUTH_OK)
        sock.settimeout(90.0)
        _send(sock, len(payload).to_bytes(4, "big"), "размер")
        _recv(sock, 1, "подготовка", RESPONSE_UPDATE_PREPARE_OK)
        _send(sock, hashlib.md5(payload).hexdigest().encode(), "MD5")
        _recv(sock, 1, "MD5", RESPONSE_BIN_MD5_OK)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 0)
        start = time.monotonic()
        off = 0
        while off < len(payload):
            chunk = payload[off:off + BLOCK]
            _send(sock, chunk, "данные")
            off += len(chunk)
            if version >= OTA_VERSION_2_0:
                try:
                    _recv(sock, 1, "блок", RESPONSE_CHUNK_OK)
                except EspOtaNetworkError as err:
                    if off >= len(payload):
                        raise EspOtaCommitted(str(err)) from err
                    raise
            if on_progress:
                on_progress(int(off * 100 / len(payload)))
        dur = max(0.001, time.monotonic() - start)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            _recv(sock, 1, "приём", RESPONSE_RECEIVE_OK)
            _recv(sock, 1, "завершение", RESPONSE_UPDATE_END_OK)
        except EspOtaNetworkError as err:
            raise EspOtaCommitted(str(err)) from err
        try:
            _send(sock, bytes([RESPONSE_OK]), "подтверждение")
        except EspOtaNetworkError:
            pass   # датчик и так уже перезагружается в новую прошивку
        return len(payload) / 1024 / dur
    finally:
        sock.close()
