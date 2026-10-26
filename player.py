# -*- coding: utf-8 -*-
"""Прослушивание записи внутри программы: пауза, перемотка, переход к реплике.

Запись — стерео (слева вы, справа собеседники); для прослушивания каналы сводятся в один,
чтобы обе стороны разговора звучали в обоих ушах. Вывод — PortAudio (PyAudioWPatch).
"""
import threading
import wave
from pathlib import Path

import numpy as np


class PlayerError(Exception):
    pass


def load_mono(path):
    """Прочитать WAV Meeting Hub и свести в моно int16. Вернуть (samples, rate)."""
    with wave.open(str(path), "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        if width != 2:
            raise PlayerError(f"неподдерживаемый формат ({width * 8} бит)")
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    if ch > 1:
        data = data.reshape(-1, ch).astype(np.int32).sum(axis=1)
        peak = int(np.abs(data).max()) if len(data) else 0
        if peak > 32767:                              # без перегруза при громких обоих каналах
            data = data * (32767 / peak)
        data = data.astype(np.int16)
    return np.ascontiguousarray(data), rate


class PortAudioOut:
    """Вывод звука через PortAudio в режиме обратного вызова."""

    def __init__(self):
        try:
            import pyaudiowpatch as pyaudio
        except ImportError:
            try:
                import pyaudio  # type: ignore
            except ImportError as e:
                raise PlayerError("нет модуля вывода звука (PyAudioWPatch)") from e
        self.pa = pyaudio
        self.p = pyaudio.PyAudio()
        self.stream = None

    def start(self, rate, pull):
        def cb(_in, frames, _time, _status):
            data, more = pull(frames)
            return data, (self.pa.paContinue if more else self.pa.paComplete)
        self.stop()
        try:
            self.stream = self.p.open(format=self.pa.paInt16, channels=1, rate=rate, output=True,
                                      frames_per_buffer=1024, stream_callback=cb)
            self.stream.start_stream()
        except Exception as e:
            self.stream = None
            raise PlayerError(f"не удалось открыть динамики: {e}") from e

    def stop(self):
        if self.stream is not None:
            s, self.stream = self.stream, None
            try:
                s.stop_stream()
                s.close()
            except Exception:
                pass

    def close(self):
        self.stop()
        try:
            self.p.terminate()
        except Exception:
            pass


class Player:
    """Состояние проигрывателя; звук выводит backend (start(rate, pull) / stop() / close())."""

    def __init__(self, backend_factory=PortAudioOut):
        self.backend_factory = backend_factory
        self.backend = None
        self.path = None
        self.samples = np.zeros(0, dtype=np.int16)
        self.rate = 16000
        self.pos = 0
        self.playing = False
        self.lock = threading.Lock()

    # --- данные
    def load(self, path):
        path = Path(path)
        if self.path == path:
            return
        self.stop()
        self.samples, self.rate = load_mono(path)
        self.path, self.pos = path, 0

    @property
    def duration(self):
        return len(self.samples) / self.rate if self.rate else 0.0

    @property
    def position(self):
        return self.pos / self.rate if self.rate else 0.0

    def seek(self, sec):
        with self.lock:
            self.pos = int(max(0.0, min(sec, self.duration)) * self.rate)

    def pull(self, frames):
        """Следующие frames отсчётов (байты) и есть ли продолжение. Вызывается из потока звука."""
        with self.lock:
            chunk = self.samples[self.pos:self.pos + frames]
            self.pos += len(chunk)
            more = self.pos < len(self.samples)
            if not more:
                self.playing = False
        if len(chunk) < frames:
            chunk = np.concatenate([chunk, np.zeros(frames - len(chunk), dtype=np.int16)])
        return chunk.tobytes(), more

    # --- управление
    def play(self):
        if self.path is None or not len(self.samples):
            raise PlayerError("нечего проигрывать")
        if self.pos >= len(self.samples):
            self.pos = 0
        if self.backend is None:
            self.backend = self.backend_factory()
        self.playing = True
        try:
            self.backend.start(self.rate, self.pull)
        except Exception:
            self.playing = False
            raise

    def pause(self):
        self.playing = False
        if self.backend is not None:
            self.backend.stop()

    def toggle(self):
        if self.playing:
            self.pause()
        else:
            self.play()

    def stop(self):
        self.pause()
        self.pos = 0

    def close(self):
        self.pause()
        if self.backend is not None:
            self.backend.close()
            self.backend = None
