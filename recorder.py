# -*- coding: utf-8 -*-
"""Meeting Hub — запись встречи: микрофон (вы) и системный звук (собеседники).

Устройство записи:
  * два источника читаются в отдельных потоках и складываются в очередь;
  * один поток-писатель сразу пишет их на диск в два моно-файла *.part.wav
    (заголовок WAV обновляется после каждой записи, поэтому сбой программы
    теряет не больше последних долей секунды);
  * источники выравниваются по часам: если источник молчал и не отдавал данных
    (системный звук без воспроизведения так и делает), пропуск заполняется тишиной;
  * при остановке оба файла сводятся в один стерео-WAV:
    левый канал — микрофон, правый — системный звук.

Формат 16 кГц / 16 бит: достаточно для речи и сразу подходит для распознавания.
Час встречи ≈ 230 МБ.
"""
import json
import os
import queue
import shutil
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

RATE = 16000
BLOCK_SEC = 0.25
SOURCES = ("mic", "system")
SOURCE_LABELS = {"mic": "микрофон", "system": "звук компьютера"}
MIN_FREE_BYTES = 300 * 1024 * 1024
GAP_TOLERANCE_SEC = 0.3
MAX_GAP_SEC = 12 * 3600


class RecorderError(Exception):
    pass


# ---------------------------------------------------------------------------
# Источники звука
# ---------------------------------------------------------------------------
class SoundcardBackend:
    """Микрофон и loopback устройства вывода через пакет soundcard (WASAPI)."""

    def __init__(self):
        try:
            import soundcard  # noqa: F401
        except Exception as e:  # пакет не установлен или не загрузился
            raise RecorderError(f"модуль записи не установлен ({e}). Запустите setup.bat") from e

    def open(self, name, rate):
        import soundcard as sc
        if name == "mic":
            dev = sc.default_microphone()
        else:
            speaker = sc.default_speaker()
            dev = sc.get_microphone(id=str(speaker.id), include_loopback=True)
        rec = dev.recorder(samplerate=rate, channels=1)
        rec.__enter__()
        return _SoundcardSource(rec, getattr(dev, "name", name))


class _SoundcardSource:
    def __init__(self, rec, device):
        self.rec = rec
        self.device = device

    def read(self, frames):
        data = np.asarray(self.rec.record(numframes=frames), dtype=np.float32)
        return data[:, 0] if data.ndim == 2 else data

    def close(self):
        try:
            self.rec.__exit__(None, None, None)
        except Exception:
            pass


def available():
    """(True, '') если запись возможна, иначе (False, причина)."""
    try:
        SoundcardBackend()
        return True, ""
    except RecorderError as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Дорожка (один моно-файл)
# ---------------------------------------------------------------------------
class _Track:
    def __init__(self, path, rate):
        self.path = Path(path)
        self.rate = rate
        self.w = wave.open(str(self.path), "wb")
        self.w.setnchannels(1)
        self.w.setsampwidth(2)
        self.w.setframerate(rate)
        self.frames = 0
        self.level = 0.0
        self.device = ""
        self.error = ""
        self.started = False
        self.last_data = None

    def write_samples(self, samples):
        pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
        self.w.writeframes(pcm.tobytes())
        self.frames += len(pcm)

    def write_silence(self, frames):
        chunk = self.rate * 10
        while frames > 0:
            n = min(frames, chunk)
            self.w.writeframes(b"\x00\x00" * n)
            self.frames += n
            frames -= n

    def close(self):
        try:
            self.w.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Запись
# ---------------------------------------------------------------------------
class AudioRecorder:
    def __init__(self, folder, backend=None, clock=time.time, rate=RATE, block_sec=BLOCK_SEC):
        self.folder = Path(folder)
        self.backend = backend
        self.clock = clock
        self.rate = rate
        self.block = max(1, int(rate * block_sec))
        self.running = False
        self.t0 = None
        self.stem = ""
        self.meta = {}
        self.tracks = {}
        self._queue = queue.Queue()
        self._capture = []
        self._writer = None
        self._writer_stop = threading.Event()
        self._lock = threading.Lock()

    # ---------- запуск ----------
    def start(self, meta=None):
        if self.running:
            raise RecorderError("запись уже идёт")
        self.folder.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.folder).free
        if free < MIN_FREE_BYTES:
            raise RecorderError(f"мало места на диске: свободно {free // (1024 * 1024)} МБ")
        if self.backend is None:
            self.backend = SoundcardBackend()
        self.stem = datetime.now().strftime("%Y%m%d_%H%M%S")
        n = 1
        while (self.folder / f"{self.stem}.wav").exists() or (self.folder / f"{self.stem}.json").exists():
            self.stem = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{n}"
            n += 1
        self.t0 = self.clock()
        self.tracks = {name: _Track(self.folder / f"{self.stem}.{name}.part.wav", self.rate) for name in SOURCES}
        self.meta = dict(meta or {})
        self.meta.update({"started": datetime.now().isoformat(timespec="seconds"), "rate": self.rate,
                          "channels": {"left": "микрофон (вы)", "right": "звук компьютера (собеседники)"},
                          "status": "recording"})
        self._write_meta()
        self.running = True
        self._writer_stop.clear()
        self._writer = threading.Thread(target=self._write_loop, name="rec-writer", daemon=True)
        self._writer.start()
        self._capture = []
        for name in SOURCES:
            th = threading.Thread(target=self._capture_loop, args=(name,), name=f"rec-{name}", daemon=True)
            th.start()
            self._capture.append(th)
        return self.stem

    def _capture_loop(self, name):
        track = self.tracks[name]
        try:
            source = self.backend.open(name, self.rate)
        except Exception as e:
            track.error = f"не удалось открыть {SOURCE_LABELS[name]}: {e}"
            return
        track.device = getattr(source, "device", "")
        track.started = True
        try:
            while self.running:
                block = source.read(self.block)
                t = self.clock()
                if not self.running:
                    break
                if block is not None and len(block):
                    self._queue.put((name, t, block))
        except Exception as e:
            track.error = f"{SOURCE_LABELS[name]}: запись прервалась ({e})"
        finally:
            source.close()

    def _write_loop(self):
        while not (self._writer_stop.is_set() and self._queue.empty()):
            try:
                name, t, block = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.write_block(name, t, block)
            except Exception as e:
                self.tracks[name].error = f"ошибка записи на диск: {e}"

    def write_block(self, name, t, block):
        """Записать блок, при необходимости заполнив пропуск тишиной. t — время конца блока."""
        track = self.tracks[name]
        with self._lock:
            expected_start = int(round((t - self.t0) * self.rate)) - len(block)
            gap = expected_start - track.frames
            if gap > self.rate * GAP_TOLERANCE_SEC:
                track.write_silence(min(gap, self.rate * MAX_GAP_SEC))
            track.write_samples(block)
            rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64)))) if len(block) else 0.0
            track.level = min(1.0, rms * 6.0)
            track.last_data = t

    # ---------- состояние ----------
    def elapsed(self):
        return 0.0 if not self.running or self.t0 is None else max(0.0, self.clock() - self.t0)

    def health(self):
        """Состояние источников для интерфейса: {name: {'level', 'error', 'started'}}."""
        now = self.clock()
        out = {}
        for name, tr in self.tracks.items():
            level = tr.level if tr.last_data and now - tr.last_data < 1.5 else 0.0
            out[name] = {"level": level, "error": tr.error, "started": tr.started, "device": tr.device}
        return out

    # ---------- остановка ----------
    def stop(self, cut_at=None):
        """Остановить и сохранить запись. cut_at — время, после которого звук не нужен.

        Возвращает словарь с путём к файлу, длительностью и ошибками.
        """
        if not self.running:
            return None
        t_stop = self.clock()
        self.running = False
        for th in self._capture:
            th.join(timeout=2.0)
        self._writer_stop.set()
        if self._writer is not None:
            self._writer.join(timeout=15.0)
        end = t_stop if cut_at is None else max(self.t0, min(t_stop, cut_at))
        target = max(0, int(round((end - self.t0) * self.rate)))
        with self._lock:
            for tr in self.tracks.values():
                if tr.frames < target:
                    tr.write_silence(target - tr.frames)
                tr.close()
        out = self.folder / f"{self.stem}.wav"
        merge_tracks(self.tracks["mic"].path, self.tracks["system"].path, out, target, self.rate)
        for tr in self.tracks.values():
            try:
                tr.path.unlink()
            except OSError:
                pass
        errors = [tr.error for tr in self.tracks.values() if tr.error]
        self.meta.update({
            "ended": datetime.now().isoformat(timespec="seconds"),
            "duration_sec": round(target / self.rate, 1),
            "trimmed_sec": round(max(0.0, t_stop - end), 1),
            "file": out.name,
            "devices": {name: tr.device for name, tr in self.tracks.items()},
            "errors": errors,
            "status": "saved",
        })
        self._write_meta()
        return {"path": out, "duration": target / self.rate, "errors": errors,
                "trimmed": max(0.0, t_stop - end)}

    def _write_meta(self):
        path = self.folder / f"{self.stem}.json"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(self.meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Сведение и восстановление
# ---------------------------------------------------------------------------
def _read_mono(path):
    try:
        return wave.open(str(path), "rb")
    except (OSError, EOFError, wave.Error):
        return None


def merge_tracks(mic_path, system_path, out_path, frames=None, rate=RATE):
    """Свести два моно-файла в стерео (L — микрофон, R — системный звук) кусками по 10 с."""
    readers = [_read_mono(mic_path), _read_mono(system_path)]
    if frames is None:
        frames = max((r.getnframes() for r in readers if r), default=0)
    tmp = Path(str(out_path) + ".tmp")
    w = wave.open(str(tmp), "wb")
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(rate)
    chunk = rate * 10
    done = 0
    try:
        while done < frames:
            n = min(chunk, frames - done)
            chans = []
            for r in readers:
                data = np.frombuffer(r.readframes(n), dtype="<i2") if r else np.zeros(0, dtype="<i2")
                if len(data) < n:
                    data = np.concatenate([data, np.zeros(n - len(data), dtype="<i2")])
                chans.append(data)
            w.writeframes(np.column_stack(chans).astype("<i2").tobytes())
            done += n
    finally:
        w.close()
        for r in readers:
            if r:
                r.close()
    os.replace(tmp, out_path)
    return frames


def recover_unfinished(folder, rate=RATE):
    """Свести записи, оставшиеся после сбоя (есть *.part.wav, нет итогового файла)."""
    folder = Path(folder)
    if not folder.exists():
        return []
    stems = sorted({p.name.split(".")[0] for p in folder.glob("*.part.wav")})
    recovered = []
    for stem in stems:
        out = folder / f"{stem}.wav"
        mic = folder / f"{stem}.mic.part.wav"
        system = folder / f"{stem}.system.part.wav"
        if out.exists():
            continue
        try:
            frames = merge_tracks(mic, system, out, None, rate)
        except Exception:
            continue
        for p in (mic, system):
            try:
                p.unlink()
            except OSError:
                pass
        meta_path = folder / f"{stem}.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        meta.update({"status": "recovered", "file": out.name, "duration_sec": round(frames / rate, 1),
                     "note": "Запись восстановлена после неожиданного закрытия программы"})
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        recovered.append(out)
    return recovered


def list_recordings(folder):
    """Список сохранённых записей (новые первыми) с данными из JSON рядом с файлом."""
    folder = Path(folder)
    items = []
    if not folder.exists():
        return items
    for wav in folder.glob("*.wav"):
        if wav.name.endswith(".part.wav"):
            continue
        meta = {}
        try:
            meta = json.loads(wav.with_suffix(".json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if "duration_sec" not in meta:
            try:
                with wave.open(str(wav), "rb") as r:
                    meta["duration_sec"] = round(r.getnframes() / r.getframerate(), 1)
            except (OSError, EOFError, wave.Error):
                meta["duration_sec"] = 0
        items.append({"path": wav, "size": wav.stat().st_size, **meta})
    items.sort(key=lambda m: m["path"].name, reverse=True)
    return items
