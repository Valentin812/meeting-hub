# -*- coding: utf-8 -*-
"""Обновление из программы: скачать установщик новой версии с GitHub и тихо поставить его поверх.

Данные пользователя лежат отдельно (%LOCALAPPDATA%\\Meeting Hub) и при обновлении не трогаются.
После установки программа запускается снова (параметр установщика /launch=1).
"""
import hashlib
import os
import subprocess
import sys
import urllib.request
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
