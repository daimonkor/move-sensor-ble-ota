"""v1.29: новая версия интеграции видна в Marketplace сразу, без «Обновить информацию».

Встроенный Marketplace сам перепроверяет установленные пользовательские репозитории раз в 48 ч.
Раз в час (и через 2 мин после старта HA) смотрим последний релиз нашего репозитория на GitHub;
если он новее установленной версии, а Marketplace о нём ещё не знает, обновляем запись
репозитория в Marketplace тем же вызовом, что и пункт меню «Обновить информацию»
(marketplace/repository/refresh -> repository.update_repository(force=True)). После этого
сущность обновления интеграции и подсветка на панели появляются сами.

Marketplace не даёт публичного API для этого: всё в try/except, при любой ошибке — запись в
лог (debug) и ждём следующей проверки; без Marketplace (HACS и т. п.) просто ничего не делаем.

v1.29.2: показ новой версии — всегда; установка — только с переключателем «Auto-update integration»
(по умолчанию выключен). Ставит штатной update.install сущности Marketplace; перезапуск HA — за
пользователем (Marketplace сам показывает «нужен перезапуск», плюс наше уведомление).
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import re

import aiohttp

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

REPO = "daimonkor/move-sensor-ble-ota"
CHECK_EVERY = timedelta(hours=1)
FIRST_CHECK_S = 120
_FLAG = f"{DOMAIN}_selfupdate"
_STATE = f"{DOMAIN}_selfupdate_state"
SIGNAL_AUTO = f"{DOMAIN}_auto_update_changed"
NOTIFY_ID = f"{DOMAIN}_auto_updated"


def _state(hass: HomeAssistant) -> dict:
    return hass.data.setdefault(_STATE, {"auto_install": False, "installed": None, "store": None})


def auto_install(hass: HomeAssistant) -> bool:
    return bool(_state(hass)["auto_install"])


def update_entity_id(hass: HomeAssistant) -> str | None:
    """Сущность обновления этой интеграции в Marketplace (unique_id = id репозитория на GitHub)."""
    try:
        entries = hass.config_entries.async_loaded_entries("marketplace")
        repo = entries[0].runtime_data.repositories.get_by_full_name(REPO) if entries else None
        if repo is not None:
            return er.async_get(hass).async_get_entity_id("update", "marketplace", str(repo.data.id))
    except Exception:  # noqa: BLE001
        pass
    return None


async def async_set_auto_install(hass: HomeAssistant, on: bool) -> None:
    st = _state(hass)
    st["auto_install"] = on
    if st["store"] is not None:
        await st["store"].async_save({"auto_install": on})
    async_dispatcher_send(hass, SIGNAL_AUTO)
    if on and st["installed"]:
        hass.async_create_task(async_check(hass, st["installed"]))   # сразу, не ждать часа


async def _async_auto_install(hass: HomeAssistant, installed: str) -> None:
    if not auto_install(hass) or (eid := update_entity_id(hass)) is None:
        return
    state = hass.states.get(eid)
    latest = install_target(state.state if state else None, dict(state.attributes) if state else {}, installed)
    if latest is None:
        return
    _LOGGER.info("Auto-update: installing Move Sensor BLE OTA %s (installed %s)", latest, installed)
    await hass.services.async_call("update", "install", {"entity_id": eid}, blocking=True)
    persistent_notification.async_create(
        hass,
        f"Move Sensor BLE OTA {latest} was installed automatically (was v{installed}). "
        "Restart Home Assistant to start using it.",
        title="Move Sensor BLE OTA updated",
        notification_id=NOTIFY_ID,
    )


def install_target(state: str | None, attrs: dict, installed: str) -> str | None:
    """Версия, которую поставить: сущность Marketplace говорит «есть обновление», не ставит уже,
    и её версия новее установленной. Иначе None (чистая функция — для тестов)."""
    if state != "on" or attrs.get("in_progress"):
        return None
    latest = attrs.get("latest_version")
    if not latest or not _vkey(installed) or _vkey(latest) <= _vkey(installed):
        return None
    return str(latest)


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
        await _async_refresh(hass, tag, installed)
        await _async_auto_install(hass, installed)
    except Exception as err:  # noqa: BLE001 — внутренности Marketplace могут поменяться
        _LOGGER.debug("Marketplace refresh / auto-update failed: %s: %s", type(err).__name__, err)


async def _async_refresh(hass: HomeAssistant, tag: str, installed: str) -> None:
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


async def async_start(hass: HomeAssistant, installed: str) -> None:
    """Один раз на запуск HA (не на каждую запись датчика)."""
    if hass.data.get(_FLAG):
        return
    hass.data[_FLAG] = ()
    st = _state(hass)
    st["installed"] = installed
    st["store"] = Store(hass, 1, f"{DOMAIN}.selfupdate")
    st["auto_install"] = bool(((await st["store"].async_load()) or {}).get("auto_install"))

    async def run(_now=None) -> None:
        await async_check(hass, installed)

    unsub_first = async_call_later(hass, FIRST_CHECK_S, run)
    unsub_every = async_track_time_interval(hass, run, CHECK_EVERY)
    hass.data[_FLAG] = (unsub_first, unsub_every)


@callback
def async_stop(hass: HomeAssistant) -> None:
    for unsub in hass.data.pop(_FLAG, None) or ():
        unsub()
