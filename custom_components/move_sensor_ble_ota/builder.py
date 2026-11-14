"""Сборка прошивки в аддоне ESPHome Builder изнутри Home Assistant (2026-11-14).

Путь как у самого HA Core (components/hassio/ingress.py):
  Supervisor API (SUPERVISOR_TOKEN) -> аддон ESPHome, его ingress-токен, ingress-сессия
  -> WebSocket http://supervisor/ingress/<token>/ws с кукой ingress_session и заголовками
     X-Hass-Source: core.ingress / X-Ingress-Path
  -> firmware/compile, firmware/get_job, firmware/download_token
  -> GET .../api/firmware/download?token=... -> firmware.ota.bin (образ ESP32, 0xE9).
Внешний токен и браузер не нужны. Работает только в HA под Supervisor (HAOS / Supervised).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Callable

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

COMPILE_TIMEOUT_S = 30 * 60
POLL_S = 4


class BuildError(Exception):
    """Сборка не удалась (текст — для статуса и лога)."""


def _supervisor() -> tuple[str, str]:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise BuildError("нет SUPERVISOR_TOKEN: HA работает без Supervisor, сборка через аддон недоступна")
    return os.environ.get("SUPERVISOR", "supervisor"), token


async def _sv(session: aiohttp.ClientSession, method: str, path: str, body: dict | None = None) -> dict:
    host, token = _supervisor()
    async with session.request(
        method, f"http://{host}{path}", headers={"Authorization": f"Bearer {token}"},
        json=body, timeout=aiohttp.ClientTimeout(total=30),
    ) as r:
        try:
            js = await r.json(content_type=None)
        except ValueError:
            js = {}
        if r.status != 200 or js.get("result") != "ok":
            raise BuildError(f"Supervisor {path}: {js.get('message') or r.status}")
        return js.get("data") or {}


class _Builder:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.session = async_get_clientsession(hass)
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.base = ""
        self.headers: dict[str, str] = {}
        self._id = 0

    async def connect(self) -> None:
        addons = (await _sv(self.session, "get", "/addons")).get("addons", [])
        addon = next((a for a in addons if a.get("slug", "").endswith("_esphome") or a.get("slug") == "esphome"), None)
        if addon is None:
            raise BuildError("аддон ESPHome Builder не установлен")
        info = await _sv(self.session, "get", f"/addons/{addon['slug']}/info")
        if info.get("state") != "started":
            raise BuildError(f"аддон ESPHome не запущен ({info.get('state')})")
        entry = (info.get("ingress_entry") or "").rstrip("/")
        if not entry:
            raise BuildError("у аддона ESPHome нет ingress")
        token = entry.rsplit("/", 1)[-1]
        # Supervisor требует user_id для ingress-сессии от Core — берём владельца / активного админа
        users = await self.hass.auth.async_get_users()
        user = next((u for u in users if u.is_owner and u.is_active), None) or next(
            (u for u in users if u.is_admin and u.is_active and not u.system_generated), None)
        if user is None:
            raise BuildError("нет активного администратора HA для ingress-сессии")
        sess = (await _sv(self.session, "post", "/ingress/session", {"user_id": user.id})).get("session")
        if not sess:
            raise BuildError("Supervisor не выдал ingress-сессию")
        host, _ = _supervisor()
        self.base = f"http://{host}/ingress/{token}/"
        self.headers = {
            "Cookie": f"ingress_session={sess}",
            "X-Hass-Source": "core.ingress",
            "X-Ingress-Path": entry,
        }
        self.ws = await self.session.ws_connect(
            self.base + "ws", headers=self.headers, heartbeat=30, max_msg_size=0,
            timeout=aiohttp.ClientWSTimeout(ws_close=10) if hasattr(aiohttp, "ClientWSTimeout") else 10,
        )

    async def cmd(self, command: str, args: dict, timeout: float = 60) -> dict:
        assert self.ws is not None
        self._id += 1
        mid = str(self._id)
        await self.ws.send_json({"command": command, "message_id": mid, "args": args})
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise BuildError(f"ESPHome {command}: таймаут")
            msg = await self.ws.receive(timeout=left)
            if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                raise BuildError("ESPHome: соединение закрыто")
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = msg.json()
            if str(data.get("message_id")) == mid and ("result" in data or "error" in data):
                if data.get("error"):
                    raise BuildError(f"ESPHome {command}: {data['error']}")
                return data["result"]

    async def close(self) -> None:
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()


async def build_firmware(
    hass: HomeAssistant, configuration: str, on_progress: Callable[[int], None] | None = None
) -> bytes:
    """Собрать <configuration> в аддоне ESPHome и вернуть firmware.ota.bin."""
    b = _Builder(hass)
    try:
        await b.connect()
        job = await b.cmd("firmware/compile", {"configuration": configuration})
        jid = job["job_id"]
        _LOGGER.info("ESPHome: сборка %s (задача %s)", configuration, jid)
        end = time.monotonic() + COMPILE_TIMEOUT_S
        while True:
            if time.monotonic() > end:
                raise BuildError("сборка: таймаут")
            j = await b.cmd("firmware/get_job", {"job_id": jid})
            st = j.get("status")
            if on_progress and isinstance(j.get("progress"), int):
                on_progress(j["progress"])
            if st not in ("running", "queued", "pending"):
                if st != "completed" or j.get("exit_code") not in (0, None):
                    out = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", "".join(j.get("output") or [])).splitlines()
                    tail = [l for l in out if re.search(r"error|Error|FAILED", l)][-8:] or out[-8:]
                    _LOGGER.error("ESPHome: сборка %s не удалась:\n%s", configuration, "\n".join(tail))
                    raise BuildError(f"сборка не удалась ({st}, код {j.get('exit_code')}): "
                                     + (tail[-1].strip() if tail else ""))
                break
            await asyncio.sleep(POLL_S)
        tok = (await b.cmd("firmware/download_token", {"configuration": configuration, "file": "firmware.ota.bin"}))["token"]
        async with b.session.get(
            b.base + "api/firmware/download", params={"token": tok}, headers=b.headers,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as r:
            if r.status != 200:
                raise BuildError(f"скачивание firmware.ota.bin: HTTP {r.status}")
            data = await r.read()
        if not data or data[0] != 0xE9:
            raise BuildError(f"скачанный firmware.ota.bin не похож на образ ESP32 ({len(data)} байт)")
        _LOGGER.info("ESPHome: %s собран, %d байт", configuration, len(data))
        return data
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise BuildError(f"связь с аддоном ESPHome: {type(err).__name__}: {err}") from err
    finally:
        await b.close()
