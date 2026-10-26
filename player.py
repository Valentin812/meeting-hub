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
    """Вывод через PortAudio. Перебирает устройства и форматы: не все динамики принимают 16 кГц моно."""

    name = "PortAudio"

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
        self.config = None

    def candidates(self, rate):
        """(индекс устройства, частота, каналы) — сначала устройство по умолчанию в его родном формате."""
        out, seen = [], set()
        devices = []
        try:
            devices.append(self.p.get_default_output_device_info())
        except Exception:
            pass
        for api in ("paWASAPI", "paMME", "paDirectSound"):
            try:
                info = self.p.get_host_api_info_by_type(getattr(self.pa, api))
                if info.get("defaultOutputDevice", -1) >= 0:
                    devices.append(self.p.get_device_info_by_index(info["defaultOutputDevice"]))
            except Exception:
                pass
        for dev in devices:
            idx = dev["index"]
            native = int(dev.get("defaultSampleRate") or 48000)
            chans = max(1, min(2, int(dev.get("maxOutputChannels") or 2)))
            for r, c in ((native, chans), (native, 2), (48000, 2), (44100, 2), (rate, 1), (rate, 2)):
                key = (idx, r, c)
                if key not in seen:
                    seen.add(key)
                    out.append(key)
        return out

    def start(self, rate, pull, player=None):
        self.stop()
        errors = []
        for idx, r, c in self.candidates(rate):
            def cb(_in, frames, _time, _status, r=r, c=c):
                data, more = player.pull_converted(frames, r, c) if player is not None else pull(frames)
                return data, (self.pa.paContinue if more else self.pa.paComplete)
            try:
                self.stream = self.p.open(format=self.pa.paInt16, channels=c, rate=r, output=True,
                                          output_device_index=idx, frames_per_buffer=1024, stream_callback=cb)
                self.stream.start_stream()
                self.config = (idx, r, c)
                return
            except Exception as e:
                self.stream = None
                errors.append(f"{r} Гц/{c} к: {e}")
        raise PlayerError("динамики не открылись (" + "; ".join(errors[:3]) + ")")

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


class WinsoundOut:
    """Запасной вывод через системный winmm (как стандартные звуки Windows). Позиция — по часам."""

    name = "Windows"

    def __init__(self):
        try:
            import winsound
        except ImportError as e:
            raise PlayerError("winsound доступен только в Windows") from e
        self.ws = winsound
        self.player = None
        self.t0 = None
        self.pos0 = 0
        self.buf = None

    def start(self, rate, pull, player=None):
        import io
        import time
        self.stop()
        self.player = player
        data = player.samples[player.pos:]
        bio = io.BytesIO()
        with wave.open(bio, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(data.tobytes())
        self.buf = bio.getvalue()
        try:
            self.ws.PlaySound(self.buf, self.ws.SND_MEMORY | self.ws.SND_ASYNC | self.ws.SND_NODEFAULT)
        except Exception as e:
            self.buf = None
            raise PlayerError(f"Windows не воспроизвёл звук: {e}") from e
        self.t0, self.pos0 = time.monotonic(), player.pos

    def current_pos(self):
        import time
        if self.t0 is None or self.player is None:
            return None
        return self.pos0 + int((time.monotonic() - self.t0) * self.player.rate)

    def stop(self):
        if self.t0 is not None and self.player is not None:
            pos = self.current_pos()
            self.player.pos = min(pos, len(self.player.samples))
        self.t0 = None
        try:
            self.ws.PlaySound(None, 0)
        except Exception:
            pass
        self.buf = None

    def close(self):
        self.stop()


def default_backends():
    """Сначала PortAudio (точная позиция), если не вышло — системный вывод Windows."""
    return [PortAudioOut, WinsoundOut]


class Player:
    """Состояние проигрывателя; звук выводит backend (start(rate, pull) / stop() / close())."""

    def __init__(self, backend_factory=None, backends=None):
        self.backends = list(backends or ([backend_factory] if backend_factory else default_backends()))
        self.backend = None
        self.errors = []
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
        cur = getattr(self.backend, "current_pos", None)
        if self.playing and cur is not None:
            pos = cur()
            if pos is not None:
                if pos >= len(self.samples):
                    self.playing = False
                    pos = len(self.samples)
                self.pos = pos
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

    def pull_converted(self, frames, out_rate, channels):
        """Как pull, но для устройства с другой частотой и числом каналов (простая интерполяция)."""
        if out_rate == self.rate:
            data, more = self.pull(frames)
            mono = np.frombuffer(data, dtype=np.int16)
        else:
            need = int(np.ceil(frames * self.rate / out_rate)) + 1
            with self.lock:
                chunk = self.samples[self.pos:self.pos + need]
                self.pos += max(0, need - 1) if len(chunk) == need else len(chunk)
                more = self.pos < len(self.samples)
                if not more:
                    self.playing = False
            if len(chunk) < 2:
                mono = np.zeros(frames, dtype=np.int16)
            else:
                x = np.linspace(0, len(chunk) - 1, frames)
                mono = np.interp(x, np.arange(len(chunk)), chunk.astype(np.float32)).astype(np.int16)
        if channels > 1:
            mono = np.repeat(mono, channels)
        return mono.tobytes(), more

    # --- управление
    def play(self):
        if self.path is None or not len(self.samples):
            raise PlayerError("нечего проигрывать")
        if self.pos >= len(self.samples):
            self.pos = 0
        self.playing = True
        if self.backend is not None:
            try:
                self.backend.start(self.rate, self.pull, player=self)
                return
            except Exception as e:
                self.errors.append(f"{getattr(self.backend, 'name', '?')}: {e}")
                self._drop_backend()
        for factory in self.backends:                     # первый способ вывода, который заработает
            try:
                backend = factory()
                backend.start(self.rate, self.pull, player=self)
                self.backend = backend
                return
            except Exception as e:
                self.errors.append(f"{getattr(factory, 'name', getattr(factory, '__name__', '?'))}: {e}")
        self.playing = False
        raise PlayerError("не удалось воспроизвести звук. " + " | ".join(self.errors[-2:]))

    def _drop_backend(self):
        try:
            self.backend.close()
        except Exception:
            pass
        self.backend = None

    def pause(self):
        if self.backend is not None:
            self.backend.stop()
        self.playing = False

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
