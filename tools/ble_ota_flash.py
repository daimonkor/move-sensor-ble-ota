#!/usr/bin/env python3
"""
Сборка и прошивка Move Sensor BLE по BLE OTA — одной командой, без браузера.

    python3 ble_ota_flash.py                 # собрать move-sensor-ble.yaml и прошить по BLE
    python3 ble_ota_flash.py --compile-only  # только собрать и скачать .bin
    python3 ble_ota_flash.py --no-start      # собрать и загрузить в интеграцию, не прошивать
    python3 ble_ota_flash.py --file x.bin    # прошить готовый файл (без сборки)

Как работает (только стандартная библиотека Python 3.8+):
  1. HA WebSocket (/api/websocket, токен ha_token из esphome/secrets.yaml)
     -> supervisor/api: находит аддон ESPHome, его ingress-URL и открывает ingress-сессию.
  2. WebSocket ESPHome Builder (<ingress>/ws): firmware/compile -> ждёт задачу (firmware/get_job)
     -> firmware/download_token -> скачивает firmware.ota.bin (<ingress>/api/firmware/download).
  3. Интеграция move_sensor_ble_ota: POST /api/move_sensor_ble_ota/upload?start=1
     -> датчик прошивается в ближайшее окно пробуждения (BLE через прокси).
  4. Ждёт /api/move_sensor_ble_ota/status = success и новую версию в «Firmware version (BLE)».

Нужно: доступ к HA (http://homeassistant.local:8123) и долгоживущий токен администратора
в secrets.yaml (ключ ha_token). 2026-11-14.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SECRETS = os.path.normpath(os.path.join(HERE, "..", "secrets.yaml"))
DEFAULT_URL = "http://homeassistant.local:8123"
DEFAULT_CONFIG = "move-sensor-ble.yaml"
VERSION_ENTITY_SUFFIX = "_firmware_version_ble"


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def die(msg: str) -> None:
    log("ОШИБКА: " + msg)
    sys.exit(1)


def read_secret(path: str, key: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(rf"^{re.escape(key)}\s*:\s*(.+?)\s*$", line)
                if m:
                    return m.group(1).strip().strip('"').strip("'")
    except OSError as err:
        die(f"не прочитать {path}: {err}")
    die(f"в {path} нет ключа {key}")
    return ""


# ------------------------------------------------------------------ мини-клиент WebSocket
class WS:
    """Минимальный клиент WebSocket (RFC 6455): текстовые кадры, фрагментация, ping/pong."""

    def __init__(self, url: str, headers: dict | None = None, timeout: float = 30.0):
        u = urllib.parse.urlparse(url)
        if u.scheme not in ("ws", "http"):
            die(f"поддерживается только ws:// ({url})")
        host, port = u.hostname, u.port or 80
        path = u.path + (("?" + u.query) if u.query else "")
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = [f"GET {path} HTTP/1.1", f"Host: {host}:{port}", "Upgrade: websocket",
               "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
        for k, v in (headers or {}).items():
            req.append(f"{k}: {v}")
        self.sock.sendall(("\r\n".join(req) + "\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                die(f"WebSocket {path}: соединение закрыто при рукопожатии")
            resp += chunk
        head, self.buf = resp.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status + " ":
            die(f"WebSocket {u.path}: {status}")

    def _recv_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("WebSocket закрыт")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        hdr = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, obj) -> None:
        self._send_frame(0x1, json.dumps(obj).encode())

    def recv(self, timeout: float | None = None):
        self.sock.settimeout(timeout)
        data = b""
        while True:
            b1, b2 = self._recv_exact(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if b2 & 0x80 else None
            payload = self._recv_exact(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:          # ping
                self._send_frame(0xA, payload)
                continue
            if opcode == 0x8:
                raise ConnectionError("WebSocket закрыт сервером")
            if opcode in (0x0, 0x1, 0x2):
                data += payload
                if fin:
                    return json.loads(data.decode())

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
            self.sock.close()
        except OSError:
            pass


# ------------------------------------------------------------------ Home Assistant
class HA:
    def __init__(self, url: str, token: str):
        self.url, self.token = url.rstrip("/"), token
        self._id = 0
        self.ws = WS(self.url.replace("http", "ws", 1) + "/api/websocket")
        if self.ws.recv(10).get("type") != "auth_required":
            die("HA WebSocket: неожиданный ответ")
        self.ws.send({"type": "auth", "access_token": token})
        if self.ws.recv(10).get("type") != "auth_ok":
            die("HA: токен не принят (ha_token)")

    def call(self, msg: dict, timeout: float = 30):
        self._id += 1
        msg = dict(msg, id=self._id)
        self.ws.send(msg)
        end = time.time() + timeout
        while time.time() < end:
            r = self.ws.recv(timeout)
            if r.get("id") == self._id and r.get("type") == "result":
                if not r.get("success"):
                    die(f"HA {msg.get('type')}: {r.get('error')}")
                return r.get("result")
        die(f"HA {msg.get('type')}: таймаут")

    def supervisor(self, endpoint: str, method: str = "get"):
        return self.call({"type": "supervisor/api", "endpoint": endpoint, "method": method})

    def rest(self, path: str, data: bytes | None = None, ctype: str | None = None, timeout: float = 120):
        req = urllib.request.Request(self.url + path, data=data, method="POST" if data is not None else "GET")
        req.add_header("Authorization", "Bearer " + self.token)
        if ctype:
            req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as err:
            die(f"HA {path}: HTTP {err.code} {err.read().decode(errors='replace')[:300]}")


# ------------------------------------------------------------------ ESPHome Builder
class ESPHome:
    def __init__(self, ha: HA):
        addons = ha.supervisor("/addons")
        addons = addons.get("addons", addons) if isinstance(addons, dict) else addons
        slug = next((a["slug"] for a in addons if a["slug"].endswith("_esphome") or a["slug"] == "esphome"), None)
        if not slug:
            die("аддон ESPHome не найден")
        info = ha.supervisor(f"/addons/{slug}/info")
        self.ingress = info.get("ingress_url") or die("у аддона ESPHome нет ingress")
        session = ha.supervisor("/ingress/session", "post")["session"]
        self.cookie = f"ingress_session={session}"
        self.base = ha.url + self.ingress
        self.ws = WS(ha.url.replace("http", "ws", 1) + self.ingress + "ws", {"Cookie": self.cookie})
        self._id = 0

    def cmd(self, command: str, args: dict, timeout: float = 60):
        self._id += 1
        mid = str(self._id)
        self.ws.send({"command": command, "message_id": mid, "args": args})
        end = time.time() + timeout
        while time.time() < end:
            r = self.ws.recv(timeout)
            if str(r.get("message_id")) == mid and ("result" in r or "error" in r):
                if r.get("error"):
                    die(f"ESPHome {command}: {r['error']}")
                return r["result"]
        die(f"ESPHome {command}: таймаут")

    def compile(self, configuration: str, timeout: float = 1800) -> None:
        job = self.cmd("firmware/compile", {"configuration": configuration})
        jid = job["job_id"]
        log(f"сборка {configuration} (задача {jid})...")
        end, last = time.time() + timeout, None
        while time.time() < end:
            j = self.cmd("firmware/get_job", {"job_id": jid})
            st = j.get("status")
            if st not in ("running", "queued", "pending"):
                out = "".join(j.get("output") or [])
                out = re.sub(r"\x1b\[[0-9;]*[A-Za-z]|\\033\[K", "", out).splitlines()
                for line in out:
                    if re.search(r"^(RAM|Flash):", line.strip()):
                        log("  " + line.strip())
                if st != "completed" or j.get("exit_code") not in (0, None):
                    for line in [l for l in out if re.search(r"error|Error|FAILED", l)][-25:] or out[-25:]:
                        print("    " + line)
                    die(f"сборка не удалась: {st}, код {j.get('exit_code')}")
                log("сборка готова")
                return
            if j.get("progress") != last and j.get("progress") is not None:
                last = j.get("progress")
            time.sleep(4)
        die("сборка: таймаут")

    def download(self, configuration: str, file: str = "firmware.ota.bin") -> bytes:
        tok = self.cmd("firmware/download_token", {"configuration": configuration, "file": file})["token"]
        req = urllib.request.Request(self.base + "api/firmware/download?token=" + urllib.parse.quote(tok))
        req.add_header("Cookie", self.cookie)
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        if not data or data[0] != 0xE9:
            die(f"скачанный {file} не похож на образ ESP32 ({len(data)} байт)")
        log(f"скачан {file}: {len(data)} байт")
        return data


# ------------------------------------------------------------------ прошивка через интеграцию
def version_entity(ha: HA) -> str | None:
    states = ha.rest("/api/states")
    for s in states:
        if s["entity_id"].startswith("sensor.") and s["entity_id"].endswith(VERSION_ENTITY_SUFFIX):
            return s["entity_id"]
    return None


def flash(ha: HA, data: bytes, filename: str, start: bool, timeout_min: int) -> None:
    vent = version_entity(ha)
    before = ha.rest(f"/api/states/{vent}")["state"] if vent else None
    q = urllib.parse.urlencode({"filename": filename, "start": "1" if start else "0", "timeout": timeout_min})
    res = ha.rest("/api/move_sensor_ble_ota/upload?" + q, data, "application/octet-stream")
    log(f"загружено в интеграцию: {res.get('path')} ({res.get('size')} байт, проект {res.get('project')})")
    if not start:
        return
    if not res.get("started"):
        die(f"прошивка не запущена: {res.get('start_error')}")
    log(f"ждём окно пробуждения датчика (до {timeout_min} мин); было: {before}")
    end, last = time.time() + timeout_min * 60 + 60, None
    while time.time() < end:
        st = ha.rest("/api/move_sensor_ble_ota/status")
        d = next(iter(st.values())) if isinstance(st, dict) and st else {}
        line = f"{d.get('status')} {d.get('progress')}%"
        if line != last:
            log("  OTA: " + line + (f" ({d.get('error')})" if d.get("error") else ""))
            last = line
        if d.get("status") in ("error", "cancelled"):
            die(f"OTA: {d.get('status')} — {d.get('error')}")
        if d.get("status") == "success":
            break
        time.sleep(3)
    else:
        die("OTA: таймаут")
    # датчик перезагружается; интеграция читает версию в первом окне после перезагрузки
    if vent:
        end = time.time() + 180
        while time.time() < end:
            now = ha.rest(f"/api/states/{vent}")["state"]
            if now and now != before and now not in ("unknown", "unavailable"):
                log(f"датчик подтвердил новую версию: {now}")
                return
            time.sleep(5)
        log("версия ещё не перечитана — проверь «Firmware version (BLE)» после следующего пробуждения")


def main() -> None:
    ap = argparse.ArgumentParser(description="Сборка + BLE OTA для Move Sensor BLE через Home Assistant")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="YAML в /config/esphome (по умолчанию move-sensor-ble.yaml)")
    ap.add_argument("--url", default=os.environ.get("HA_URL", DEFAULT_URL))
    ap.add_argument("--secrets", default=DEFAULT_SECRETS)
    ap.add_argument("--file", help="готовый .ota.bin — без сборки")
    ap.add_argument("--compile-only", action="store_true", help="только собрать и скачать .bin рядом со скриптом")
    ap.add_argument("--no-start", action="store_true", help="загрузить в интеграцию, но не прошивать")
    ap.add_argument("--timeout", type=int, default=180, help="сколько ждать пробуждения датчика, мин")
    a = ap.parse_args()

    token = os.environ.get("HA_TOKEN") or read_secret(a.secrets, "ha_token")
    ha = HA(a.url, token)
    log(f"HA: {a.url} — ок")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if a.file:
        with open(a.file, "rb") as f:
            data = f.read()
        name = os.path.basename(a.file)
    else:
        esp = ESPHome(ha)
        esp.compile(a.config)
        data = esp.download(a.config)
        name = f"{os.path.splitext(a.config)[0]}-{stamp}.ota.bin"
        if a.compile_only:
            out = os.path.join(HERE, name)
            with open(out, "wb") as f:
                f.write(data)
            log(f"сохранено: {out}")
            return
    flash(ha, data, name, start=not a.no_start, timeout_min=a.timeout)
    log("готово")


if __name__ == "__main__":
    main()
