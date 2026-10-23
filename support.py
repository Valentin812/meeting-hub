# -*- coding: utf-8 -*-
"""Meeting Hub — всё, что нужно для работы у других пользователей.

* где хранить данные (профиль пользователя, а не папка программы);
* перенос данных из папки программы и из папки старой версии;
* распознавание причин, по которым не работает запись, и инструкции для каждой;
* архив для поддержки (без записей и расшифровок).

Модуль не зависит от Tkinter и проверяется автотестами.
"""
import json
import os
import platform
import shutil
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

DATA_FILES = ("meeting_hub_training.json", "meeting_hub_settings.json")
LOG_NAME = "meeting_hub.log"


# ---------------------------------------------------------------------------
# Где лежат программа и данные
# ---------------------------------------------------------------------------
def app_dir():
    """Папка программы: рядом с MeetingHub.exe в сборке, рядом с исходниками при запуске из них."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def data_dir():
    """Папка данных пользователя.

    * MEETINGHUB_DATA — явное указание (тесты, портативный режим);
    * файл portable.txt рядом с программой — данные рядом с программой (флешка, ZIP-версия);
    * сборка exe — %LOCALAPPDATA%\\Meeting Hub: не теряется при обновлении и удалении программы;
    * запуск из исходников — папка исходников (как раньше).
    """
    override = os.environ.get("MEETINGHUB_DATA")
    if override:
        path = Path(override)
    elif (app_dir() / "portable.txt").exists() or not getattr(sys, "frozen", False):
        path = app_dir()
    else:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        path = Path(base) / "Meeting Hub"
    path.mkdir(parents=True, exist_ok=True)
    return path


def has_user_data(folder):
    folder = Path(folder)
    if any((folder / f).exists() for f in DATA_FILES):
        return True
    rec = folder / "recordings"
    return rec.is_dir() and any(rec.glob("*.wav"))


def import_from(source, target):
    """Перенести данные старой версии из папки source в папку данных target.

    Шаблоны, журнал и настройки копируются (только если в target их ещё нет — ничего не затираем),
    записи копируются по одной, существующие файлы пропускаются. Исходная папка не меняется.
    Возвращает список перенесённого для показа пользователю.
    """
    source, target = Path(source), Path(target)
    done = []
    if source.resolve() == target.resolve():
        return done
    for name in DATA_FILES:
        src, dst = source / name, target / name
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
            done.append(name)
    src_rec, dst_rec = source / "recordings", target / "recordings"
    if src_rec.is_dir():
        dst_rec.mkdir(parents=True, exist_ok=True)
        copied = 0
        for f in src_rec.iterdir():
            if f.is_file() and not (dst_rec / f.name).exists():
                shutil.copy2(f, dst_rec / f.name)
                copied += f.suffix.lower() == ".wav"
        if copied:
            done.append(f"записей: {copied}")
    return done


def find_old_versions():
    """Папки старых версий (ZIP) в Загрузках и на рабочем столе — для предложения переноса."""
    found = []
    home = Path.home()
    for base in (home / "Downloads", home / "Desktop", home / "Загрузки", Path("C:/MeetingHub")):
        if not base.is_dir():
            continue
        for training in list(base.glob("*/meeting_hub_training.json")) + \
                list(base.glob("*/*/meeting_hub_training.json")) + list(base.glob("meeting_hub_training.json")):
            found.append(training.parent)
    found = sorted(set(found), key=lambda p: (p / "meeting_hub_training.json").stat().st_mtime, reverse=True)
    return found


# ---------------------------------------------------------------------------
# Почему не работает запись и что делать
# ---------------------------------------------------------------------------
SECURITY = {
    "avp.exe": "Kaspersky", "avpui.exe": "Kaspersky", "kavfs.exe": "Kaspersky",
    "dwservice.exe": "Dr.Web", "spideragent.exe": "Dr.Web", "dwengine.exe": "Dr.Web",
    "ekrn.exe": "ESET", "egui.exe": "ESET",
    "avastsvc.exe": "Avast", "avgsvc.exe": "AVG", "mcshield.exe": "McAfee", "ccsvchst.exe": "Norton",
    "sophoshealth.exe": "Sophos", "bdagent.exe": "Bitdefender", "psanhost.exe": "Panda",
    "iwdmagent.exe": "InfoWatch", "iwagent.exe": "InfoWatch", "nscserver.exe": "SearchInform",
}

GUIDES = {
    "kaspersky": {
        "title": "Запись блокирует Kaspersky",
        "text": ("Kaspersky разрешает микрофон и звук компьютера только «Доверенным» программам. "
                 "Meeting Hub нужно один раз добавить в доверенные."),
        "steps": [
            "Откройте Kaspersky → Настройка → «Предотвращение вторжений».",
            "Нажмите «Управление приложениями» и введите в поиске meeting.",
            "У строки MeetingHub.exe нажмите на квадрат в колонке «Ограничения» и выберите «Доверенные».",
            "Закройте и снова откройте Meeting Hub, затем нажмите «Проверить ещё раз».",
            "Если настройки закрыты замком — ими управляет администратор: перешлите ему эту инструкцию.",
        ],
    },
    "privacy": {
        "title": "Windows запрещает программам доступ к микрофону",
        "text": "В параметрах конфиденциальности Windows выключен доступ к микрофону.",
        "steps": [
            "Нажмите «Открыть настройки Windows» ниже (Параметры → Конфиденциальность → Микрофон).",
            "Включите «Разрешить приложениям доступ к микрофону».",
            "Ниже на той же странице включите «Разрешить классическим приложениям доступ к микрофону».",
            "Вернитесь в Meeting Hub и нажмите «Проверить ещё раз».",
        ],
        "settings_uri": "ms-settings:privacy-microphone",
    },
    "no_device": {
        "title": "Не найдено устройство звука",
        "text": "Windows не сообщает микрофон или динамики по умолчанию.",
        "steps": [
            "Подключите гарнитуру или микрофон.",
            "Нажмите «Открыть настройки Windows» и выберите устройства ввода и вывода по умолчанию.",
            "Нажмите «Проверить ещё раз».",
        ],
        "settings_uri": "ms-settings:sound",
    },
    "other_av": {
        "title": "Запись может блокировать антивирус: {vendor}",
        "text": ("Устройства есть и доступ в Windows разрешён, но программа не может их открыть. Так бывает, "
                 "когда антивирус ограничивает доступ к микрофону для неизвестных программ."),
        "steps": [
            "Откройте настройки антивируса {vendor} и найдите раздел о защите микрофона, веб-камеры "
            "или правах приложений.",
            "Разрешите MeetingHub.exe доступ к микрофону (или добавьте программу в доверенные / исключения).",
            "Перезапустите Meeting Hub и нажмите «Проверить ещё раз».",
        ],
    },
    "unknown": {
        "title": "Запись не работает",
        "text": "Причину определить не удалось.",
        "steps": [
            "Нажмите «Сохранить отчёт для поддержки» и отправьте архив разработчику.",
        ],
    },
    "partial_system": {
        "title": "Микрофон работает, звук компьютера не записывается",
        "text": ("Свой голос будет в записи, голоса собеседников — нет. Обычно причина в антивирусе "
                 "или в устройстве вывода по умолчанию."),
        "steps": [
            "Проверьте, что встреча звучит через устройство вывода Windows по умолчанию.",
            "Если включён антивирус — разрешите MeetingHub.exe доступ к аудио (см. инструкцию для Kaspersky).",
            "Нажмите «Проверить ещё раз», включив любое видео.",
        ],
    },
}


def running_security_software():
    if os.name != "nt":
        return []
    import subprocess
    try:
        res = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True,
                             encoding="cp866", errors="replace", timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return []
    names = {line.split('","')[0].strip('"').lower() for line in res.stdout.splitlines() if line.strip()}
    return sorted({vendor for key, vendor in SECURITY.items() if key in names})


def microphone_privacy():
    """'Allow' / 'Deny' / '' по ключам конфиденциальности Windows (самое строгое значение)."""
    if os.name != "nt":
        return ""
    import winreg
    base = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
    values = []
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in ("", "\\NonPackaged"):
            try:
                with winreg.OpenKey(root, base + sub) as k:
                    values.append(str(winreg.QueryValueEx(k, "Value")[0]))
            except OSError:
                pass
    if any(v.lower() == "deny" for v in values):
        return "Deny"
    return "Allow" if values else ""


def diagnose(test_result, kaspersky_loaded=False, privacy="", security=()):
    """По результату пробной записи определить причину и выбрать инструкцию.

    test_result — словарь self_test: {'mic': {...}, 'system': {...}}.
    Возвращает (код, инструкция) или (None, None), если всё работает.
    """
    mic = test_result.get("mic", {})
    system = test_result.get("system", {})
    mic_err, sys_err = mic.get("error", ""), system.get("error", "")
    if not mic_err and not sys_err:
        return None, None
    text = (mic_err + " " + sys_err).lower()
    if kaspersky_loaded:
        return "kaspersky", GUIDES["kaspersky"]
    if privacy == "Deny" or ("0x80070005" in text and mic_err):
        return "privacy", GUIDES["privacy"]
    if "0x80070490" in text or "не найден" in text or "no device" in text:
        return "no_device", GUIDES["no_device"]
    others = [v for v in security if v != "Kaspersky"]
    if others and mic_err:
        vendor = ", ".join(others)
        guide = {k: (v.replace("{vendor}", vendor) if isinstance(v, str) else
                     [s.replace("{vendor}", vendor) for s in v] if isinstance(v, list) else v)
                 for k, v in GUIDES["other_av"].items()}
        return "other_av", guide
    if not mic_err and sys_err:
        return "partial_system", GUIDES["partial_system"]
    return "unknown", GUIDES["unknown"]


def guide_text(guide):
    lines = [guide["title"], "", guide["text"], ""]
    lines += [f"{i}. {s}" for i, s in enumerate(guide["steps"], 1)]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Отчёт для поддержки
# ---------------------------------------------------------------------------
def support_bundle(target_zip, data, version, audio_report=None, extra=None):
    """Собрать ZIP для поддержки: сведения о системе, журнал, отчёт о звуке, список записей.

    Не включает: сами записи, расшифровки, текст журнала решений с заголовками окон.
    """
    data = Path(data)
    info = {
        "version": version,
        "created": datetime.now().isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "windows": platform.platform(),
        "data_dir": str(data),
        "security_software": running_security_software(),
        "microphone_privacy": microphone_privacy(),
    }
    if extra:
        info.update(extra)
    recordings = []
    rec_dir = data / "recordings"
    if rec_dir.is_dir():
        for f in sorted(rec_dir.glob("*.wav")):
            recordings.append({"name": f.name, "size_mb": round(f.stat().st_size / 1048576, 1)})
    info["recordings"] = recordings
    try:
        training = json.loads((data / "meeting_hub_training.json").read_text(encoding="utf-8"))
        journal = training.get("journal", [])
        info["journal_summary"] = {
            "entries": len(journal),
            "patterns": len(training.get("meeting_patterns", [])),
            "recent_events": [{k: j.get(k) for k in ("time", "prev", "new", "event", "initiator", "label")}
                              for j in journal[-40:]],
        }
    except (OSError, ValueError):
        info["journal_summary"] = "нет данных"
    target_zip = Path(target_zip)
    with zipfile.ZipFile(target_zip, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("system.json", json.dumps(info, ensure_ascii=False, indent=2))
        for log in sorted(data.glob(LOG_NAME + "*")):
            z.write(log, log.name)
        if audio_report and Path(audio_report).exists():
            z.write(audio_report, Path(audio_report).name)
        z.writestr("README.txt", "Архив для поддержки Meeting Hub. Записи встреч и расшифровки сюда не входят.\n")
    return target_zip


def now_stamp():
    return time.strftime("%Y%m%d-%H%M%S")
