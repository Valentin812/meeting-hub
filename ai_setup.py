# -*- coding: utf-8 -*-
"""Установка локального ИИ (Ollama + модель для итогов) прямо из программы, без батников и командной строки."""
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

BASE = "http://localhost:11434"
DOWNLOAD_URL = "https://ollama.com/download/OllamaSetup.exe"
SITE = "https://ollama.com/download/windows"


class SetupError(Exception):
    pass


def find_exe():
    exe = shutil.which("ollama")
    if exe:
        return exe
    local = os.environ.get("LOCALAPPDATA")
    for p in ([Path(local) / "Programs" / "Ollama" / "ollama.exe"] if local else []):
        if p.exists():
            return str(p)
    return None


def models(base=BASE, timeout=3):
    req = urllib.request.Request(base + "/api/tags")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return [x.get("name", "") for x in json.loads(r.read().decode("utf-8")).get("models", [])]


def has_model(names, model):
    return any(n == model or n == model + ":latest" or n.split(":")[0] == model for n in names)


def status(model, base=BASE, exe=None):
    """'ok' — всё готово; 'no_model' — Ollama работает без модели; 'not_running' — установлена, но не запущена;
    'not_installed' — не установлена."""
    try:
        return "ok" if has_model(models(base), model) else "no_model"
    except Exception:
        exe = exe if exe is not None else find_exe()
        return "not_running" if exe else "not_installed"


def download_installer(folder, progress=None, cancel=None, url=DOWNLOAD_URL, opener=None, chunk=256 * 1024):
    """Скачать установщик Ollama с официального сайта. progress(скачано, всего)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "OllamaSetup.exe"
    part = target.with_name(target.name + ".part")
    opener = opener or (lambda u: urllib.request.urlopen(
        urllib.request.Request(u, headers={"User-Agent": "MeetingHub"}), timeout=30))
    done = 0
    try:
        with opener(url) as r, open(part, "wb") as f:
            total = int(getattr(r, "headers", {}).get("Content-Length") or 0)
            while True:
                if cancel is not None and cancel.is_set():
                    raise SetupError("Скачивание отменено.")
                data = r.read(chunk)
                if not data:
                    break
                f.write(data)
                done += len(data)
                if progress:
                    progress(done, total)
        if done < 1024 * 1024:
            raise SetupError("Скачался слишком маленький файл — проверьте интернет.")
        os.replace(part, target)
        return target
    except SetupError:
        part.unlink(missing_ok=True)
        raise
    except Exception as e:
        part.unlink(missing_ok=True)
        raise SetupError(f"Не удалось скачать Ollama: {e}") from e


def install(installer, cancel=None, timeout=600):
    """Тихая установка (ставится для текущего пользователя, права администратора не нужны)."""
    try:
        proc = subprocess.Popen([str(installer), "/SILENT", "/NORESTART", "/SUPPRESSMSGBOXES"])
    except OSError as e:
        raise SetupError(f"Не удалось запустить установщик Ollama: {e}") from e
    start = time.time()
    while proc.poll() is None:
        if cancel is not None and cancel.is_set():
            raise SetupError("Установка отменена.")
        if time.time() - start > timeout:
            raise SetupError("Установка Ollama длится слишком долго.")
        time.sleep(0.5)
    if proc.returncode not in (0, None):
        raise SetupError(f"Установщик Ollama завершился с кодом {proc.returncode}.")


def ensure_running(base=BASE, exe=None, wait=60, cancel=None):
    """Дождаться, пока Ollama начнёт отвечать; при необходимости запустить."""
    try:
        models(base)
        return
    except Exception:
        pass
    exe = exe or find_exe()
    if not exe:
        raise SetupError("Ollama не найдена после установки. Перезагрузите компьютер или поставьте её вручную: " + SITE)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    try:
        subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags)
    except OSError as e:
        raise SetupError(f"Не удалось запустить Ollama: {e}") from e
    end = time.time() + wait
    while time.time() < end:
        if cancel is not None and cancel.is_set():
            raise SetupError("Отменено.")
        try:
            models(base)
            return
        except Exception:
            time.sleep(1)
    raise SetupError("Ollama не отвечает после запуска.")


def pull(model, progress=None, cancel=None, base=BASE):
    """Скачать модель. progress(скачано, всего, текст)."""
    req = urllib.request.Request(base + "/api/pull", data=json.dumps({"model": model, "stream": True}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        response = urllib.request.urlopen(req, timeout=60)
    except Exception as e:
        raise SetupError(f"Не удалось начать загрузку модели: {e}") from e
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise SetupError("Загрузка модели отменена.")
            line = response.readline()
            if not line:
                break
            part = json.loads(line.decode("utf-8"))
            if part.get("error"):
                raise SetupError(f"Ollama: {part['error']}")
            if progress:
                progress(int(part.get("completed") or 0), int(part.get("total") or 0), part.get("status", ""))
            if part.get("status") == "success":
                return
    finally:
        response.close()
    if not has_model(models(base), model):
        raise SetupError("Загрузка модели оборвалась. Нажмите «Установить» ещё раз — докачается с того же места.")


def setup(model, folder, progress=None, cancel=None, base=BASE):
    """Всё по шагам: установить Ollama (если нет) → запустить → скачать модель.
    progress(этап, скачано, всего). Возвращает True, если что-то делали."""
    st = status(model, base)
    if st == "ok":
        return False
    if st == "not_installed":
        installer = download_installer(folder, lambda d, t: progress and progress("Скачиваю Ollama", d, t), cancel)
        if progress:
            progress("Устанавливаю Ollama", 0, 0)
        install(installer, cancel)
        Path(installer).unlink(missing_ok=True)
    if progress:
        progress("Запускаю Ollama", 0, 0)
    ensure_running(base, cancel=cancel)
    if not has_model(models(base), model):
        pull(model, lambda d, t, s: progress and progress("Скачиваю модель для итогов", d, t), cancel, base)
    return True
