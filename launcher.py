# -*- coding: utf-8 -*-
"""Запускающий файл MeetingHub.exe.

Сам exe почти не меняется между версиями — поэтому антивирус (Касперский и др.) один раз получает разрешение
и больше не спрашивает. Код программы обновляется отдельно: новая версия лежит в папке данных
(%LOCALAPPDATA%\\Meeting Hub\\code\\<версия>) и подхватывается при запуске. Если новая версия не стартует
два раза подряд, запуск откатывается на предыдущую (или на ту, что вшита в exe).

Здесь только стандартная библиотека и ничего от приложения — этот файл обязан работать при любом коде.
"""
import importlib
import importlib.abc
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

MAX_ATTEMPTS = 2


def _bundle_dir():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def version_tuple(text):
    nums = re.findall(r"\d+", str(text or ""))[:3]
    return tuple(int(n) for n in nums) + (0,) * (3 - len(nums))


def data_dir():
    """Та же папка данных, что и в support.data_dir (дублируем намеренно: запуск не должен зависеть от кода)."""
    override = os.environ.get("MEETINGHUB_DATA")
    if override:
        return Path(override)
    exe_dir = Path(sys.executable).resolve().parent
    if (exe_dir / "portable.txt").exists() or not getattr(sys, "frozen", False):
        return exe_dir if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Meeting Hub"


def base_info():
    """(версия, runtime) приложения, вшитого в exe."""
    rt = _read_json(_bundle_dir() / "runtime.json", {}) or {}
    version = ""
    try:
        version = (_bundle_dir() / "base_version.txt").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return version, int(rt.get("runtime") or 0)


def choose_code(data, base_version, base_runtime, save=True):
    """Папка с кодом новой версии или None. Считает неудачные запуски и откатывает при двух подряд."""
    root = Path(data) / "code"
    state_path = root / "state.json"
    state = _read_json(state_path, None)
    if not isinstance(state, dict) or not state.get("active"):
        return None

    def write():
        if save:
            try:
                state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass

    if state.get("pending") and int(state.get("attempts") or 0) >= MAX_ATTEMPTS:
        state.setdefault("bad", []).append(state["active"])          # не стартует — откат
        state["active"], state["previous"] = state.get("previous") or "", ""
        state["pending"], state["attempts"] = False, 0
        write()
        if not state["active"]:
            return None
    folder = root / state["active"]
    info = _read_json(folder / "code.json", {}) or {}
    if (not folder.is_dir() or info.get("version") != state["active"]
            or int(info.get("runtime") or 0) > base_runtime
            or version_tuple(info.get("version")) <= version_tuple(base_version)):
        return None
    if state.get("pending"):
        state["attempts"] = int(state.get("attempts") or 0) + 1
        write()
    return folder


def force_rollback(data):
    """Новый код не загрузился вообще — при следующем запуске сразу откат."""
    path = Path(data) / "code" / "state.json"
    state = _read_json(path, None)
    if isinstance(state, dict) and state.get("active"):
        state["pending"], state["attempts"] = True, MAX_ATTEMPTS
        try:
            path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass


class CodeFinder(importlib.abc.MetaPathFinder):
    """Отдаёт модули верхнего уровня из папки обновления раньше, чем вшитые в exe."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.names = {p.stem for p in self.folder.glob("*.py")}

    def find_spec(self, name, path=None, target=None):
        if path is None and name in self.names:
            return importlib.util.spec_from_file_location(name, str(self.folder / (name + ".py")))
        return None


def wait_for_parent():
    """После обновления кода программа перезапускает себя: ждём, пока закроется прежний процесс."""
    pid = int(os.environ.pop("MEETINGHUB_WAIT_PID", "0") or 0)
    if not pid or os.name != "nt":
        return
    try:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)      # SYNCHRONIZE
        if handle:
            ctypes.windll.kernel32.WaitForSingleObject(handle, 20000)
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        time.sleep(3)


def main():
    wait_for_parent()
    os.environ.pop("MEETINGHUB_CODE_DIR", None)
    version, runtime = base_info()
    os.environ["MEETINGHUB_BASE_RUNTIME"] = str(runtime)
    os.environ["MEETINGHUB_BASE_VERSION"] = version
    folder = None
    if getattr(sys, "frozen", False) and runtime:
        try:
            folder = choose_code(data_dir(), version, runtime)
        except Exception:
            folder = None
    if folder is not None:
        sys.meta_path.insert(0, CodeFinder(folder))
        os.environ["MEETINGHUB_CODE_DIR"] = str(folder)
    try:
        app = importlib.import_module("app")
    except Exception:
        if folder is None:
            raise
        force_rollback(data_dir())                         # битое обновление: перезапуск на прежней версии
        import subprocess
        subprocess.Popen([sys.executable] + sys.argv[1:], close_fds=True)
        return
    app.main()


if __name__ == "__main__":
    main()
