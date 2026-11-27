"""Power-loss log (firmware 1.10.0+, integration 1.31): entry decoding and the human-readable line.

  python3 -m pytest -q tests
"""
from __future__ import annotations

import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "powerlog", os.path.join(ROOT, "custom_components", "move_sensor_ble_ota", "powerlog.py"))
powerlog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(powerlog)


def _entry(seq=3, reset=1, rtc=0x00, stage=8, cause=2, flags=(1 << 5) | 0x04, vb=156, a0=200, wake=47, sleep_s=293,
           has_state=1) -> bytes:
    st = bytes([stage | (cause << 4), flags, vb, a0, wake & 0xFF, wake >> 8, sleep_s & 0xFF, sleep_s >> 8])
    return bytes([1, seq & 0xFF, seq >> 8, reset, 1, rtc]) + st + bytes([has_state, 0])


def test_decode_power_removed_asleep():
    e = powerlog.decode(_entry())
    assert e["seq"] == 3 and e["kind"] == "power_removed" and e["stage"] == "asleep"
    assert e["wake_cause"] == "motion" and e["sleep_kind"] == "until_motion" and e["occupied"]
    assert e["battery_v"] == 4.06 and e["a0_v"] == 2.0 and e["wake"] == 47 and e["sleep_s"] == 293
    assert not e["usb_host"] and not e["rtc_kept"]
    assert powerlog.state(e) == "power removed: asleep"


def test_rtc_mark_wins_when_kept():
    # flash says «awake», the RTC mark says the LED was on -> the mark is more precise
    e = powerlog.decode(_entry(rtc=0x80 | 5, stage=3, flags=0x01))
    assert e["kind"] == "power_dip" and e["stage"] == "led_on" and e["saved_stage"] == "awake" and e["usb_host"]


def test_rtc_mark_ignored_when_lost():
    e = powerlog.decode(_entry(rtc=5, stage=3))
    assert e["stage"] == "awake" and e["kind"] == "power_removed"


def test_other_kinds_and_missing_state():
    assert powerlog.decode(_entry(reset=9))["kind"] == "brownout"
    assert powerlog.decode(_entry(reset=4))["kind"] == "crash"
    assert powerlog.decode(_entry(reset=6))["kind"] == "watchdog"
    e = powerlog.decode(_entry(has_state=0))
    assert e["stage"] == "unknown" and e["battery_v"] is None and e["wake"] is None


def test_bad_input():
    assert powerlog.decode(b"") is None
    assert powerlog.decode(bytes(16)) is None          # version 0
    assert powerlog.decode(_entry()[:10]) is None
    assert powerlog.state(None) is None


def test_text_languages_and_times():
    e = powerlog.decode(_entry())
    e.update(before="2026-11-27T23:17:38+02:00", after="2026-11-27T23:40:26+02:00")
    ru = powerlog.text(e, "ru")
    assert ru.startswith("27.11 23:17 → 23:40 · питание пропало полностью · во сне")
    assert "батарея 4,06 В (A0 2,00 В)" in ru and "пробуждение №47" in ru and "без пакетов 23 мин" in ru
    en = powerlog.text(e, "en-GB")
    assert "power fully removed" in en and "battery 4.06 V" in en and "no packets for 23 min" in en
    assert "живлення зникло повністю" in powerlog.text(e, "uk")
    assert powerlog.text(e, "de") == powerlog.text(e, "en")   # unknown language -> English
    del e["before"], e["after"]
    assert powerlog.text(e, "en").startswith("time unknown")


# ---- v1.32: verdict, failure report, power-test summary
def test_verdicts():
    d = powerlog.decode
    assert powerlog.verdict(d(_entry())) == "cut_asleep"
    assert powerlog.verdict(d(_entry(stage=4))) == "cut_under_load"                 # flash says «transmitting»
    assert powerlog.verdict(d(_entry(rtc=0x80 | 5, stage=3))) == "dip"              # RTC kept -> short dip
    assert powerlog.verdict(d(_entry(flags=0x01))) == "usb_powered"
    assert powerlog.verdict(d(_entry(vb=100))) == "low_battery"                     # 3.50 V
    assert powerlog.verdict(d(_entry(reset=9))) == "brownout"
    assert powerlog.verdict(d(_entry(reset=4))) == "crash"
    assert powerlog.verdict(d(_entry(reset=4, flags=0x01))) == "crash"              # a crash is a crash, USB or not
    assert powerlog.verdict(d(_entry(has_state=0))) == "unknown"
    assert powerlog.verdict(None) == "unknown"
    assert set(powerlog.VERDICTS) >= {powerlog.verdict(d(_entry(stage=s))) for s in range(1, 10)}


def test_report_lines():
    e = powerlog.decode(_entry())
    e.update(before="2026-11-27T23:17:38+02:00", after="2026-11-27T23:40:26+02:00")
    ctx = {"others_max": 14, "others_min": 9, "packets": 12, "lost": 0, "rssi_median": -78, "temperature": 11.3,
           "motion_window": 1500, "climate_window": 2500, "climate_interval": 5}
    r = powerlog.report(e, ctx, "ru")
    assert r["verdict"] == "cut_asleep" and r["text"] == "\n".join(r["lines"]) and len(r["lines"]) == 5
    assert r["lines"][0].startswith("27.11 23:17 → 23:40")
    assert "радио и светодиод ни при чём" in r["lines"][1]
    assert "слышал 14 других" in r["lines"][2]
    assert "12 пакетов, потеряно 0, сигнал -78 dBm, 11,3 °C; окна 1500/2500 мс, климат раз в 5 мин" in r["lines"][3]
    assert "До подключения USB" in r["lines"][4]
    assert "no other Bluetooth devices" in powerlog.report(e, {"others_max": 0}, "en")["lines"][2]
    assert "No reception data" in powerlog.report(e, {}, "en")["lines"][2]
    usb = powerlog.report(powerlog.decode(_entry(flags=0x01)), {}, "en")
    assert usb["verdict"] == "usb_powered" and len(usb["lines"]) == 3            # no «measure it» hint for USB
    for lang in ("en", "ru", "uk", "de"):
        for stage in range(1, 10):
            for reset in (1, 4, 6, 9):
                assert powerlog.report(powerlog.decode(_entry(stage=stage, reset=reset)), ctx, lang)["text"]


def test_report_without_entries():
    r = powerlog.report(None, None, "ru")
    assert r["verdict"] == "none" and "ни одного сбоя" in r["text"]


def test_power_test_summary():
    res = {"passed": True, "minutes": 10, "packets": 19, "expected": 20, "lost": 1, "reboots": 0, "new_entries": 0,
           "battery_min": 86, "battery_max": 87}
    assert powerlog.test_result(res, "ru").startswith("пройден: 10 мин, 19 пакетов из примерно 20, потеряно 1")
    assert powerlog.test_result({**res, "passed": False, "reboots": 2}, "en").startswith("failed: 10 min")
    assert "2 reboots" in powerlog.test_result({**res, "passed": False, "reboots": 2}, "de")
