# -*- coding: utf-8 -*-
"""Системные звуки Windows на время встречи.

Звуки Windows (сообщения, предупреждения, «дзынь» уведомлений) попадают в запись «Собеседники»: их не отличить
от голосов. На время записи программа очищает назначенные звуки событий в реестре текущего пользователя
(HKCU\\AppEvents\\Schemes\\Apps) и после записи возвращает всё как было. Исходные значения сначала сохраняются
в файл рядом с настройками, поэтому после сбоя или закрытия программы звуки восстанавливаются при следующем запуске.
Звуки самих программ (мессенджеры, браузер, Teams) это не затрагивает.
"""
import json
import os
from pathlib import Path

ROOT = r"AppEvents\Schemes\Apps"


def supported():
    return os.name == "nt"


def _winreg(reg):
    if reg is not None:
        return reg
    import winreg
    return winreg


def _events(reg):
    """[(путь_к_ключу_.Current)] для всех событий всех программ."""
    out = []
    try:
        root = reg.OpenKey(reg.HKEY_CURRENT_USER, ROOT)
    except OSError:
        return out
    with root:
        i = 0
        while True:
            try:
                app = reg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                with reg.OpenKey(root, app) as app_key:
                    j = 0
                    while True:
                        try:
                            event = reg.EnumKey(app_key, j)
                        except OSError:
                            break
                        j += 1
                        out.append(f"{ROOT}\\{app}\\{event}\\.Current")
            except OSError:
                continue
    return out


def is_muted(backup):
    return Path(backup).exists()


def mute(backup, reg=None):
    """Очистить звуки событий. Возвращает число очищенных. Повторный вызов ничего не меняет."""
    backup = Path(backup)
    if backup.exists():
        return 0
    reg = _winreg(reg)
    saved = []
    for path in _events(reg):
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, path) as key:
                value, kind = reg.QueryValueEx(key, "")
        except OSError:
            continue
        if value:
            saved.append([path, value, kind])
    if not saved:
        return 0
    backup.parent.mkdir(parents=True, exist_ok=True)
    tmp = backup.with_name(backup.name + ".tmp")
    tmp.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, backup)                       # сначала запасная копия, потом очистка
    cleared = 0
    for path, _value, kind in saved:
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, path, 0, reg.KEY_SET_VALUE) as key:
                reg.SetValueEx(key, "", 0, kind, "")
                cleared += 1
        except OSError:
            pass
    return cleared


def restore(backup, reg=None):
    """Вернуть звуки как были. Возвращает число восстановленных."""
    backup = Path(backup)
    if not backup.exists():
        return 0
    reg = _winreg(reg)
    try:
        saved = json.loads(backup.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = []
    back, failed = 0, 0
    for path, value, kind in saved:
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, path, 0, reg.KEY_SET_VALUE) as key:
                reg.SetValueEx(key, "", 0, kind, value)
                back += 1
        except OSError:
            failed += 1
    if not failed:
        try:
            backup.unlink()
        except OSError:
            pass
    return back
