# -*- coding: utf-8 -*-
"""Подробная проверка звуковых устройств для Meeting Hub.

Запуск: «Подробная проверка звука…» на вкладке «Записи», audio_check.bat
или MeetingHub.exe --audio-check. Ничего не записывает на диск, кроме отчёта
meeting_hub_audio_report.txt рядом с программой. В отчёте только названия
устройств, настройки доступа к микрофону и тексты ошибок.
"""
import os
import platform
import sys
import threading
import time
import traceback
from pathlib import Path


def base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


HERE = base_dir()
REPORT = HERE / "meeting_hub_audio_report.txt"
lines = []


def out(text=""):
    lines.append(str(text))
    try:
        print(text)
    except Exception:
        pass


def last_error():
    return traceback.format_exc().strip().splitlines()[-1]


def short_tb():
    tb = traceback.format_exc().strip().splitlines()
    keep = [l for l in tb if l.strip().startswith(("File", "raise", "hr =", "_com", "with ", "rec", "return"))]
    return "\n      ".join(keep[-4:] + tb[-1:])


def privacy():
    """Разрешения Windows на доступ к микрофону (Параметры → Конфиденциальность → Микрофон)."""
    if os.name != "nt":
        return
    import winreg
    base = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
    for root, rname in ((winreg.HKEY_LOCAL_MACHINE, "HKLM"), (winreg.HKEY_CURRENT_USER, "HKCU")):
        for sub, label in (("", "все приложения"), ("NonPackaged", "классические приложения")):
            try:
                with winreg.OpenKey(root, base + ("\\" + sub if sub else "")) as k:
                    value, _ = winreg.QueryValueEx(k, "Value")
                out(f"  {rname} {label}: {value}")
            except OSError:
                out(f"  {rname} {label}: (нет записи)")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base + r"\NonPackaged") as k:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                low = name.lower()
                if "python" in low or "meetinghub" in low:
                    out(f"  запись для {name.split('#')[-1]}: есть")
    except OSError:
        pass


def compat():
    """Режим совместимости (свойства файла → Совместимость) ломает звуковые API Windows."""
    if os.name != "nt":
        return
    import winreg
    exe = sys.executable.lower()
    found = False
    for root, rname in ((winreg.HKEY_CURRENT_USER, "HKCU"), (winreg.HKEY_LOCAL_MACHINE, "HKLM")):
        try:
            with winreg.OpenKey(root, r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers") as k:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    i += 1
                    if name.lower() == exe or "python" in name.lower() or "meetinghub" in name.lower():
                        out(f"  {rname}: {name} = {value}")
                        found = True
        except OSError:
            pass
    if not found:
        out("  не включён")


SECURITY = {
    "avp.exe": "Kaspersky", "avpui.exe": "Kaspersky", "kavfs.exe": "Kaspersky", "klnagent.exe": "Kaspersky (агент администрирования)",
    "dwservice.exe": "Dr.Web", "spideragent.exe": "Dr.Web", "dwengine.exe": "Dr.Web",
    "ekrn.exe": "ESET", "egui.exe": "ESET", "msmpeng.exe": "Microsoft Defender",
    "avastsvc.exe": "Avast", "avgsvc.exe": "AVG", "mcshield.exe": "McAfee", "ccsvchst.exe": "Norton/Symantec",
    "sophoshealth.exe": "Sophos", "bdagent.exe": "Bitdefender", "psanhost.exe": "Panda",
    "iwdmagent.exe": "InfoWatch (DLP)", "iwagent.exe": "InfoWatch (DLP)", "nscserver.exe": "SearchInform (DLP)",
    "searchinform": "SearchInform (DLP)", "staffcop": "StaffCop (контроль сотрудников)", "falconsensor": "CrowdStrike",
    "csfalconservice.exe": "CrowdStrike", "sentinelagent.exe": "SentinelOne", "cyserver.exe": "Cortex XDR",
}


def security_software():
    """Антивирусы и системы контроля, которые умеют блокировать доступ к микрофону."""
    if os.name != "nt":
        return
    import subprocess
    res = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True,
                         encoding="cp866", errors="replace", timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    names = {line.split('","')[0].strip('"').lower() for line in res.stdout.splitlines() if line.strip()}
    found = sorted({vendor for name in names for key, vendor in SECURITY.items() if key in name})
    out("  " + (", ".join(found) if found else "известные антивирусы и DLP не найдены"))


def foreign_modules():
    """Сторонние библиотеки, загруженные в процесс программы (антивирус и DLP так перехватывают звук)."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    psapi = ctypes.WinDLL("psapi")
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.EnumProcessModules.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE), wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD)]
    psapi.GetModuleFileNameExW.argtypes = [wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    proc = kernel32.GetCurrentProcess()
    mods = (wintypes.HMODULE * 2048)()
    need = wintypes.DWORD()
    psapi.EnumProcessModules(proc, mods, ctypes.sizeof(mods), ctypes.byref(need))
    windir = os.environ.get("WINDIR", r"C:\Windows").lower()
    own = [str(HERE).lower(), str(Path(sys.executable).parent).lower(), str(Path(sys.base_prefix)).lower()]
    shown = 0
    for i in range(min(need.value // ctypes.sizeof(wintypes.HMODULE), 2048)):
        buf = ctypes.create_unicode_buffer(1024)
        psapi.GetModuleFileNameExW(proc, mods[i], buf, 1024)
        path = buf.value
        low = path.lower()
        if not path or low.startswith(windir) or any(low.startswith(o) for o in own) or "site-packages" in low:
            continue
        out(f"  {path}")
        shown += 1
    if not shown:
        out("  сторонних библиотек нет")


def portaudio_all():
    """Пробуем открыть КАЖДОЕ устройство записи и loopback через PortAudio."""
    import numpy as np
    import pyaudiowpatch as pyaudio
    p = pyaudio.PyAudio()
    try:
        ok = 0
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d["maxInputChannels"] <= 0:
                continue
            api = p.get_host_api_info_by_index(d["hostApi"])["name"].replace("Windows ", "")
            rate = int(d["defaultSampleRate"])
            ch = max(1, min(int(d["maxInputChannels"]), 2))
            try:
                st = p.open(format=pyaudio.paFloat32, channels=ch, rate=rate, input=True,
                            input_device_index=i, frames_per_buffer=rate // 20)
                got, peak, end = 0, 0.0, time.time() + 0.4
                while time.time() < end:
                    n = st.get_read_available()
                    if n:
                        data = np.frombuffer(st.read(n, exception_on_overflow=False), dtype=np.float32)
                        got += n
                        peak = max(peak, float(np.abs(data).max()) if len(data) else 0.0)
                    else:
                        time.sleep(0.02)
                st.close()
                ok += 1
                out(f"  OK      [{api}] {d['name']} | {rate} Гц, {ch} кан. | кадров {got}, пик {peak:.3f}")
            except Exception:
                out(f"  ОШИБКА  [{api}] {d['name']} | {rate} Гц, {ch} кан. | {last_error()}")
        out(f"  Итого открылось устройств: {ok}")
    finally:
        p.terminate()


def soundcard_default(prefix=""):
    import numpy as np
    import soundcard as sc
    import recorder
    try:
        mic = sc.default_microphone()
        spk = sc.default_speaker()
        loop = sc.get_microphone(id=str(spk.id), include_loopback=True)
    except Exception:
        out(f"{prefix}  устройства по умолчанию: ОШИБКА — {last_error()}")
        return
    out(f"{prefix}  по умолчанию: микрофон «{mic.name}», динамики «{spk.name}»")
    for label, dev in (("микрофон", mic), ("звук компьютера", loop)):
        try:
            r = recorder.native_recorder(dev, 2_000_000)
            with r:
                data = np.asarray(r.record(numframes=r.samplerate // 4))
            out(f"{prefix}  OK      {label}: родной формат {r.samplerate} Гц, {len(r.channelmap)} кан., "
                f"пик {float(np.abs(data).max()):.3f}")
        except Exception:
            out(f"{prefix}  ОШИБКА  {label}: родной формат — {last_error()}")
        try:
            with dev.recorder(samplerate=48000, channels=1) as r:
                r.record(numframes=4800)
            out(f"{prefix}  OK      {label}: 48000 Гц моно")
        except Exception:
            out(f"{prefix}  ОШИБКА  {label}: 48000 Гц моно — {last_error()}")


def main():
    out("=== Meeting Hub: проверка звука ===")
    out(time.strftime("%Y-%m-%d %H:%M:%S"))
    frozen = "exe" if getattr(sys, "frozen", False) else "скрипт"
    out(f"Python {platform.python_version()} ({platform.architecture()[0]}, {frozen}), {sys.executable}")
    out(f"Windows: {platform.platform()} {platform.win32_ver() if os.name == 'nt' else ''}")
    try:
        import importlib.metadata as md
        for pkg in ("PyAudioWPatch", "soundcard", "cffi", "numpy"):
            try:
                out(f"{pkg} {md.version(pkg)}")
            except Exception as e:
                out(f"{pkg}: версия неизвестна ({type(e).__name__})")
    except Exception:
        pass
    sys.path.insert(0, str(HERE))

    out("\n--- Доступ к микрофону (Параметры Windows) ---")
    try:
        privacy()
    except Exception:
        out("  ОШИБКА: " + last_error())

    out("\n--- Режим совместимости для этой программы ---")
    try:
        compat()
    except Exception:
        out("  ОШИБКА: " + last_error())

    out("\n--- PortAudio: все устройства записи ---")
    try:
        portaudio_all()
    except Exception:
        out("  PortAudio: ОШИБКА\n      " + short_tb())

    out("\n--- Антивирусы и системы контроля ---")
    try:
        security_software()
    except Exception:
        out("  ОШИБКА: " + last_error())

    out("\n--- Сторонние библиотеки в процессе программы ---")
    try:
        foreign_modules()
    except Exception:
        out("  ОШИБКА: " + last_error())

    out("\n--- soundcard: устройства по умолчанию ---")
    try:
        soundcard_default()
    except Exception:
        out("  soundcard: ОШИБКА\n      " + short_tb())

    out("\n--- soundcard в отдельном потоке (как в программе) ---")

    def worker():
        import ctypes
        hr = ctypes.windll.ole32.CoInitializeEx(None, 0) if os.name == "nt" else None
        try:
            soundcard_default("[поток]")
        except Exception:
            out("[поток] ОШИБКА: " + last_error())
        finally:
            if hr in (0, 1):
                ctypes.windll.ole32.CoUninitialize()

    th = threading.Thread(target=worker)
    th.start()
    th.join(60)


def run(report=REPORT):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        main()
    except Exception:
        out("Проверка прервалась:\n" + traceback.format_exc())
    Path(report).write_text("\n".join(lines), encoding="utf-8")
    try:
        print(f"\nОтчёт сохранён: {report}")
    except Exception:
        pass
    return report


if __name__ == "__main__":
    run()
