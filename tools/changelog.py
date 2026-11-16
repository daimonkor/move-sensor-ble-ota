#!/usr/bin/env python3
"""CHANGELOG прошивки Move Sensor BLE: черновик из коммитов -> релиз под пользователя.

  python3 tools/changelog.py draft             # раздел «## Не выпущено» из коммитов прошивки после релиза
  python3 tools/changelog.py release 1.4.0     # «Не выпущено» -> «## 1.4.0 — дата», чистка, тег fw-1.4.0
  python3 tools/changelog.py show              # что войдёт в черновик (без записи)

Коммиты прошивки — те, что меняют esphome/ (кроме самого CHANGELOG). Последний релиз — тег fw-X.Y.Z,
иначе последний коммит, менявший CHANGELOG. Пиши сообщения так, как их прочитает пользователь:
  feat(fw): Защита батареи от глубокого разряда      -> Добавлено
  fix(fw): Датчик не засыпает посреди прошивки       -> Исправлено
  change(fw): Климат по умолчанию раз в 10 мин       -> Изменено   (также perf)
  chore/refactor/docs/test/ci/build/style(fw): …     -> в CHANGELOG не попадает (техническое)
Сообщения без типа (старые) раскладываются по словам. Через «;» — несколько пунктов.
CHANGELOG пишется в репозиторий и, если HA смонтирован (CONFIG, по умолчанию /Volumes/config), туда же —
его читает интеграция (sync_from_ha.sh копирует HA -> репозиторий и иначе затёр бы черновик).
"""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REL = "esphome/move_sensor_ble/CHANGELOG.md"
CL = os.path.join(ROOT, REL)
BASE_YAML = os.path.join(ROOT, "esphome/move_sensor_ble/base.yaml")
CONFIG = os.environ.get("CONFIG", "/Volumes/config")
UNRELEASED = "Не выпущено"
GROUPS = ("Добавлено", "Исправлено", "Изменено")
TYPE_GROUP = {"feat": "Добавлено", "add": "Добавлено", "fix": "Исправлено", "change": "Изменено",
              "perf": "Изменено", "remove": "Изменено"}
TECH = {"chore", "refactor", "docs", "doc", "test", "tests", "ci", "build", "style", "wip", "release"}
CONV_RE = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?:\s*(?P<text>.+)$", re.I)
# старые сообщения: «v1.15.5: …», «Firmware: …», «Sensor site: …»
PREFIX_RE = re.compile(r"^(?:v\d+\.\d+(?:\.\d+)?|firmware|fw|sensor site|site|docs)\s*:\s*", re.I)
FIX_WORDS = re.compile(r"\b(fix|fixed|don'?t|do not|no longer|repair|repeat|restore|guard|исправ|не засыпает)\w*", re.I)
ADD_WORDS = re.compile(r"\b(add|added|new|own|support|switcher|manual|добав|нов)\w*", re.I)
SECTION_RE = re.compile(r"^## .*$", re.M)


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", ROOT, *args], check=True, capture_output=True, text=True).stdout


def base_ref() -> str | None:
    tags = [t for t in git("tag", "--list", "fw-*", "--sort=-v:refname").split() if t]
    if tags:
        return tags[0]
    # тегов ещё нет — последний коммит, менявший CHANGELOG (после первого релиза ставь теги fw-X.Y.Z:
    # иначе закоммиченный черновик сдвинет точку отсчёта)
    out = git("log", "-1", "--format=%H", "--", REL).strip()
    return out or None


def fw_commits(base: str | None) -> list[tuple[str, str]]:
    rng = f"{base}..HEAD" if base else "HEAD"
    out = git("log", "--reverse", "--format=%h%x09%s", rng, "--", "esphome/", f":(exclude){REL}")
    return [tuple(line.split("\t", 1)) for line in out.splitlines() if "\t" in line]


def classify(subject: str) -> list[tuple[str, str]]:
    """[(группа, пункт)] из темы коммита; [] — техническое."""
    m = CONV_RE.match(subject)
    if m and m.group("type").lower() not in TYPE_GROUP and m.group("type").lower() not in TECH:
        m = None                      # «Firmware: …», «Factory reset: …» — не тип коммита, а префикс
    if m:
        typ = m.group("type").lower()
        if typ in TECH:
            return []
        group = TYPE_GROUP.get(typ, "Изменено")
        parts = [m.group("text")]
    else:
        group = None
        parts = [PREFIX_RE.sub("", subject)]
    items = []
    for part in (p.strip() for p in ";".join(parts).split(";")):
        if not part:
            continue
        g = group or ("Исправлено" if FIX_WORDS.search(part) else "Добавлено" if ADD_WORDS.search(part)
                      else "Изменено")
        items.append((g, part))
    return items


def tidy(text: str) -> str:
    text = re.sub(r"\s*\(([0-9a-f]{7,12})\)\s*$", "", text.strip())   # хеш коммита из черновика
    text = re.sub(r"\s+", " ", text)
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?", "…")) else text + "."


def render_section(heading: str, groups: dict[str, list[str]]) -> str:
    out = [f"## {heading}", ""]
    for g in GROUPS:
        if groups.get(g):
            out += [f"### {g}", *[f"- {x}" for x in groups[g]], ""]
    return "\n".join(out).rstrip() + "\n"


def split(text: str) -> tuple[str, list[str]]:
    """(шапка, разделы «## …» целиком)."""
    heads = list(SECTION_RE.finditer(text))
    if not heads:
        return text.rstrip() + "\n\n", []
    head = text[:heads[0].start()]
    secs = [text[m.start():(heads[i + 1].start() if i + 1 < len(heads) else len(text))].strip() + "\n"
            for i, m in enumerate(heads)]
    return head, secs


def parse_groups(section: str) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    cur = None
    for line in section.splitlines():
        if line.startswith("### "):
            cur = line[4:].strip()
            groups.setdefault(cur, [])
        elif line.lstrip().startswith("- ") and cur:
            groups[cur].append(line.lstrip()[2:].strip())
        elif line.startswith("  ") and cur and groups[cur]:      # перенос строки пункта
            groups[cur][-1] += " " + line.strip()
    return groups


def write(text: str) -> None:
    with open(CL, "w", encoding="utf-8") as f:
        f.write(text)
    ha = os.path.join(CONFIG, REL)
    if os.path.isdir(os.path.dirname(ha)):
        shutil.copyfile(CL, ha)
        print(f"  -> {ha}")


def draft(dry: bool = False) -> str:
    base = base_ref()
    groups: dict[str, list[str]] = {g: [] for g in GROUPS}
    text = open(CL, encoding="utf-8").read()
    # уже выпущенные пункты не возвращаем в черновик (коммит релиза тоже меняет прошивку)
    released = {tidy(x).lower() for sec in split(text)[1] if not sec.startswith(f"## {UNRELEASED}")
                for items in parse_groups(sec).values() for x in items}
    seen = set(released)
    for sha, subj in fw_commits(base):
        for g, item in classify(subj):
            key = tidy(item).lower()
            if key in seen:
                continue
            seen.add(key)
            groups[g].append(f"{item} ({sha})")
    head, secs = split(text)
    secs = [s for s in secs if not s.startswith(f"## {UNRELEASED}")]
    n = sum(len(v) for v in groups.values())
    if n:
        secs.insert(0, render_section(UNRELEASED, groups))
    new = head + "\n".join(secs)
    print(f"base: {base or '(начало истории)'} · пунктов: {n}")
    if dry:
        print(render_section(UNRELEASED, groups) if n else "(нечего добавить)")
    elif new != text:
        write(new)
        print(f"draft -> {REL}")
    return new


def set_fw_version(version: str) -> None:
    """substitutions: fw_version в base.yaml (если есть) — версия, которую прошивка сообщает о себе."""
    for path in (BASE_YAML, os.path.join(CONFIG, "esphome/move_sensor_ble/base.yaml")):
        if not os.path.isfile(path):
            continue
        s = open(path, encoding="utf-8").read()
        s2 = re.sub(r'^(\s*fw_version:\s*)"?[\w.+-]+"?', rf'\g<1>"{version}"', s, count=1, flags=re.M)
        if s2 != s:
            if not path.startswith(ROOT):   # на HA — сначала копия (правило: бэкап перед правкой)
                shutil.copyfile(path, f"{path}.bak_before_fw_version_{dt.datetime.now():%Y%m%d_%H%M%S}")
            open(path, "w", encoding="utf-8").write(s2)
            print(f"  fw_version -> {version}: {path}")


def release(version: str, date: str | None = None) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        sys.exit("версия вида 1.4.0")
    draft()
    text = open(CL, encoding="utf-8").read()
    head, secs = split(text)
    if any(re.match(rf"## v?{re.escape(version)}\b", s) for s in secs):
        sys.exit(f"{version} уже есть в CHANGELOG")
    if not secs or not secs[0].startswith(f"## {UNRELEASED}"):
        sys.exit("нет раздела «Не выпущено» — нечего выпускать (коммитов прошивки после прошлого релиза нет)")
    groups = parse_groups(secs[0])
    clean: dict[str, list[str]] = {}
    seen = set()
    for g in GROUPS:
        for item in groups.get(g, []):
            t = tidy(item)
            if t.lower() not in seen:
                seen.add(t.lower())
                clean.setdefault(g, []).append(t)
    day = date or dt.date.today().strftime("%d.%m.%Y")
    secs[0] = render_section(f"{version} — {day}", clean)
    write(head + "\n".join(secs))
    set_fw_version(version)
    print(f"release {version} -> {REL}\n"
          f"Дальше: перечитать пункты глазами пользователя (переписать понятнее, по-русски), проверить конфиг\n"
          f"ESPHome (fw_version в base.yaml), закоммитить и поставить тег: git tag fw-{version}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "draft":
        draft()
    elif cmd == "show":
        draft(dry=True)
    elif cmd == "release" and len(sys.argv) > 2:
        release(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    else:
        sys.exit(__doc__)
