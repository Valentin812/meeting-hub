# -*- coding: utf-8 -*-
"""Проверка записи на настоящем звуковом устройстве Windows (виртуальная карта в CI).

Печатает подробную диагностику в аннотации GitHub. Используется для поиска причины
ошибки «invalid argument» на Python 3.14.
"""
import os
import sys
import threading
import time
import traceback
import platform
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def note(title, text):
    text = str(text).replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print(f"::notice title={title}::{text}")


def main():
    import importlib.metadata as md
    note("env", f"python {platform.python_version()} cffi {md.version('cffi')} "
                f"soundcard {md.version('soundcard')} numpy {md.version('numpy')} win {platform.win32_ver()}")
    import soundcard as sc
    import recorder
    try:
        note("devices", f"speakers={sc.all_speakers()} mics={sc.all_microphones(include_loopback=True)}")
    except Exception:
        note("devices", traceback.format_exc())
    results = {}
    for label, kwargs in (("loopback 48000", dict(samplerate=48000)),
                          ("loopback 16000", dict(samplerate=16000)),
                          ("loopback 48000 blocksize 4800", dict(samplerate=48000, blocksize=4800))):
        try:
            spk = sc.default_speaker()
            mic = sc.get_microphone(id=str(spk.id), include_loopback=True)
            with mic.recorder(channels=1, **kwargs) as r:
                data = r.record(numframes=4800)
            results[label] = f"ok {data.shape}"
        except Exception:
            results[label] = traceback.format_exc().strip().splitlines()[-3:]
    for kind in ("mic", "system"):
        try:
            src = recorder.AutoBackend().open(kind, 16000)
            block = src.read(4000)
            src.close()
            results[f"backend {kind}"] = f"ok {src.config} len={len(block)}"
        except Exception as e:
            results[f"backend {kind}"] = str(e)[:200]
    note("raw-soundcard", results)

    # Полный цикл как в приложении: самотест и запись, во время записи играет тон.
    import numpy as np
    stop = threading.Event()

    def play():
        try:
            spk = sc.default_speaker()
            tone = (0.3 * np.sin(2 * np.pi * 440 * np.arange(48000) / 48000)).astype(np.float32)
            with spk.player(samplerate=48000, channels=1) as p:
                while not stop.is_set():
                    p.play(tone)
        except Exception:
            note("player", traceback.format_exc())

    th = threading.Thread(target=play, daemon=True)
    th.start()
    time.sleep(0.5)
    note("self_test", recorder.self_test(2.0))
    note("self_test_soundcard_only", recorder.self_test(1.0, recorder.SoundcardBackend()))
    rec = recorder.AudioRecorder(Path(os.environ.get("RUNNER_TEMP", ".")) / "rec")
    rec.start({"meeting": "ci"})
    time.sleep(3)
    res = rec.stop()
    stop.set()
    import wave
    with wave.open(str(res["path"]), "rb") as w:
        frames = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").reshape(-1, 2)
    note("recording", f"duration={res['duration']:.2f} errors={res['errors']} "
                      f"mic_peak={int(np.abs(frames[:, 0]).max())} system_peak={int(np.abs(frames[:, 1]).max())}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        note("probe-failed", traceback.format_exc())
