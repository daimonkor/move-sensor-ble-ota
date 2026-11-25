"""Unit tests for integration 1.27-1.29 (no Home Assistant needed; code is taken from the sources by AST).

  python3 -m pytest -q tests

- image_toolchain / find_toolchain: ESPHome and ESP-IDF versions read from a firmware image (1.27);
- version_key: «2026.11.1» > «2026.11.0», a -dev build is older than its release (1.27);
- auto advertising window right after a clean install: starts from the sensor's windows, no
  shortening without a full RSSI sample and the 3 dB margin, a weaker signal lengthens at once (1.29.2);
- selfupdate: version compare and the auto-update install decision (1.29 / 1.29.2).
"""
from __future__ import annotations

import ast
import datetime
import os
import re
import struct
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "custom_components", "move_sensor_ble_ota")
OTA = os.path.join(PKG, "ota.py")
SELF = os.path.join(PKG, "selfupdate.py")
CONST = os.path.join(PKG, "const.py")


def _nodes(path: str, names: set[str], cls: str | None = None) -> list[ast.stmt]:
    tree = ast.parse(open(path, encoding="utf-8").read())
    body = tree.body
    if cls is not None:
        body = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls).body
    out = []
    for node in body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            out.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets):
            out.append(node)
    missing = names - {getattr(n, "name", None) for n in out} - {
        t.id for n in out if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    assert not missing, f"not found in {os.path.basename(path)}: {missing}"
    return out


def _exec(nodes: list[ast.stmt], ns: dict) -> dict:
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "src", "exec"), ns)  # noqa: S102
    return ns


CONSTS = _exec(_nodes(CONST, {"AUTO_WINDOW_LEVELS", "AUTO_WINDOW_RSSI", "AUTO_WINDOW_HYST_DB", "AUTO_WINDOW_SAMPLES"}), {})
TC = _exec(_nodes(OTA, {"_ESPHOME_VER_RE", "image_toolchain", "find_toolchain", "version_key", "same_value"}),
           {"re": re, "os": os})
WIN = _exec(_nodes(OTA, {"rssi_median", "_window_target_level", "_applied_window_level"}, cls="OtaManager"),
            {**CONSTS, "same_value": TC["same_value"],
             "dt_util": types.SimpleNamespace(utcnow=lambda: datetime.datetime.now(datetime.UTC))})
SU = _exec(_nodes(SELF, {"_vkey", "install_target"}), {"re": re})


# ---------------- firmware image: ESPHome / ESP-IDF version ----------------
def _image(esphome: str | None, idf: str, sha: bytes = b"\xe1\xd6\x51\x0d") -> bytes:
    """Minimal app image: 24-byte header + 8-byte segment header + esp_app_desc_t, then a log string."""
    desc = bytearray(256)
    struct.pack_into("<I", desc, 0, 0xABCD5432)
    desc[16:16 + 9] = b"2026.11.1"            # version (ESPHome puts its own there)
    desc[48:48 + 15] = b"move-sensor-ble"
    desc[112:112 + len(idf)] = idf.encode()
    desc[144:148] = sha
    tail = (f"{esphome} (config hash 0x1234abcd)".encode() if esphome else b"no version here")
    return b"\xe9" + bytes(31) + bytes(desc) + b"\0" * 64 + tail + b"\0"


def test_image_toolchain_reads_esphome_and_idf():
    tc = TC["image_toolchain"](_image("2026.11.1", "v5.5.6"))
    assert tc == {"esphome": "2026.11.1", "idf": "v5.5.6", "sha": "e1d6510d"}


def test_image_toolchain_dev_build_and_missing_string():
    assert TC["image_toolchain"](_image("2026.12.0-dev", "v5.5.6"))["esphome"] == "2026.12.0-dev"
    tc = TC["image_toolchain"](_image(None, "v5.5.6"))
    assert "esphome" not in tc and tc["idf"] == "v5.5.6"


def test_image_toolchain_not_an_app_image():
    assert TC["image_toolchain"](b"\xe9" + bytes(400)) == {}


def test_find_toolchain_by_sha(tmp_path):
    (tmp_path / "a.ota.bin").write_bytes(_image("2026.11.0", "v5.5.6", b"\x01\x02\x03\x04"))
    (tmp_path / "backup").mkdir()
    (tmp_path / "backup" / "b.ota.bin").write_bytes(_image("2026.10.2", "v5.4.1", b"\xaa\xbb\xcc\xdd"))
    assert TC["find_toolchain"](str(tmp_path), "01020304")["esphome"] == "2026.11.0"
    assert TC["find_toolchain"](str(tmp_path), "aabbccdd")["idf"] == "v5.4.1"   # backup/ too
    assert TC["find_toolchain"](str(tmp_path), "deadbeef") is None


@pytest.mark.parametrize(("a", "b"), [
    ("2026.11.1", "2026.11.0"), ("2026.12.0", "2026.11.9"), ("2027.1.0", "2026.12.3"),
    ("2026.12.0", "2026.12.0-dev"), ("2026.12.0", "2026.12.0b1"),
])
def test_version_key_order(a, b):
    assert TC["version_key"](a) > TC["version_key"](b)


def test_version_key_garbage():
    assert TC["version_key"]("") == () and TC["version_key"](None) == ()


# ---------------- auto advertising window (1.29.2) ----------------
L0, L1, L2 = CONSTS["AUTO_WINDOW_LEVELS"]


def _mgr(rssi: list[int], level, applied: dict, boost=None):
    o = types.SimpleNamespace(rssi_hist=rssi, window_level=level, applied=applied, window_boost_until=boost)
    for name in ("rssi_median", "_window_target_level", "_applied_window_level"):
        setattr(o, name, types.MethodType(WIN[name], o))
    return o


def _win(level):
    return {"motion_window": level[0], "climate_window": level[1]}


def test_fresh_install_keeps_sensor_windows_with_few_samples():
    # the case seen on 2026-11-25: 5 packets at -74 dBm after a clean install shortened 1500/2500 -> 800/1500
    assert _mgr([-74] * 5, None, _win(L1))._window_target_level() == 1


def test_fresh_install_needs_margin_even_with_full_sample():
    assert _mgr([-74] * 10, None, _win(L1))._window_target_level() == 1   # -74 < -75 + 3
    assert _mgr([-71] * 10, None, _win(L1))._window_target_level() == 0   # margin ok


def test_fresh_install_full_sample_but_few_hist_entries_blocks_shortening():
    assert _mgr([-60] * 9, None, _win(L1))._window_target_level() == 1


def test_weaker_signal_lengthens_at_once():
    assert _mgr([-90] * 5, None, _win(L0))._window_target_level() == 2
    assert _mgr([-80] * 5, 0, _win(L0))._window_target_level() == 1


def test_known_level_behaves_as_before():
    assert _mgr([-74] * 10, 1, _win(L1))._window_target_level() == 1
    assert _mgr([-70] * 10, 1, _win(L1))._window_target_level() == 0


def test_too_few_samples_no_decision():
    assert _mgr([-60] * 4, None, _win(L1))._window_target_level() is None


def test_boost_after_missed_packet():
    later = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
    assert _mgr([-60] * 10, 0, _win(L0), boost=later)._window_target_level() == 1


def test_applied_window_level():
    assert _mgr([], None, _win(L2))._applied_window_level() == 2
    assert _mgr([], None, {"motion_window": 2000, "climate_window": 3000})._applied_window_level() is None
    assert _mgr([], None, {})._applied_window_level() is None


# ---------------- selfupdate (1.29 / 1.29.2) ----------------
def test_selfupdate_vkey():
    v = SU["_vkey"]
    assert v("v1.29.1") == (1, 29, 1) == v("1.29.1")
    assert v("v1.30.0") > v("v1.29.9") and v("garbage") == ()


@pytest.mark.parametrize(("state", "attrs", "installed", "expected"), [
    ("on", {"latest_version": "v1.29.3"}, "1.29.2", "v1.29.3"),
    ("off", {"latest_version": "v1.29.3"}, "1.29.2", None),                       # Marketplace: no update
    ("on", {"latest_version": "v1.29.3", "in_progress": True}, "1.29.2", None),   # already installing
    ("on", {"latest_version": "v1.29.2"}, "1.29.2", None),                        # same version
    ("on", {"latest_version": "v1.28.0"}, "1.29.2", None),                        # never downgrade
    ("on", {}, "1.29.2", None),
    (None, {}, "1.29.2", None),                                                   # entity missing
    ("on", {"latest_version": "v1.29.3"}, "?", None),                             # own version unknown
])
def test_install_target(state, attrs, installed, expected):
    assert SU["install_target"](state, attrs, installed) == expected
