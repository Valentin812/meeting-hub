# -*- coding: utf-8 -*-
"""Meeting Hub v5 — лёгкие сигналы Windows без внешних зависимостей.

* list_windows()  — видимые окна верхнего уровня: (заголовок, имя процесса);
* mic_in_use()    — какие приложения держат микрофон прямо сейчас.

Микрофон определяется по реестру CapabilityAccessManager: пока приложение
использует микрофон, Windows хранит у него LastUsedTimeStop = 0. Это факт
«микрофон открыт», а не «идёт конференция» — вывод делает meeting_detector.
Звук и речь не записываются и не анализируются.
"""
import os
import time

from meeting_detector import Observation

MIC_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_proc_names = {}


def _process_name(pid, kernel32, wintypes, ctypes):
    cached = _proc_names.get(pid)
    if cached is not None:
        return cached
    name = ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value).lower()
        finally:
            kernel32.CloseHandle(handle)
    if len(_proc_names) > 2000:
        _proc_names.clear()
    _proc_names[pid] = name
    return name


def list_windows():
    """Вернуть [(заголовок, процесс)] для видимых окон с непустым заголовком."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    own_pid = os.getpid()
    found = []

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == own_pid:
            return True
        found.append((title, _process_name(pid.value, kernel32, wintypes, ctypes)))
        return True

    cb = enum_proc(callback)  # держим ссылку, пока идёт перечисление
    user32.EnumWindows(cb, 0)
    return found


def _app_id(key_name):
    """«C:#Program Files#Google#Chrome#Application#chrome.exe» → «chrome.exe»."""
    return key_name.split("#")[-1].lower()


def _scan(winreg, key, users):
    i = 0
    while True:
        try:
            name = winreg.EnumKey(key, i)
        except OSError:
            break
        i += 1
        if name.lower() == "nonpackaged":
            continue
        try:
            with winreg.OpenKey(key, name) as sub:
                start = _query(winreg, sub, "LastUsedTimeStart")
                stop = _query(winreg, sub, "LastUsedTimeStop")
        except OSError:
            continue
        if start and stop == 0:
            users.add(_app_id(name))


def _query(winreg, key, value):
    try:
        data, _typ = winreg.QueryValueEx(key, value)
        return int(data)
    except (OSError, ValueError, TypeError):
        return None


def mic_in_use():
    """Множество приложений, которые держат микрофон сейчас. Бросает OSError при сбое."""
    if os.name != "nt":
        raise OSError("проверка микрофона доступна только в Windows")
    import winreg
    users = set()
    try:
        root_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, MIC_KEY)
    except FileNotFoundError:
        return users          # микрофоном на этом компьютере ещё никто не пользовался
    with root_key as root:
        _scan(winreg, root, users)
        try:
            with winreg.OpenKey(root, "NonPackaged") as nonpackaged:
                _scan(winreg, nonpackaged, users)
        except FileNotFoundError:
            pass
    return users


def observe(now=None):
    """Одно наблюдение. Сбой одного источника не мешает другому."""
    now = time.time() if now is None else now
    try:
        windows = list_windows()
        win_error = ""
    except OSError as e:
        windows, win_error = [], str(e)
    try:
        mic, mic_error = mic_in_use(), ""
    except OSError as e:
        mic, mic_error = None, str(e)
    if win_error:
        mic_error = (mic_error + "; " if mic_error else "") + "окна: " + win_error
    return Observation(now=now, windows=windows, mic=mic, mic_error=mic_error)
