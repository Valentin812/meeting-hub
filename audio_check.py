# -*- coding: utf-8 -*-
"""Подробная проверка звуковых устройств для Meeting Hub.

Запуск: audio_check.bat (или кнопка «Подробная проверка» на вкладке «Записи»).
Ничего не записывает на диск, кроме отчёта meeting_hub_audio_report.txt рядом с программой.
Отчёт содержит только названия устройств и тексты ошибок — пришлите его разработчику.
"""
import os
import platform
import sys
import threading
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPORT = HERE / "meeting_hub_audio_report.txt"
lines = []


def out(text=""):
    lines.append(str(text))
    try:
        print(text)
    except Exception:
        pass


def short_tb():
    tb = traceback.format_exc().strip().splitlines()
    keep = [l for l in tb if l.strip().startswith(("File", "raise", "hr =", "_com", "with ", "rec", "return"))]
    return "\n      ".join(keep[-6:] + tb[-1:])


def try_record(dev, rate, channels, blocksize):
    import numpy as np
    try:
        with dev.recorder(samplerate=rate, channels=channels, blocksize=blocksize) as r:
            data = np.asarray(r.record(numframes=rate // 4))
        return f"OK  форма {data.shape}, пик {float(np.abs(data).max()):.3f}"
    except Exception:
        return "ОШИБКА\n      " + short_tb()


def check_devices(prefix):
    import soundcard as sc
    try:
        mic = sc.default_microphone()
        out(f"{prefix}микрофон по умолчанию: {mic.name} | каналов {mic.channels}")
    except Exception:
        mic = None
        out(f"{prefix}микрофон по умолчанию: ОШИБКА\n      " + short_tb())
    try:
        spk = sc.default_speaker()
        out(f"{prefix}динамики по умолчанию: {spk.name} | каналов {spk.channels}")
        loop = sc.get_microphone(id=str(spk.id), include_loopback=True)
    except Exception:
        loop = None
        out(f"{prefix}динамики/loopback: ОШИБКА\n      " + short_tb())
    for label, dev in (("микрофон", mic), ("звук компьютера", loop)):
        if dev is None:
            continue
        for rate, channels, blocksize in ((48000, 1, None), (48000, 2, None), (44100, 1, None),
                                          (16000, 1, None), (48000, 1, 4800), (48000, None, None)):
            out(f"{prefix}  {label} {rate} Гц, каналов {channels}, блок {blocksize}: "
                f"{try_record(dev, rate, channels, blocksize)}")


def main():
    out("=== Meeting Hub: проверка звука ===")
    out(f"Python {platform.python_version()} ({platform.architecture()[0]}), {sys.executable}")
    out(f"Windows: {platform.platform()} {platform.win32_ver() if os.name == 'nt' else ''}")
    try:
        import importlib.metadata as md
        for pkg in ("soundcard", "cffi", "numpy"):
            try:
                out(f"{pkg} {md.version(pkg)}")
            except Exception as e:
                out(f"{pkg}: не установлен ({e})")
    except Exception:
        pass
    try:
        import soundcard as sc
    except Exception:
        out("Импорт soundcard: ОШИБКА\n      " + short_tb())
        return
    try:
        out("Микрофоны: " + "; ".join(m.name for m in sc.all_microphones()))
        out("Динамики: " + "; ".join(s.name for s in sc.all_speakers()))
    except Exception:
        out("Список устройств: ОШИБКА\n      " + short_tb())

    out("\n--- Главный поток ---")
    check_devices("")

    out("\n--- Отдельный поток с CoInitializeEx (как в программе) ---")

    def worker():
        import ctypes
        hr = ctypes.windll.ole32.CoInitializeEx(None, 0) if os.name == "nt" else None
        out(f"CoInitializeEx: {hr}")
        try:
            check_devices("[поток] ")
        finally:
            if hr in (0, 1):
                ctypes.windll.ole32.CoUninitialize()

    th = threading.Thread(target=worker)
    th.start()
    th.join(60)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        main()
    except Exception:
        out("Проверка прервалась:\n" + traceback.format_exc())
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    try:
        print(f"\nОтчёт сохранён: {REPORT}")
    except Exception:
        pass
