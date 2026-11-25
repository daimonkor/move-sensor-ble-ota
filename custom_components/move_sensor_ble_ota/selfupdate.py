"""v1.29: новая версия интеграции видна в Marketplace сразу, без «Обновить информацию».

Встроенный Marketplace сам перепроверяет установленные пользовательские репозитории раз в 48 ч.
Раз в час (и через 2 мин после старта HA) смотрим последний релиз нашего репозитория на GitHub;
если он новее установленной версии, а Marketplace о нём ещё не знает, обновляем запись
репозитория в Marketplace тем же вызовом, что и пункт меню «Обновить информацию»
(marketplace/repository/refresh -> repository.update_repository(force=True)). После этого
сущность обновления интеграции и подсветка на панели появляются сами.

Marketplace не даёт публичного API для этого: всё в try/except, при любой ошибке — запись в
лог (debug) и ждём следующей проверки; без Marketplace (HACS и т. п.) просто ничего не делаем.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import re

import aiohttp

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

REPO = "daimonkor/move-sensor-ble-ota"
CHECK_EVERY = timedelta(hours=1)
FIRST_CHECK_S = 120
_FLAG = f"{DOMAIN}_selfupdate"


def _vkey(v: str | None) -> tuple:
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", (v or "").strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else ()


async def _latest_release(hass: HomeAssistant) -> str | None:
    session = async_get_clientsession(hass)
    async with session.get(
        f"https://api.github.com/repos/{REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json"},
        timeout=aiohttp.ClientTimeout(total=20),
    ) as r:
        if r.status != 200:
            _LOGGER.debug("GitHub latest release: HTTP %s", r.status)
            return None
        js = await r.json(content_type=None)
    if js.get("draft") or js.get("prerelease"):
        return None
    return js.get("tag_name")


async def async_check(hass: HomeAssistant, installed: str) -> None:
    try:
        tag = await _latest_release(hass)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
        _LOGGER.debug("GitHub latest release: %s", err)
        return
    if not tag or not _vkey(installed) or _vkey(tag) <= _vkey(installed):
        return
    try:
        entries = hass.config_entries.async_loaded_entries("marketplace")
        if not entries:
            return
        mp = entries[0].runtime_data
        repo = mp.repositories.get_by_full_name(REPO)
        if repo is None:
            return
        known = getattr(repo.data, "last_version", None)
        if known and _vkey(str(known)) >= _vkey(tag):
            return   # Marketplace уже знает о новой версии
        _LOGGER.info("Move Sensor BLE OTA %s is released (installed %s) — refreshing it in the Marketplace",
                     tag, installed)
        await repo.update_repository(ignore_issues=True, force=True)
        await mp.data.async_write()
    except Exception as err:  # noqa: BLE001 — внутренности Marketplace могут поменяться
        _LOGGER.debug("Marketplace refresh failed: %s: %s", type(err).__name__, err)


@callback
def async_start(hass: HomeAssistant, installed: str) -> None:
    """Один раз на запуск HA (не на каждую запись датчика)."""
    if hass.data.get(_FLAG):
        return

    async def run(_now=None) -> None:
        await async_check(hass, installed)

    unsub_first = async_call_later(hass, FIRST_CHECK_S, run)
    unsub_every = async_track_time_interval(hass, run, CHECK_EVERY)
    hass.data[_FLAG] = (unsub_first, unsub_every)


@callback
def async_stop(hass: HomeAssistant) -> None:
    for unsub in hass.data.pop(_FLAG, None) or ():
        unsub()
