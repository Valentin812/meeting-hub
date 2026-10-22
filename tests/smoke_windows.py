# -*- coding: utf-8 -*-
"""Дымовой тест на настоящей Windows (GitHub Actions, windows-latest).

Проверяет, что вызовы Windows API (окна, реестр микрофона) работают на этой версии Python,
а интерфейс собирается, проходит несколько циклов опроса, показывает все виды
всплывающих окон и корректно закрывается. Реальную встречу он не заменяет.
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def check_signals():
    import win_signals
    obs = win_signals.observe()
    print(f"windows: {len(obs.windows)}; mic: {obs.mic!r}; error: {obs.mic_error!r}")
    assert isinstance(obs.windows, list)
    assert obs.mic is None or isinstance(obs.mic, set)
    start = time.perf_counter()
    for _ in range(20):
        win_signals.observe()
    per_tick = (time.perf_counter() - start) / 20 * 1000
    print(f"::notice title=signals::windows={len(obs.windows)} mic={obs.mic!r} error={obs.mic_error!r} "
          f"observe={per_tick:.1f} ms")
    assert per_tick < 500, "опрос слишком медленный"


def check_ui():
    # Работаем на копии, чтобы не трогать файл обучения в репозитории.
    work = Path(tempfile.mkdtemp())
    for name in ("app.py", "meeting_detector.py", "win_signals.py", "training_store.py"):
        shutil.copy(ROOT / name, work / name)
    shutil.copy(ROOT / "tests" / "data" / "meeting_hub_training_v49.json", work / "meeting_hub_training.json")
    sys.path.insert(0, str(work))
    import tkinter as tk
    import importlib
    app_mod = importlib.import_module("app")
    from meeting_detector import Candidate, COUNTDOWN, ASKING, ENDING, IN_MEETING, IDLE

    root = tk.Tk()
    app = app_mod.App(root)
    seen = []

    def step(n=0):
        if n < 4:
            root.after(300, step, n + 1)
            return
        # Останавливаем цикл опроса, чтобы реальные окна раннера не меняли состояние во время проверки.
        if app._tick_job is not None:
            root.after_cancel(app._tick_job)
            app._tick_job = None
        det = app.detector
        for kind in (COUNTDOWN, ASKING, ENDING):
            det.state = kind
            det.candidate = Candidate("window+mic", "chrome.exe", "Meet – abc - Google Chrome", "Google Meet")
            now = time.time()
            det.countdown_until = det.ask_until = det.ending_until = now + 5
            app.refresh_prompt()
            root.update()
            assert app.prompt is not None and app.prompt.kind == kind, kind
            seen.append(kind)
        app.user_action("keep_meeting")
        assert det.state == IN_MEETING
        app.user_action("manual_end")
        assert det.state == IDLE and app.prompt is None
        app.on_close()

    root.after(500, step)
    root.mainloop()
    assert seen == [COUNTDOWN, ASKING, ENDING], seen
    files = sorted(p.name for p in work.iterdir())
    print("files after run:", files)
    assert any(".v4-backup-" in f for f in files), "нет резервной копии данных v4.9"
    assert "meeting_hub.log" in files, "нет журнала"


if __name__ == "__main__":
    if os.name != "nt":
        print("Пропуск: тест только для Windows")
        sys.exit(0)
    import traceback
    for check in (check_signals, check_ui):
        try:
            check()
        except Exception:
            # Аннотация видна в интерфейсе GitHub и через API, даже когда сам лог недоступен.
            text = traceback.format_exc().strip().replace("%", "%25").replace("\r", "").replace("\n", "%0A")
            print(f"::error title={check.__name__}::{text}")
            sys.exit(1)
    print("SMOKE OK")
