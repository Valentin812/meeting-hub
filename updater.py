# -*- coding: utf-8 -*-
"""Обновление из программы: скачать установщик новой версии с GitHub и тихо поставить его поверх.

Данные пользователя лежат отдельно (%LOCALAPPDATA%\\Meeting Hub) и при обновлении не трогаются.
После установки программа запускается снова (параметр установщика /launch=1).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path


class UpdateError(Exception):
    pass


def installer_asset(release):
    for a in release.get("assets") or []:
        name = a.get("name", "")
        if name.lower().startswith("meetinghubsetup") and name.lower().endswith(".exe"):
            return {"name": name, "url": a.get("browser_download_url"), "size": int(a.get("size") or 0),
                    "digest": a.get("digest") or ""}
    return None


def can_self_update(app_dir=None, frozen=None):
    """Обновлять сами можем только установленную программу (не портативную и не запуск из исходников)."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if os.name != "nt" or not frozen:
        return False
    app_dir = Path(app_dir or Path(sys.executable).parent)
    return (app_dir / "unins000.exe").exists() and not (app_dir / "portable.txt").exists()


def download(asset, folder, progress=None, cancel=None, opener=None, chunk=256 * 1024):
    """Скачать установщик в folder. progress(скачано, всего). Вернуть путь. Проверить размер и SHA-256."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / asset["name"]
    part = target.with_name(target.name + ".part")
    opener = opener or (lambda url: urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "MeetingHub"}), timeout=30))
    total = int(asset.get("size") or 0)
    sha = hashlib.sha256()
    done = 0
    try:
        with opener(asset["url"]) as r, open(part, "wb") as f:
            total = total or int(getattr(r, "headers", {}).get("Content-Length") or 0)
            while True:
                if cancel is not None and cancel.is_set():
                    raise UpdateError("Скачивание отменено.")
                data = r.read(chunk)
                if not data:
                    break
                f.write(data)
                sha.update(data)
                done += len(data)
                if progress:
                    progress(done, total)
    except UpdateError:
        part.unlink(missing_ok=True)
        raise
    except Exception as e:
        part.unlink(missing_ok=True)
        raise UpdateError(f"Не удалось скачать обновление: {e}") from e
    if total and done != total:
        part.unlink(missing_ok=True)
        raise UpdateError(f"Файл скачан не полностью ({done} из {total} байт). Попробуйте ещё раз.")
    digest = asset.get("digest") or ""
    if digest.startswith("sha256:") and digest[7:].lower() != sha.hexdigest():
        part.unlink(missing_ok=True)
        raise UpdateError("Скачанный файл повреждён (не совпала контрольная сумма). Попробуйте ещё раз.")
    os.replace(part, target)
    return target


def launch_installer(path):
    """Запустить тихую установку отдельно от программы: она закроет старую версию и запустит новую."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/launch=1"],
                     creationflags=flags, close_fds=True)


# ---------------------------------------------------------------------------
# Обновление только кода программы (exe при этом не меняется — антивирус не спрашивает заново)
# ---------------------------------------------------------------------------
CODE_NAME = re.compile(r"^MeetingHubCode-v(\d+(?:\.\d+){0,2})-r(\d+)\.zip$", re.I)


def code_asset(release):
    for a in release.get("assets") or []:
        m = CODE_NAME.match(a.get("name", ""))
        if m:
            return {"name": a["name"], "url": a.get("browser_download_url"), "size": int(a.get("size") or 0),
                    "digest": a.get("digest") or "", "version": m.group(1), "runtime": int(m.group(2))}
    return None


def base_runtime():
    """Номер runtime у exe, из которого запущена программа; 0 — запуск не из exe (обновлять код нельзя)."""
    try:
        return int(os.environ.get("MEETINGHUB_BASE_RUNTIME") or 0)
    except ValueError:
        return 0


def can_update_code(asset, runtime=None):
    runtime = base_runtime() if runtime is None else runtime
    return bool(asset) and runtime > 0 and int(asset.get("runtime") or 0) <= runtime and bool(asset.get("url"))


def install_code(zip_path, data, version, runtime=None):
    """Распаковать архив в <данные>/code/<версия> и сделать его активным. Прежний остаётся для отката."""
    runtime = base_runtime() if runtime is None else runtime
    root = Path(data) / "code"
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / (version + ".new")
    final = root / version
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
            for n in names:
                if "/" in n or "\\" in n or not (n.endswith(".py") or n in ("code.json", "runtime.json")):
                    raise UpdateError(f"В архиве обновления неожиданный файл: {n}")
            if "code.json" not in names or "app.py" not in names:
                raise UpdateError("Архив обновления неполный.")
            info = json.loads(z.read("code.json").decode("utf-8"))
            if info.get("version") != version:
                raise UpdateError("Версия в архиве не совпадает с заявленной.")
            if int(info.get("runtime") or 0) > runtime:
                raise UpdateError("Это обновление требует новой основы программы — поставьте его установщиком.")
            tmp.mkdir()
            z.extractall(tmp)
        for name in tmp.glob("*.py"):
            compile(name.read_text(encoding="utf-8"), str(name), "exec")          # битый код не активируем
    except UpdateError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise UpdateError(f"Не удалось подготовить обновление: {e}") from e
    shutil.rmtree(final, ignore_errors=True)
    os.replace(tmp, final)
    state_path = root / "state.json"
    try:
        old = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    previous = old.get("active") if old.get("active") != version else old.get("previous", "")
    state = {"active": version, "previous": previous or "", "pending": True, "attempts": 0,
             "bad": [b for b in old.get("bad", []) if b != version]}
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    keep = {version, previous}
    for d in root.iterdir():
        if d.is_dir() and d.name not in keep:
            shutil.rmtree(d, ignore_errors=True)
    return final


def mark_code_good(data):
    """Новая версия кода проработала достаточно — откат больше не нужен."""
    path = Path(data) / "code" / "state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if state.get("pending"):
        state["pending"], state["attempts"] = False, 0
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        return True
    return False


def restart_self():
    """Запустить программу заново: новый процесс дождётся закрытия этого (MEETINGHUB_WAIT_PID)."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    env = dict(os.environ, MEETINGHUB_WAIT_PID=str(os.getpid()))
    subprocess.Popen([sys.executable] + sys.argv[1:], creationflags=flags, close_fds=True, env=env)
