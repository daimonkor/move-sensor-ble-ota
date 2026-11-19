"""Unit tests that need no Home Assistant: pure functions are taken from the sources by AST.

  python3 -m pytest -q tests

Covers what changed with firmware 1.7.0 / integration 1.21.x:
- custom BLE block: 14-bit wake count + service / router flags (custom_parse);
- firmware fingerprint: the integration (fw_fingerprint) and the build (bthome/_tree_fingerprint)
  must give the same value for the same tree, and the version string carries it;
- fw_semver / version_fingerprint parsing of the firmware version string.
"""
from __future__ import annotations

import ast
import hashlib
import os
import re
import shutil
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OTA = os.path.join(ROOT, "custom_components", "move_sensor_ble_ota", "ota.py")
BTHOME = os.path.join(ROOT, "esphome", "components", "bthome", "__init__.py")
ESPHOME = os.path.join(ROOT, "esphome")
DEVICE_YAML = "move-sensor-ble-148f98.yaml"


def _load(path: str, names: set[str], extra: dict | None = None) -> dict:
    """Execute only the named top-level functions / assignments of a module."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    body = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            body.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets):
            body.append(node)
    ns: dict = {"os": os, "hashlib": hashlib, "re": re, **(extra or {})}
    exec(compile(ast.Module(body=body, type_ignores=[]), path, "exec"), ns)  # noqa: S102
    return ns


OTA_NS = _load(OTA, {"custom_parse", "fw_fingerprint", "version_fingerprint", "fw_semver",
                     "CUSTOM_COMPANY_ID", "WAKE_MASK", "FLAG_SERVICE", "FLAG_ROUTER"})


def _bthome_fp(config_dir: str, yaml_name: str) -> str:
    core = types.SimpleNamespace(config_dir=config_dir, config_path=os.path.join(config_dir, yaml_name))
    ns = _load(BTHOME, {"_tree_fingerprint"}, {"CORE": core})
    return ns["_tree_fingerprint"]()


# ---------------- custom block ----------------
def _md(wake: int, rev: int = 0, motion: int = 0) -> dict:
    return {OTA_NS["CUSTOM_COMPANY_ID"]: wake.to_bytes(2, "little") + bytes([rev, motion])}


def test_custom_parse_flags_and_mask():
    p = OTA_NS["custom_parse"](_md(0x8000 | 0x4000 | 1234, 7, 3))
    assert p == {"wake": 1234, "settings_rev": 7, "motion_count": 3, "service": True, "router": True}
    p = OTA_NS["custom_parse"](_md(0x8000 | 5))
    assert p["service"] is True and p["router"] is False and p["wake"] == 5
    p = OTA_NS["custom_parse"](_md(0x3FFF))
    assert p["service"] is False and p["router"] is False and p["wake"] == 0x3FFF


def test_custom_parse_missing_or_short():
    assert OTA_NS["custom_parse"]({}) == {}
    assert OTA_NS["custom_parse"]({OTA_NS["CUSTOM_COMPANY_ID"]: b"\x01\x02"}) == {}


def test_wake_wrap_delta():
    mask = OTA_NS["WAKE_MASK"]
    assert (5 - 16380) & mask == 9          # across the 14-bit wrap
    assert (120 - 100) & mask == 20


# ---------------- version string ----------------
def test_version_parsing():
    v = "1.7.0 td994b39c v2026.11.19.200633-27bb2a4f"
    assert OTA_NS["fw_semver"](v) == "1.7.0"
    assert OTA_NS["version_fingerprint"](v) == "d994b39c"
    assert OTA_NS["version_fingerprint"]("1.6.1 v2026.11.17.120000-0011aabb") is None   # older firmware
    assert OTA_NS["version_fingerprint"](None) is None
    assert v.endswith("-27bb2a4f")   # the integration matches its own builds by the ELF tail


# ---------------- firmware fingerprint ----------------
def test_fingerprint_same_in_build_and_integration():
    a = OTA_NS["fw_fingerprint"](ESPHOME, DEVICE_YAML)
    b = _bthome_fp(ESPHOME, DEVICE_YAML)
    assert re.fullmatch(r"[0-9a-f]{8}", a)
    assert a == b


@pytest.fixture
def tree(tmp_path):
    dst = tmp_path / "esphome"
    shutil.copytree(ESPHOME, dst, ignore=shutil.ignore_patterns("__pycache__", "labels"))
    return str(dst)


def test_fingerprint_tracks_build_inputs_only(tree):
    fp = OTA_NS["fw_fingerprint"]
    base = fp(tree, DEVICE_YAML)
    # not a build input: site sources, CHANGELOG, backups -> same fingerprint
    for rel in ("move_sensor_ble/site/app.js", "move_sensor_ble/CHANGELOG.md"):
        with open(os.path.join(tree, rel), "a", encoding="utf-8") as f:
            f.write("\n// edit\n")
    open(os.path.join(tree, "move_sensor_ble", "base.yaml.bak_x"), "w").close()
    assert fp(tree, DEVICE_YAML) == base == _bthome_fp(tree, DEVICE_YAML)
    # build inputs -> new fingerprint, both sides agree
    for rel in ("move_sensor_ble/base.yaml", "move_sensor_ble/site_bundle.js", "move_sensor_rtc.h",
                "components/bthome/ble_ota.cpp", DEVICE_YAML):
        before = fp(tree, DEVICE_YAML)
        with open(os.path.join(tree, rel), "a", encoding="utf-8") as f:
            f.write("\n# edit\n")
        after = fp(tree, DEVICE_YAML)
        assert after != before, rel
        assert after == _bthome_fp(tree, DEVICE_YAML), rel
