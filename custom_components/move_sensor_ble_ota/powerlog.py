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


# ---------------------------------------------------------------- отчёт о сбое (v1.32)
# Вывод по записи журнала и тому, что интеграция видела вокруг сбоя. ctx (всё необязательно):
#   others_min / others_max — сколько других BLE-устройств слышал Home Assistant за минуту во время паузы
#   packets, lost            — пакеты датчика за час до сбоя: принято / потеряно (по номерам пакетов)
#   rssi_median, temperature — сигнал и температура перед сбоем
#   motion_window, climate_window, climate_interval — настройки на тот момент
VERDICTS = ("usb_powered", "low_battery", "brownout", "crash", "dip", "cut_under_load", "cut_asleep", "unknown")
_LOAD_STAGES = ("awake", "transmitting", "led_on", "window_done", "ble_client", "boot", "battery_measured")

_D = {
    "en": {
        "usb_powered": "A computer was on USB, so the battery is not involved: check the board, the USB socket and the cable.",
        "low_battery": "The battery was low ({v} V): this looks like discharge. Charge it and watch the percentage.",
        "brownout": "The voltage sagged slowly (brownout): a weak battery or a high-resistance contact in the power path.",
        "crash": "The firmware crashed or hung — this is not a power failure. Connect USB and look at the log.",
        "dip": "Power dipped for a moment and came back: a loose contact (battery connector, power switch, the wire to the BAT+ pad) or a tug on the USB cable.",
        "cut_under_load": "Power was cut while the sensor was working ({stage}): either the load tripped the battery protection or a contact opened at that moment. Repeat the power test; if it fails again, add a 100–220 µF capacitor after the power switch.",
        "cut_asleep": "Power was cut while the sensor slept, so the radio and the LED are not the cause: look for a loose contact or a short in the battery path (battery protection, connector, power switch, the wire to the BAT+ pad).",
        "unknown": "Not enough data for a conclusion.",
        "rx_ok": "Reception was fine: Home Assistant heard {n} other Bluetooth devices during the silence, so it was the sensor that went quiet.",
        "rx_down": "Home Assistant heard no other Bluetooth devices during the silence: the Bluetooth proxy or the router may have been down as well.",
        "rx_unknown": "No reception data for that time (Home Assistant was restarted or the report is older than a day).",
        "before": "The hour before: {p} packets, {l} lost, signal {r} dBm, {t} °C; windows {mw}/{cw} ms, climate every {ci} min.",
        "check": "Before plugging in USB: switch the power off for 3 s and on. If the LED blinks, the battery protection had tripped (a short somewhere). If not, measure between «+» and «−» of the PIR header: about 4 V — power does not reach the board (the BAT+ wire); about 0 V — battery, connector or power switch.",
        "none": "The sensor has logged no power failures yet.",
    },
    "ru": {
        "usb_powered": "Компьютер был на USB, значит банка ни при чём: проверь плату, гнездо USB и кабель.",
        "low_battery": "Батарея была разряжена ({v} В): похоже на разряд. Заряди и понаблюдай за процентом.",
        "brownout": "Напряжение просело медленно (brownout): слабая банка или контакт с большим сопротивлением в цепи питания.",
        "crash": "Прошивка упала или зависла — это не сбой питания. Подключи USB и посмотри лог.",
        "dip": "Питание провалилось на мгновение и вернулось: плавающий контакт (разъём банки, выключатель, провод к площадке BAT+) или рывок кабеля USB.",
        "cut_under_load": "Питание пропало, когда датчик работал ({stage}): либо нагрузка выбила защиту банки, либо в этот момент разомкнулся контакт. Повтори тест питания; если упадёт снова — поставь конденсатор 100–220 мкФ после выключателя.",
        "cut_asleep": "Питание пропало, когда датчик спал, значит радио и светодиод ни при чём: ищи плавающий контакт или замыкание в цепи банки (плата защиты, разъём, выключатель, провод к площадке BAT+).",
        "unknown": "Данных для вывода не хватает.",
        "rx_ok": "Приём работал: за время тишины Home Assistant слышал {n} других Bluetooth-устройств, то есть замолчал именно датчик.",
        "rx_down": "За время тишины Home Assistant не слышал и другие Bluetooth-устройства: мог упасть прокси или роутер.",
        "rx_unknown": "Данных о приёме за это время нет (Home Assistant перезапускался или отчёт старше суток).",
        "before": "За час до сбоя: {p} пакетов, потеряно {l}, сигнал {r} dBm, {t} °C; окна {mw}/{cw} мс, климат раз в {ci} мин.",
        "check": "До подключения USB: выключи питание на 3 с и включи. Мигнул светодиод — была закрыта защита банки (где-то коротит). Не мигнул — измерь между «+» и «−» гребёнки PIR: около 4 В — питание не доходит до платы (провод к BAT+); около 0 В — банка, разъём или выключатель.",
        "none": "Датчик ещё не записал ни одного сбоя питания.",
    },
    "uk": {
        "usb_powered": "Комп'ютер був на USB, тож батарея ні до чого: перевір плату, гніздо USB і кабель.",
        "low_battery": "Батарея була розряджена ({v} В): схоже на розряд. Заряди й поспостерігай за відсотком.",
        "brownout": "Напруга просіла повільно (brownout): слабка батарея або контакт із великим опором у колі живлення.",
        "crash": "Прошивка впала або зависла — це не збій живлення. Підключи USB і подивися лог.",
        "dip": "Живлення провалилося на мить і повернулося: плаваючий контакт (роз'єм батареї, вимикач, дріт до площадки BAT+) або ривок кабелю USB.",
        "cut_under_load": "Живлення зникло, коли датчик працював ({stage}): або навантаження вибило захист батареї, або в цей момент розімкнувся контакт. Повтори тест живлення; якщо впаде знову — постав конденсатор 100–220 мкФ після вимикача.",
        "cut_asleep": "Живлення зникло, коли датчик спав, тож радіо і світлодіод ні до чого: шукай плаваючий контакт або замикання в колі батареї (плата захисту, роз'єм, вимикач, дріт до площадки BAT+).",
        "unknown": "Даних для висновку бракує.",
        "rx_ok": "Прийом працював: за час тиші Home Assistant чув {n} інших Bluetooth-пристроїв, тобто замовк саме датчик.",
        "rx_down": "За час тиші Home Assistant не чув й інші Bluetooth-пристрої: міг упасти проксі або роутер.",
        "rx_unknown": "Даних про прийом за цей час немає (Home Assistant перезапускався або звіт старший за добу).",
        "before": "За годину до збою: {p} пакетів, втрачено {l}, сигнал {r} dBm, {t} °C; вікна {mw}/{cw} мс, клімат раз на {ci} хв.",
        "check": "До підключення USB: вимкни живлення на 3 с і ввімкни. Блимнув світлодіод — був закритий захист батареї (десь коротить). Не блимнув — виміряй між «+» і «−» гребінки PIR: близько 4 В — живлення не доходить до плати (дріт до BAT+); близько 0 В — батарея, роз'єм або вимикач.",
        "none": "Датчик ще не записав жодного збою живлення.",
    },
}


def verdict(entry: dict | None) -> str:
    """Код вывода по записи журнала (один из VERDICTS)."""
    if not entry:
        return "unknown"
    kind, stage = entry.get("kind"), entry.get("stage")
    if kind in ("crash", "watchdog"):
        return "crash"
    if entry.get("usb_host"):
        return "usb_powered"
    v = entry.get("battery_v")
    if v is not None and v < 3.6:
        return "low_battery"
    if kind == "brownout":
        return "brownout"
    if kind == "power_dip":
        return "dip"
    if kind == "power_removed":
        if stage in ("asleep", "service_mode"):
            return "cut_asleep" if stage == "asleep" else "cut_under_load"
        if stage in _LOAD_STAGES:
            return "cut_under_load"
    return "unknown"


def report(entry: dict | None, ctx: dict | None = None, lang: str = "en") -> dict:
    """Отчёт о последнем сбое: код вывода и строки на языке Home Assistant."""
    code = (lang or "en").split("-")[0].lower()
    d = _D.get(code, _D["en"])
    t = _T.get(code, _T["en"])
    lg = "en" if d is _D["en"] else "x"
    if not entry:
        return {"verdict": "none", "lines": [d["none"]], "text": d["none"]}
    ctx = ctx or {}
    v = verdict(entry)
    lines = [text(entry, lang)]
    lines.append(d[v].format(v=_num(entry["battery_v"], lg) if entry.get("battery_v") is not None else "?",
                             stage=t.get(entry.get("stage"), t["unknown"])))
    if ctx.get("others_max") is None:
        lines.append(d["rx_unknown"])
    elif ctx["others_max"] > 0:
        lines.append(d["rx_ok"].format(n=ctx["others_max"]))
    else:
        lines.append(d["rx_down"])
    if ctx.get("packets"):
        def show(x, nd=0):
            return "?" if x is None else (f"{x:.{nd}f}" if lg == "en" else f"{x:.{nd}f}".replace(".", ","))
        lines.append(d["before"].format(p=ctx["packets"], l=ctx.get("lost", 0), r=show(ctx.get("rssi_median")),
                                        t=show(ctx.get("temperature"), 1), mw=show(ctx.get("motion_window")),
                                        cw=show(ctx.get("climate_window")), ci=show(ctx.get("climate_interval"))))
    if v in ("cut_asleep", "cut_under_load", "dip", "brownout", "unknown"):
        lines.append(d["check"])
    return {"verdict": v, "lines": lines, "text": "\n".join(lines)}


def test_result(res: dict, lang: str = "en") -> str:
    """Итог теста питания одной строкой."""
    code = (lang or "en").split("-")[0].lower()
    s = {
        "en": ("passed", "failed", "{m} min, {p} packets of about {e} expected, {l} lost, {r} reboots, {n} new power-log entries, battery {a}–{b} %"),
        "ru": ("пройден", "не пройден", "{m} мин, {p} пакетов из примерно {e}, потеряно {l}, перезагрузок {r}, новых записей о сбое {n}, батарея {a}–{b} %"),
        "uk": ("пройдено", "не пройдено", "{m} хв, {p} пакетів із приблизно {e}, втрачено {l}, перезавантажень {r}, нових записів про збій {n}, батарея {a}–{b} %"),
    }.get(code)
    if s is None:
        s = ("passed", "failed", "{m} min, {p} packets of about {e} expected, {l} lost, {r} reboots, {n} new power-log entries, battery {a}–{b} %")
    body = s[2].format(m=res.get("minutes"), p=res.get("packets", 0), e=res.get("expected", 0), l=res.get("lost", 0),
                       r=res.get("reboots", 0), n=res.get("new_entries", 0),
                       a=res.get("battery_min", "?"), b=res.get("battery_max", "?"))
    return f"{s[0] if res.get('passed') else s[1]}: {body}"
