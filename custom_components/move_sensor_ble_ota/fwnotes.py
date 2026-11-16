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
    old_files: dict = man.get("files") or {}
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


def render(ch: dict | None, fallback: list[dict] | None, fmt_time) -> list[str]:
    """Markdown-блок «Что изменилось» для release notes."""
    lines: list[str] = []
    if ch is not None:
        files = ch["files"]
        if not files:
            return lines
        lines.append("### Что изменилось с прошитой сборки")
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
