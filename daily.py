# -*- coding: utf-8 -*-
"""Повседневная работа: автозапуск с Windows, хранение записей, проверка обновлений, поиск по встречам."""
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Meeting Hub"
REPO = "Valentin812/meeting-hub"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=5"


# ---------------------------------------------------------------------------
# Автозапуск
# ---------------------------------------------------------------------------
def autostart_command(exe=None):
    exe = exe or sys.executable
    return f'"{exe}" --minimized'


def autostart_supported():
    """Только для собранной программы на Windows: запускать python.exe с app.py при входе незачем."""
    return os.name == "nt" and bool(getattr(sys, "frozen", False))


def autostart_enabled():
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            value, _ = winreg.QueryValueEx(k, RUN_NAME)
            return bool(value)
    except OSError:
        return False


def set_autostart(enabled, exe=None):
    if os.name != "nt":
        return False
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        if enabled:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, autostart_command(exe))
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass
    return True


# ---------------------------------------------------------------------------
# Хранение
# ---------------------------------------------------------------------------
STEM_RE = re.compile(r"^\d{8}_\d{6}$")


def has_text(wav):
    """У записи есть расшифровка — значит, аудио можно удалять без потери содержания."""
    wav = Path(wav)
    return wav.with_name(wav.stem + ".transcript.json").exists() and wav.with_suffix(".txt").exists()


def audio_cleanup(folder, keep_days, now=None, protect=()):
    """Удалить WAV старше keep_days, если у них есть расшифровка. Тексты, итоги и протоколы остаются.

    Возвращает (список удалённых, освобождено байт). keep_days <= 0 — ничего не удалять.
    """
    folder = Path(folder)
    if not keep_days or keep_days <= 0 or not folder.exists():
        return [], 0
    now = now or time.time()
    limit = now - keep_days * 86400
    protect = {str(Path(p)) for p in protect}
    removed, freed = [], 0
    for wav in folder.glob("*.wav"):
        if wav.name.endswith(".part.wav") or str(wav) in protect or not STEM_RE.match(wav.stem):
            continue
        try:
            st = wav.stat()
        except OSError:
            continue
        if st.st_mtime >= limit or not has_text(wav):
            continue
        try:
            wav.unlink()
        except OSError:
            continue
        removed.append(wav)
        freed += st.st_size
        meta = wav.with_suffix(".json")
        try:
            data = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}
            data["audio_deleted"] = True
            meta.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, ValueError):
            pass
    return removed, freed


def folder_size(folder, pattern="*"):
    try:
        return sum(f.stat().st_size for f in Path(folder).glob(pattern) if f.is_file())
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Обновления
# ---------------------------------------------------------------------------
def parse_version(text):
    nums = re.findall(r"\d+", str(text or ""))[:3]
    return tuple(int(n) for n in nums) + (0,) * (3 - len(nums))


def newest_release(releases):
    """Самый новый релиз из ответа GitHub (пре-релизы тоже — ими выпускаются сборки)."""
    best = None
    for r in releases or []:
        if r.get("draft") or not r.get("tag_name"):
            continue
        if best is None or parse_version(r["tag_name"]) > parse_version(best["tag_name"]):
            best = r
    return best


def installer_url(release):
    for a in release.get("assets") or []:
        if a.get("name", "").lower().startswith("meetinghubsetup") and a.get("name", "").endswith(".exe"):
            return a.get("browser_download_url")
    return release.get("html_url")


def check_update(current, timeout=8, fetch=None):
    """(новая версия, ссылка) или None. fetch — для тестов."""
    if fetch is None:
        def fetch():
            req = urllib.request.Request(RELEASES_URL, headers={"Accept": "application/vnd.github+json",
                                                                "User-Agent": "MeetingHub"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
    best = newest_release(fetch())
    if best and parse_version(best["tag_name"]) > parse_version(current):
        return best["tag_name"].lstrip("v"), installer_url(best), best.get("html_url")
    return None


# ---------------------------------------------------------------------------
# Поиск
# ---------------------------------------------------------------------------
def _read(p):
    try:
        text = Path(p).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if str(p).endswith(".json"):
        try:
            return json.dumps(json.loads(text), ensure_ascii=False)   # кириллица могла быть в виде \uXXXX
        except ValueError:
            pass
    return text


def search(folder, query):
    """Множество имён записей (stem), где встречаются все слова запроса: название, расшифровка, итоги."""
    words = [w for w in re.findall(r"\w+", (query or "").casefold()) if w]
    if not words:
        return None
    folder = Path(folder)
    stems = {p.name.split(".")[0] for p in folder.glob("*") if STEM_RE.match(p.name.split(".")[0])}
    found = set()
    for stem in stems:
        text = " ".join(_read(folder / f"{stem}{suffix}")
                        for suffix in (".json", ".txt", ".summary.json")).casefold()
        if all(w in text for w in words):
            found.add(stem)
    return found
