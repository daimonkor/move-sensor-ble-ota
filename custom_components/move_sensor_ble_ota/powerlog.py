"""Журнал сбоев питания датчика (прошивка 1.10.0+, BLE-команда 0x17).

Датчик на каждом пробуждении и перед сном сохраняет своё состояние во флеш, а внутри пробуждения ставит
метки стадий в память часов (RTC). После загрузки по power on / brownout / panic / watchdog он кладёт это
в журнал на 8 записей. Здесь — разбор записи (16 байт) и понятный человеку текст (RU / UK / EN).
Формат записи: версия, номер u16, esp_reset_reason, причина ROM, bit7 «RTC цела» | метка стадии,
состояние 8 байт (стадия | причина << 4, флаги, батарея и A0 по 10 мВ, wake u16, sleep_s u16),
«состояние было», резерв.
"""

from __future__ import annotations

from datetime import datetime

ENTRY_SIZE = 16
LOG_SIZE = 8

STAGES = {1: "boot", 2: "battery_measured", 3: "awake", 4: "transmitting", 5: "led_on", 6: "window_done",
          7: "ble_client", 8: "asleep", 9: "service_mode"}
CAUSES = {0: "cold_boot", 1: "timer", 2: "motion", 3: "service_switch"}
SLEEP_KINDS = {0: None, 1: "until_motion", 2: "timer", 3: "until_pir_release"}
# esp_reset_reason_t -> вид события
KINDS = {1: "power", 9: "brownout", 4: "crash", 5: "watchdog", 6: "watchdog", 7: "watchdog"}


def decode(raw: bytes) -> dict | None:
    """Запись журнала -> словарь; None — не запись (короткая или неизвестная версия)."""
    if len(raw) < ENTRY_SIZE or raw[0] != 1:
        return None
    rtc_kept = bool(raw[5] & 0x80)
    rtc_stage = raw[5] & 0x7F
    has_state = bool(raw[14])
    st = raw[6:14]
    flags = st[1]
    saved_stage = st[0] & 0x0F if has_state else 0
    # где застал сбой: метка в RTC точнее (если уцелела), иначе последнее сохранённое во флеш
    stage = rtc_stage if rtc_kept and rtc_stage else saved_stage
    kind = KINDS.get(raw[3], "other")
    if kind == "power":
        kind = "power_dip" if rtc_kept else "power_removed"
    return {
        "seq": raw[1] | (raw[2] << 8),
        "kind": kind,
        "reset": raw[3],
        "reset_rom": raw[4],
        "rtc_kept": rtc_kept,
        "stage": STAGES.get(stage, "unknown"),
        "saved_stage": STAGES.get(saved_stage, "unknown"),
        "wake_cause": CAUSES.get((st[0] >> 4) & 0x0F, "unknown") if has_state else None,
        "usb_host": bool(flags & 0x01),
        "charging": bool(flags & 0x02),
        "occupied": bool(flags & 0x04),
        "battery_low": bool(flags & 0x10),
        "sleep_kind": SLEEP_KINDS.get((flags >> 5) & 3),
        "service_mode": bool(flags & 0x80),
        "battery_v": round(2.5 + st[2] / 100, 2) if has_state and st[2] else None,
        "a0_v": round(st[3] / 100, 2) if has_state and st[3] else None,
        "wake": (st[4] | (st[5] << 8)) if has_state else None,
        "sleep_s": (st[6] | (st[7] << 8)) if has_state else None,
    }


def state(entry: dict | None) -> str | None:
    """Короткое состояние сущности (английское, как имена сущностей)."""
    if not entry:
        return None
    return f"{entry['kind'].replace('_', ' ')}: {entry['stage'].replace('_', ' ')}"


_T = {
    "en": {
        "power_removed": "power fully removed", "power_dip": "short power dip", "brownout": "voltage sag (brownout)",
        "crash": "firmware crash", "watchdog": "watchdog reset", "other": "reset",
        "boot": "right after waking up, before the battery measurement",
        "battery_measured": "while starting up: battery measured, radio not started",
        "awake": "awake, radio starting", "transmitting": "while transmitting a packet", "led_on": "while the LED was on",
        "window_done": "after the advertising window, before sleep", "ble_client": "while Home Assistant was connected over BLE",
        "asleep": "asleep or at the very moment of waking up", "service_mode": "in service mode (Wi-Fi)", "unknown": "stage unknown",
        "cold_boot": "after a restart", "timer": "woken by timer", "motion": "woken by motion", "service_switch": "woken by the service switch",
        "until_motion": "waiting for motion", "sleep_timer": "timer", "until_pir_release": "waiting for the PIR to release",
        "next_wake": "next wake in {s} s", "battery": "battery {v} V", "a0": "A0 {v} V", "wake": "wake #{n}",
        "usb": "USB computer connected", "no_usb": "no USB computer", "charging": "charging",
        "silent": "no packets for {m} min", "time_unknown": "time unknown",
    },
    "ru": {
        "power_removed": "питание пропало полностью", "power_dip": "короткий провал питания", "brownout": "просадка напряжения (brownout)",
        "crash": "сбой прошивки", "watchdog": "сброс по watchdog", "other": "сброс",
        "boot": "сразу после пробуждения, до замера батареи",
        "battery_measured": "на старте: батарея измерена, радио ещё не включено",
        "awake": "проснулся, радио запускается", "transmitting": "во время передачи пакета", "led_on": "когда горел LED",
        "window_done": "после окна рекламы, перед сном", "ble_client": "когда Home Assistant был подключён по BLE",
        "asleep": "во сне или в первый момент пробуждения", "service_mode": "в сервисном режиме (Wi-Fi)", "unknown": "стадия неизвестна",
        "cold_boot": "после перезапуска", "timer": "разбудил таймер", "motion": "разбудило движение", "service_switch": "разбудил сервисный переключатель",
        "until_motion": "ждал движения", "sleep_timer": "по таймеру", "until_pir_release": "ждал отпускания PIR",
        "next_wake": "пробуждение через {s} с", "battery": "батарея {v} В", "a0": "A0 {v} В", "wake": "пробуждение №{n}",
        "usb": "компьютер на USB", "no_usb": "без USB-компьютера", "charging": "заряжался",
        "silent": "без пакетов {m} мин", "time_unknown": "время неизвестно",
    },
    "uk": {
        "power_removed": "живлення зникло повністю", "power_dip": "короткий провал живлення", "brownout": "просідання напруги (brownout)",
        "crash": "збій прошивки", "watchdog": "скидання по watchdog", "other": "скидання",
        "boot": "одразу після пробудження, до заміру батареї",
        "battery_measured": "на старті: батарею виміряно, радіо ще не ввімкнено",
        "awake": "прокинувся, радіо запускається", "transmitting": "під час передачі пакета", "led_on": "коли горів LED",
        "window_done": "після вікна реклами, перед сном", "ble_client": "коли Home Assistant був підключений по BLE",
        "asleep": "уві сні або в перший момент пробудження", "service_mode": "у сервісному режимі (Wi-Fi)", "unknown": "стадія невідома",
        "cold_boot": "після перезапуску", "timer": "розбудив таймер", "motion": "розбудив рух", "service_switch": "розбудив сервісний перемикач",
        "until_motion": "чекав руху", "sleep_timer": "за таймером", "until_pir_release": "чекав відпускання PIR",
        "next_wake": "пробудження через {s} с", "battery": "батарея {v} В", "a0": "A0 {v} В", "wake": "пробудження №{n}",
        "usb": "комп'ютер на USB", "no_usb": "без USB-комп'ютера", "charging": "заряджався",
        "silent": "без пакетів {m} хв", "time_unknown": "час невідомий",
    },
}


def _num(v: float, lang: str) -> str:
    s = f"{v:.2f}"
    return s if lang == "en" else s.replace(".", ",")


def _hm(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m %H:%M")
    except ValueError:
        return None


def text(entry: dict, lang: str = "en") -> str:
    """Одна строка журнала на языке Home Assistant."""
    t = _T.get((lang or "en").split("-")[0].lower(), _T["en"])
    lg = "en" if t is _T["en"] else "x"
    parts: list[str] = []
    before, after = _hm(entry.get("before")), _hm(entry.get("after"))
    if before and after:
        parts.append(f"{before} → {after[6:] if after[:5] == before[:5] else after}")
    elif after:
        parts.append(f"… → {after}")
    else:
        parts.append(t["time_unknown"])
    parts.append(t.get(entry.get("kind"), t["other"]))
    where = t.get(entry.get("stage"), t["unknown"])
    extra = []
    if entry.get("stage") in ("awake", "transmitting", "led_on", "window_done", "boot", "battery_measured") \
            and entry.get("wake_cause"):
        extra.append(t.get(entry["wake_cause"], ""))
    if entry.get("stage") == "asleep":
        sk = entry.get("sleep_kind")
        if sk:
            extra.append(t["sleep_timer" if sk == "timer" else sk])
        s = entry.get("sleep_s")
        if s is not None and s < 65535:
            extra.append(t["next_wake"].format(s=s))
    extra = [x for x in extra if x]
    parts.append(f"{where} ({', '.join(extra)})" if extra else where)
    if entry.get("battery_v") is not None:
        b = t["battery"].format(v=_num(entry["battery_v"], lg))
        if entry.get("a0_v") is not None:
            b += f" ({t['a0'].format(v=_num(entry['a0_v'], lg))})"
        parts.append(b)
    if entry.get("wake") is not None:
        parts.append(t["wake"].format(n=entry["wake"]))
    parts.append(t["usb"] if entry.get("usb_host") else t["no_usb"])
    if entry.get("charging"):
        parts.append(t["charging"])
    if entry.get("before") and entry.get("after"):
        try:
            m = round((datetime.fromisoformat(entry["after"]) - datetime.fromisoformat(entry["before"])).total_seconds() / 60)
            if m >= 1:
                parts.append(t["silent"].format(m=m))
        except ValueError:
            pass
    return " · ".join(parts)
