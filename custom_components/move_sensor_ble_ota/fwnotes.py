"""v1.16: release notes прошивки датчика — что изменилось в файлах сборки с прошитой сборки.

При сборке снимается копия файлов сборки (snapshot «build»); после успешной прошивки она становится
«flashed». Release notes = diff текущих файлов с «flashed». secrets.yaml в копию не попадает (только
хеш — видно, что изменился), значения паролей/ключей в diff заменяются на ***.
Все функции блокирующие — вызывать через executor.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import time

SECRET_FILES = ("secrets.yaml",)
MAX_FILE_LINES = 80        # строк diff на файл
MAX_TOTAL_LINES = 300      # строк diff всего
_SECRET_RE = re.compile(
    r"(?i)^(\s*-?\s*[\w.-]*(password|passwd|psk|key|token|secret)[\w.-]*\s*:\s*)(?!!secret\b)(\S.*)$")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _rel(path: str, base: str) -> str:
    return os.path.relpath(path, base).replace(os.sep, "/")


def mask(text: str) -> str:
    """Значения ключей/паролей, записанные прямо в файле (не !secret), — ***."""
    return "\n".join(_SECRET_RE.sub(lambda m: m.group(1) + "***", ln) for ln in text.split("\n"))


def snapshot(files: list[str], esphome_dir: str, dest: str, tree_hash: str | None) -> None:
    """Копия файлов сборки в dest (атомарно: tmp + replace)."""
    tmp = dest + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    manifest = {"hash": tree_hash, "created": time.time(), "files": {}}
    for path in files:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            continue
        rel = _rel(path, esphome_dir)
        manifest["files"][rel] = _sha(data)
        if os.path.basename(rel) in SECRET_FILES:
            continue
        out = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(data)
    with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    old = dest + ".old"
    shutil.rmtree(old, ignore_errors=True)
    if os.path.isdir(dest):
        os.replace(dest, old)
    os.replace(tmp, dest)
    shutil.rmtree(old, ignore_errors=True)


def snapshot_hash(dest: str) -> str | None:
    try:
        with open(os.path.join(dest, "manifest.json"), encoding="utf-8") as f:
            return json.load(f).get("hash")
    except (OSError, ValueError):
        return None


def promote(root: str, tree_hash: str | None) -> bool:
    """После прошивки: копия сборки (build) с этим хешем -> прошитая (flashed)."""
    src, dst = os.path.join(root, "build"), os.path.join(root, "flashed")
    if not tree_hash or snapshot_hash(src) != tree_hash:
        return False
    if snapshot_hash(dst) == tree_hash:
        return True
    tmp = dst + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src, tmp)
    shutil.rmtree(dst, ignore_errors=True)
    os.replace(tmp, dst)
    return True


def _read_text(path: str) -> list[str] | None:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    try:
        return mask(data.decode("utf-8")).splitlines()
    except UnicodeDecodeError:
        return None


def changes(files: list[str], esphome_dir: str, snap: str, flashed_hash: str | None) -> dict | None:
    """Изменения файлов сборки относительно прошитой копии. None — копии нет (или она не той сборки)."""
    try:
        with open(os.path.join(snap, "manifest.json"), encoding="utf-8") as f:
            man = json.load(f)
    except (OSError, ValueError):
        return None
    if flashed_hash and man.get("hash") != flashed_hash:
        return None
    old_files: dict = {k: v for k, v in (man.get("files") or {}).items() if k != CHANGELOG_REL}
    cur = {_rel(p, esphome_dir): p for p in files}
    out = []
    for rel in sorted(set(old_files) | set(cur)):
        path = cur.get(rel)
        if path is None:
            out.append({"file": rel, "status": "removed", "add": 0, "del": 0, "diff": []})
            continue
        try:
            with open(path, "rb") as f:
                sha = _sha(f.read())
        except OSError:
            continue
        if rel not in old_files:
            status = "added"
        elif old_files[rel] != sha:
            status = "modified"
        else:
            continue
        item = {"file": rel, "status": status, "add": 0, "del": 0, "diff": []}
        if os.path.basename(rel) not in SECRET_FILES:
            new = _read_text(path) or []
            old = (_read_text(os.path.join(snap, rel)) or []) if status == "modified" else []
            diff = list(difflib.unified_diff(old, new, "flashed/" + rel, "now/" + rel, n=2, lineterm=""))
            item["add"] = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
            item["del"] = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
            item["diff"] = diff[2:]   # без заголовков ---/+++
        out.append(item)
    return {"since": man.get("created"), "files": out}


def changed_since(files: list[str], esphome_dir: str, t: float) -> list[dict]:
    """Без копии (прошито до v1.16): какие файлы правили после сборки на датчике — по времени."""
    res = []
    for p in files:
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        if m > t + 120:
            res.append({"file": _rel(p, esphome_dir), "mtime": m})
    return sorted(res, key=lambda x: -x["mtime"])


_STATUS = {"modified": "изменён", "added": "новый", "removed": "удалён"}

# ---------------- v1.16.2: CHANGELOG прошивки (как у аддонов) ----------------
CHANGELOG_REL = "move_sensor_ble/CHANGELOG.md"
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)
_VER_RE = re.compile(r"v?(\d+\.\d+(?:\.\d+)?)")


def read_file(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def parse_changelog(text: str | None) -> list[dict]:
    """Разделы «## 1.4.0 — 17.11.2026» по порядку (сверху — новые): {heading, version, body}."""
    if not text:
        return []
    heads = list(_SECTION_RE.finditer(text))
    out = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        v = _VER_RE.match(m.group(1))
        out.append({"heading": m.group(1), "version": v.group(1) if v else None,
                    "body": text[m.end():end].strip()})
    return out


def top_version(text: str | None) -> str | None:
    for sec in parse_changelog(text):
        if sec["version"]:
            return sec["version"]
    return None


def new_sections(cur: str | None, flashed: str | None) -> list[dict]:
    """Разделы, которых не было в CHANGELOG прошитой сборки. Прошитой копии CHANGELOG нет — только верхний."""
    secs = parse_changelog(cur)
    if not secs:
        return []
    if flashed is None:
        return secs[:1]
    old = {s["heading"] for s in parse_changelog(flashed)}
    return [s for s in secs if s["heading"] not in old]


_KV_RE = re.compile(r"^[+-](\s*)(-\s+)?([\w.-]+):\s*(.*?)\s*(#.*)?$")


def friendly(ch: dict | None) -> list[str]:
    """Понятный пересказ diff: «параметр: было → стало», без комментариев и кода."""
    out: list[str] = []
    for it in (ch or {}).get("files", []):
        if os.path.basename(it["file"]) in SECRET_FILES:
            out.append(f"секреты (`{it['file']}`) изменены")
            continue
        if it["status"] != "modified":
            out.append(f"`{it['file']}` — {_STATUS.get(it['status'], it['status'])}")
            continue
        old, new, comments, other = {}, {}, 0, 0
        for d in it["diff"]:
            if d[:1] not in "+-" or d.startswith(("+++", "---")):
                continue
            body = d[1:].strip()
            if not body or body.startswith(("#", "//")):
                comments += 1
                continue
            m = _KV_RE.match(d)
            if m and it["file"].endswith((".yaml", ".yml")):
                (old if d[0] == "-" else new)[m.group(3)] = m.group(4)
            else:
                other += 1
        for k in list(new):
            if k in old:
                if old[k] != new[k]:
                    out.append(f"`{k}`: {old[k] or '—'} → {new[k] or '—'}")
                old.pop(k)
            else:
                out.append(f"`{k}`: добавлено ({new[k] or '—'})")
        out += [f"`{k}`: убрано" for k in old]
        if other:
            out.append(f"`{os.path.basename(it['file'])}`: изменена логика ({other} строк)")
        elif comments and not (old or new):
            out.append(f"`{os.path.basename(it['file'])}`: только комментарии")
    return out


def render_user(sections: list[dict], ch: dict | None) -> list[str]:
    """v1.16.2: верх примечаний — записи CHANGELOG (как у аддонов); нет записи — пересказ изменений."""
    lines: list[str] = []
    for sec in sections:
        lines += [f"## {sec['heading']}", sec["body"], ""]
    if not sections and ch and ch.get("files"):
        lines += ["## Что нового", "_Для этой сборки нет записи в CHANGELOG прошивки — коротко по файлам:_"]
        lines += [f"- {x}" for x in friendly(ch)] + [""]
    return lines


def summary_line(sections: list[dict], ch: dict | None) -> str | None:
    """Одна строка для «Обновлений»: первые пункты CHANGELOG или пересказа."""
    items: list[str] = []
    for sec in sections:
        items += [ln.lstrip("-* ").strip() for ln in sec["body"].splitlines() if ln.lstrip().startswith(("-", "*"))]
    if not items:
        items = friendly(ch)
    if not items:
        return None
    text = "; ".join(re.sub(r"`|\*\*", "", x) for x in items[:3])   # «_» в именах (ble_rssi_entity) оставляем
    return text if len(text) <= 240 else text[:237] + "…"


def render(ch: dict | None, fallback: list[dict] | None, fmt_time) -> list[str]:
    """Markdown-блок «Технические подробности»: файлы и построчный diff."""
    lines: list[str] = []
    if ch is not None:
        files = ch["files"]
        if not files:
            return lines
        lines.append("### Технические подробности: файлы с прошитой сборки")
        for it in files:
            stat = f" (+{it['add']} −{it['del']})" if it["add"] or it["del"] else ""
            extra = " — содержимое не показывается" if os.path.basename(it["file"]) in SECRET_FILES else ""
            lines.append(f"- `{it['file']}` — {_STATUS.get(it['status'], it['status'])}{stat}{extra}")
        total = 0
        for it in files:
            if not it["diff"] or total >= MAX_TOTAL_LINES:
                continue
            d = it["diff"][:min(MAX_FILE_LINES, MAX_TOTAL_LINES - total)]
            total += len(d)
            lines += ["", f"**{it['file']}**", "```diff", *d]
            if len(d) < len(it["diff"]):
                lines.append(f"… ещё {len(it['diff']) - len(d)} строк")
            lines.append("```")
        if any(it["diff"] for it in files) and total >= MAX_TOTAL_LINES:
            lines += ["", f"_Diff обрезан до {MAX_TOTAL_LINES} строк._"]
        return lines
    if fallback:
        lines.append("### Что изменилось после сборки на датчике")
        lines += [f"- `{it['file']}` — правка {fmt_time(it['mtime'])}" for it in fallback]
        lines += ["", "_Построчный diff появится начиная со следующей прошивки: интеграция сохраняет копию "
                      "файлов каждой сборки._"]
    return lines
