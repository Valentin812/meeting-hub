# -*- coding: utf-8 -*-
"""Голосовой ввод: нажали сочетание клавиш — говорите, нажали ещё раз — текст вставляется в поле,
где стоит курсор (в любой программе). Распознавание локальное — тем же движком, что и встречи
(GigaAM, запасной — Whisper); звук никуда не уходит.

Как вставляется текст: распознанное кладётся в буфер обмена, программа возвращает фокус в окно,
где вы печатали, и нажимает Ctrl+V; прежнее содержимое буфера через пару секунд возвращается.
"""
import os
import threading
import time

import numpy as np

RATE = 16000
MAX_SEC = 120            # дольше — диктовка заканчивается сама
MIN_SEC = 0.4
DEFAULT_HOTKEY = "Ctrl+Alt+Space"

MODS = {"ctrl": 0x2, "control": 0x2, "ctl": 0x2, "alt": 0x1, "shift": 0x4, "win": 0x8}
NAMED = {"space": 0x20, "пробел": 0x20, "enter": 0x0D, "tab": 0x09, "insert": 0x2D, "ins": 0x2D,
         "home": 0x24, "end": 0x23, "pause": 0x13, "pgup": 0x21, "pgdn": 0x22, "`": 0xC0, "~": 0xC0}
MOD_NOREPEAT = 0x4000


class DictationError(Exception):
    pass


def parse_hotkey(text):
    """«Ctrl+Alt+Space» → (модификаторы, код клавиши). Нужен хотя бы один модификатор."""
    parts = [p.strip().lower() for p in str(text or "").replace("-", "+").split("+") if p.strip()]
    if not parts:
        raise DictationError("сочетание не задано")
    mods, key = 0, None
    for p in parts:
        if p in MODS:
            mods |= MODS[p]
        elif key is None:
            key = p
        else:
            raise DictationError(f"в сочетании две обычные клавиши: «{text}»")
    if key is None:
        raise DictationError("добавьте к модификаторам клавишу, например Ctrl+Alt+Space")
    if not mods:
        raise DictationError("нужен Ctrl, Alt, Shift или Win — иначе клавиша перестанет печатать")
    if key in NAMED:
        vk = NAMED[key]
    elif len(key) == 1 and ("a" <= key <= "z" or "0" <= key <= "9"):
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x6F + int(key[1:])
    else:
        raise DictationError(f"неизвестная клавиша «{key}» (буквы A–Z, цифры, F1–F24, Space)")
    return mods, vk


def clean_text(segments):
    """Склеить реплики распознавания в одну фразу для поля ввода."""
    text = " ".join((s.get("text") or "").strip() for s in segments if (s.get("text") or "").strip())
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Запись с микрофона
# ---------------------------------------------------------------------------
class MicCapture:
    """Пишет микрофон в память, пока не вызовут stop(). backend — как у записи встреч (open(name, rate))."""

    def __init__(self, backend, rate=RATE, block_sec=0.1, max_sec=MAX_SEC):
        self.backend, self.rate, self.block = backend, rate, max(1, int(rate * block_sec))
        self.max_frames = int(max_sec * rate)
        self.chunks, self.error = [], ""
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="dictation-mic", daemon=True)
        self._thread.start()
        return self

    def _run(self):
        token = None
        enter = getattr(self.backend, "thread_enter", None)
        try:
            if enter is not None:
                token = enter()
        except Exception:
            token = None
        try:
            source = self.backend.open("mic", self.rate)
        except Exception as e:
            self.error = f"микрофон недоступен: {e}"
            return
        got = 0
        try:
            while not self._stop.is_set() and got < self.max_frames:
                block = source.read(self.block)
                if block is not None and len(block):
                    x = np.asarray(block, dtype=np.float32).reshape(-1)
                    self.chunks.append(x)
                    got += len(x)
        except Exception as e:
            self.error = f"запись прервалась: {e}"
        finally:
            try:
                source.close()
            except Exception:
                pass
            leave = getattr(self.backend, "thread_exit", None)
            if leave is not None:
                try:
                    leave(token)
                except Exception:
                    pass

    @property
    def full(self):
        return sum(len(c) for c in self.chunks) >= self.max_frames

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        samples = np.concatenate(self.chunks) if self.chunks else np.zeros(0, dtype=np.float32)
        if self.error and not len(samples):
            raise DictationError(self.error)
        return samples


def transcribe(engine, samples, language="ru"):
    if len(samples) < MIN_SEC * RATE:
        return ""
    peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
    if peak < 0.003:
        raise DictationError("микрофон ничего не услышал")
    return clean_text(engine.transcribe(samples, language=language or "ru"))


# ---------------------------------------------------------------------------
# Windows: глобальное сочетание клавиш и вставка текста
# ---------------------------------------------------------------------------
def supported():
    return os.name == "nt"


class HotkeyListener:
    """Ловит сочетание клавиш во всей системе (RegisterHotKey) в отдельном потоке и вызывает on_press()."""

    WM_HOTKEY, WM_QUIT = 0x0312, 0x0012

    def __init__(self, hotkey, on_press):
        self.mods, self.vk = parse_hotkey(hotkey)
        self.on_press = on_press
        self.error = ""
        self._tid = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="dictation-hotkey", daemon=True)

    def start(self):
        self._thread.start()
        self._ready.wait(3)
        if self.error:
            raise DictationError(self.error)
        return self

    def _run(self):
        import ctypes
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        self._tid = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, 1, self.mods | MOD_NOREPEAT, self.vk):
            self.error = "сочетание уже занято другой программой — выберите другое в настройках"
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == self.WM_HOTKEY:
                    try:
                        self.on_press()
                    except Exception:
                        pass
        finally:
            user32.UnregisterHotKey(None, 1)

    def stop(self):
        if self._tid:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(self._tid, self.WM_QUIT, 0, 0)


def foreground_window():
    if not supported():
        return None
    import ctypes
    return ctypes.windll.user32.GetForegroundWindow()


def paste(root, text, hwnd=None, restore_after_ms=2500):
    """Вставить текст туда, где курсор: буфер обмена + Ctrl+V. Прежний текст буфера возвращается."""
    if not text:
        return
    try:
        old = root.clipboard_get()
    except Exception:
        old = None
    root.clipboard_clear()
    root.clipboard_append(text)
    root.update()
    if supported():
        import ctypes
        user32 = ctypes.windll.user32
        if hwnd:
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.08)
        up = 0x0002
        for vk in (0x12, 0x10, 0x5B, 0x11):              # отпустить Alt, Shift, Win, Ctrl после сочетания
            user32.keybd_event(vk, 0, up, 0)
        user32.keybd_event(0x11, 0, 0, 0)
        user32.keybd_event(0x56, 0, 0, 0)
        user32.keybd_event(0x56, 0, up, 0)
        user32.keybd_event(0x11, 0, up, 0)
    if old is not None:
        def back():
            try:
                root.clipboard_clear()
                root.clipboard_append(old)
            except Exception:
                pass
        root.after(restore_after_ms, back)
