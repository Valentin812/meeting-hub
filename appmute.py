# -*- coding: utf-8 -*-
"""Заглушить звуки отдельных программ на время записи (мессенджеры, почта и т. п.).

Windows ведёт громкость каждой программы отдельно («микшер громкости»). Пока идёт запись, программа выключает звук
у программ из списка: их «дзынь» не слышно и в записи. Звук самой встречи (браузер, Teams, Zoom) не трогается.
Новые звуковые сеансы появляются, когда программа что-то проиграла, поэтому список проверяется каждые пару секунд.
После записи звук возвращается. Имена заглушённых программ сохраняются в файл: после сбоя звук вернётся при
следующем запуске. Работает через системный интерфейс аудиосеансов (Core Audio); при любой ошибке молча ничего не делает.
"""
import ctypes
import json
import os
import threading
import time
from pathlib import Path

DEFAULT_LIST = "telegram.exe, whatsapp.exe, viber.exe, outlook.exe, olk.exe, thunderbird.exe, max.exe"
# Для режима «всё, кроме…»: звук встречи идёт через эти программы, их не глушим никогда.
SAFE = {"chrome.exe", "msedge.exe", "firefox.exe", "browser.exe", "opera.exe", "brave.exe", "vivaldi.exe",
        "yandex.exe", "teams.exe", "ms-teams.exe", "zoom.exe", "webex.exe", "ciscowebex.exe", "atmgr.exe",
        "skype.exe", "meetinghub.exe", "audiodg.exe", "python.exe", "pythonw.exe"}
PASS_SEC = 1.5


def supported():
    return os.name == "nt"


def parse_names(text):
    out = []
    for part in str(text or "").replace(";", ",").replace("\n", ",").split(","):
        n = part.strip().lower()
        if n and n not in out:
            out.append(n if "." in n else n + ".exe")
    return out


def should_mute(proc, names, all_mode=False, keep=()):
    """Глушить ли программу proc (имя процесса). keep — программа текущей встречи, её не трогаем никогда."""
    proc = (proc or "").lower()
    if not proc or proc in {k.lower() for k in keep if k}:
        return False
    if all_mode:
        return proc not in SAFE
    return proc in set(names)


# ---------------------------------------------------------------------------
# Core Audio через ctypes
# ---------------------------------------------------------------------------
class GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]


def _guid(text):
    p = text.split("-")
    return GUID(int(p[0], 16), int(p[1], 16), int(p[2], 16), (ctypes.c_ubyte * 8)(*bytes.fromhex(p[3] + p[4])))


def _call(obj, index, argtypes, *args):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])
    hr = fn(obj, *args)
    if hr < 0:
        raise OSError("COM 0x%08x" % (hr & 0xFFFFFFFF))
    return hr


def _release(obj):
    try:
        if obj:
            _call(obj, 2, ())
    except OSError:
        pass


def _proc_name(pid):
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(wintypes.HANDLE(handle), 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))
    return ""


def _each_session(visit):
    """Для каждого аудиосеанса на основном устройстве вывода вызвать visit(pid, имя_процесса, громкость_ptr)."""
    c_void, c_int, c_ulong = ctypes.c_void_p, ctypes.c_int, ctypes.c_ulong
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx(None, 0)
    clsid, iid_enum = _guid("BCDE0395-E52F-467C-8E3D-C4579291692E"), _guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
    iid_mgr, iid_ctl2 = _guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F"), _guid("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")
    iid_vol = _guid("87CE5498-68D6-44E5-9215-6DA47EF883D8")
    pguid = ctypes.POINTER(GUID)
    pvoid = ctypes.POINTER(c_void)
    enumerator, device, mgr, sessions = c_void(), c_void(), c_void(), c_void()
    hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, 23, ctypes.byref(iid_enum), ctypes.byref(enumerator))
    if hr < 0:
        raise OSError("CoCreateInstance 0x%08x" % (hr & 0xFFFFFFFF))
    try:
        _call(enumerator, 4, (c_int, c_int, pvoid), 0, 1, ctypes.byref(device))          # eRender, eMultimedia
        _call(device, 3, (pguid, c_ulong, c_void, pvoid), ctypes.byref(iid_mgr), 23, None, ctypes.byref(mgr))
        _call(mgr, 5, (pvoid,), ctypes.byref(sessions))                                  # GetSessionEnumerator
        count = c_int()
        _call(sessions, 3, (ctypes.POINTER(c_int),), ctypes.byref(count))
        for i in range(count.value):
            ctl, ctl2, vol = c_void(), c_void(), c_void()
            try:
                _call(sessions, 4, (c_int, pvoid), i, ctypes.byref(ctl))
                _call(ctl, 0, (pguid, pvoid), ctypes.byref(iid_ctl2), ctypes.byref(ctl2))
                _call(ctl, 0, (pguid, pvoid), ctypes.byref(iid_vol), ctypes.byref(vol))
                if _call(ctl2, 15, ()) == 0:                                              # системные звуки Windows
                    continue
                pid = c_ulong()
                _call(ctl2, 14, (ctypes.POINTER(c_ulong),), ctypes.byref(pid))
                visit(pid.value, _proc_name(pid.value) if pid.value else "", vol)
            except OSError:
                continue
            finally:
                _release(vol)
                _release(ctl2)
                _release(ctl)
    finally:
        _release(sessions)
        _release(mgr)
        _release(device)
        _release(enumerator)


def _get_mute(vol):
    m = ctypes.c_int()
    _call(vol, 6, (ctypes.POINTER(ctypes.c_int),), ctypes.byref(m))
    return bool(m.value)


def _set_mute(vol, value):
    _call(vol, 5, (ctypes.c_int, ctypes.c_void_p), 1 if value else 0, None)


class AppMuter:
    """Во время записи периодически глушит программы из списка; stop() возвращает звук."""

    def __init__(self, backup, names, all_mode=False, keep=(), log=None):
        self.backup, self.names, self.all_mode = Path(backup), list(names), bool(all_mode)
        self.keep, self.log = tuple(keep), log
        self.running = False
        self.muted = {}                 # pid -> имя процесса, которые заглушили мы
        self._stop = threading.Event()
        self._thread = None
        self._warned = False

    def _warn(self, e):
        if not self._warned and self.log:
            self._warned = True
            self.log.warning("Звуки программ не заглушены: %s", e)

    def mute_pass(self):
        def visit(pid, proc, vol):
            if pid in self.muted or not should_mute(proc, self.names, self.all_mode, self.keep):
                return
            if _get_mute(vol):                       # пользователь уже выключил сам — не трогаем
                return
            _set_mute(vol, True)
            self.muted[pid] = proc
            self._save()
        try:
            _each_session(visit)
        except Exception as e:                       # noqa: BLE001 — любая ошибка Windows не должна мешать записи
            self._warn(e)

    def _save(self):
        try:
            self.backup.parent.mkdir(parents=True, exist_ok=True)
            self.backup.write_text(json.dumps(sorted(set(self.muted.values()))), encoding="utf-8")
        except OSError:
            pass

    def _loop(self):
        while not self._stop.is_set():
            self.mute_pass()
            self._stop.wait(PASS_SEC)
        self.unmute()

    def start(self):
        if self.running or not supported():
            return self
        self.running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="app-mute", daemon=True)
        self._thread.start()
        return self

    def unmute(self):
        pids = set(self.muted)

        def visit(pid, proc, vol):
            if pid in pids and _get_mute(vol):
                _set_mute(vol, False)
        try:
            _each_session(visit)
        except Exception as e:                       # noqa: BLE001
            self._warn(e)
        self.muted.clear()
        try:
            self.backup.unlink()
        except OSError:
            pass

    def stop(self, wait=True):
        if not self.running:
            return
        self.running = False
        self._stop.set()
        if wait and self._thread is not None:
            self._thread.join(timeout=6)


def restore_backup(backup, log=None):
    """После сбоя: вернуть звук программам, которые заглушили в прошлый раз. Возвращает число программ."""
    backup = Path(backup)
    if not supported() or not backup.exists():
        return 0
    try:
        names = set(json.loads(backup.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        names = set()
    count = [0]

    def visit(_pid, proc, vol):
        if proc in names and _get_mute(vol):
            _set_mute(vol, False)
            count[0] += 1
    try:
        _each_session(visit)
    except Exception as e:                           # noqa: BLE001
        if log:
            log.warning("Звук программ не возвращён: %s", e)
        return 0
    try:
        backup.unlink()
    except OSError:
        pass
    return count[0]
