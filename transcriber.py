# -*- coding: utf-8 -*-
"""Meeting Hub — расшифровка записей встреч.

* Запись — стерео WAV: левый канал «Вы» (микрофон), правый «Собеседники» (звук компьютера).
  Каналы расшифровываются отдельно, поэтому в тексте сразу видно, кто говорит.
* Локальный движок — faster-whisper (CTranslate2) на процессоре: int8, 2 потока,
  пропуск тишины (VAD). Звук не покидает компьютер.
* Во время встречи расшифровка ставится на паузу (не грузит процессор во время звонка)
  и продолжается после неё. Можно отменить.
* Результат рядом с записью: <запись>.txt (читаемый текст) и <запись>.transcript.json (сегменты).

Движок подключается через интерфейс Engine.transcribe(samples, language, should_stop) —
так же можно будет подключить облачный (SaluteSpeech, Groq).
"""
import json
import os
import queue
import threading
import time
import wave
from pathlib import Path

import numpy as np

RATE = 16000
SPEAKERS = ("Вы", "Собеседники")
MODELS = {
    "tiny": "самая быстрая, низкое качество (~75 МБ)",
    "base": "быстрая, среднее качество (~145 МБ)",
    "small": "рекомендуется: хорошее качество для русского (~465 МБ)",
    "medium": "лучшее качество, медленно на слабом ПК (~1,5 ГБ)",
}
DEFAULT_MODEL = "small"
MODEL_MB = {"tiny": 75, "base": 145, "small": 465, "medium": 1530}
MAX_TURN_CHARS = 280      # длиннее не склеиваем: в протоколе остаются метки времени по ходу речи
SILENCE_RMS = 0.002       # канал тише этого — не расшифровываем вовсе


class TranscriptionError(Exception):
    pass


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# Звук
# ---------------------------------------------------------------------------
def read_channels(path):
    """Прочитать запись Meeting Hub: (вы, собеседники) как float32 16 кГц."""
    with wave.open(str(path), "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        if width != 2:
            raise TranscriptionError(f"поддерживается только 16-битный WAV, а здесь {width * 8} бит")
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    if channels == 1:
        chans = [data, np.zeros(0, dtype=np.float32)]
    else:
        data = data.reshape(-1, channels)
        chans = [data[:, 0].copy(), data[:, 1].copy()]
    if rate != RATE:
        chans = [_resample(c, rate, RATE) for c in chans]
    return chans[0], chans[1]


def _resample(x, src, dst):
    if not len(x):
        return x
    n = int(round(len(x) * dst / src))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def is_silent(samples):
    return not len(samples) or float(np.sqrt(np.mean(np.square(samples, dtype=np.float64)))) < SILENCE_RMS


# ---------------------------------------------------------------------------
# Склейка и текст
# ---------------------------------------------------------------------------
def merge(mine, theirs):
    """Объединить сегменты двух каналов в один разговор по времени."""
    out = [dict(s, speaker=SPEAKERS[0]) for s in mine] + [dict(s, speaker=SPEAKERS[1]) for s in theirs]
    out.sort(key=lambda s: (s["start"], s["end"]))
    # Соседние реплики одного говорящего с паузой меньше 1,5 с склеиваем в одну.
    merged = []
    for s in out:
        if merged and merged[-1]["speaker"] == s["speaker"] and s["start"] - merged[-1]["end"] < 1.5 \
                and len(merged[-1]["text"]) + len(s["text"]) <= MAX_TURN_CHARS:
            merged[-1]["end"] = max(merged[-1]["end"], s["end"])
            merged[-1]["text"] = (merged[-1]["text"] + " " + s["text"]).strip()
        else:
            merged.append(dict(s))
    return merged


def stamp(sec):
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def format_text(segments, title=""):
    lines = [title, ""] if title else []
    for s in segments:
        lines.append(f"[{stamp(s['start'])}] {s['speaker']}: {s['text']}")
    return "\n".join(lines).strip() + "\n"


def output_paths(wav):
    wav = Path(wav)
    return wav.with_suffix(".txt"), wav.with_name(wav.stem + ".transcript.json")


def has_transcript(wav):
    txt, js = output_paths(wav)
    return txt.exists() and js.exists()


# ---------------------------------------------------------------------------
# Движки
# ---------------------------------------------------------------------------
class LocalWhisper:
    """faster-whisper на процессоре. Модель скачивается один раз в папку models."""

    name = "local"

    def __init__(self, model_dir, size=DEFAULT_MODEL, threads=2):
        self.model_dir = Path(model_dir)
        self.size = size
        self.threads = threads
        self.model = None

    @staticmethod
    def available():
        try:
            import faster_whisper  # noqa: F401
            return True, ""
        except Exception as e:
            return False, f"модуль расшифровки не установлен ({type(e).__name__}: {e})"

    def model_present(self):
        return any(self.model_dir.glob(f"models--Systran--faster-whisper-{self.size}/snapshots/*/model.bin"))

    def downloaded_mb(self):
        """Сколько уже скачано (для индикатора при первой загрузке модели)."""
        root = self.model_dir / f"models--Systran--faster-whisper-{self.size}"
        try:
            return sum(f.stat().st_size for f in root.rglob("*") if f.is_file()) / (1024 * 1024)
        except OSError:
            return 0.0

    def load(self):
        if self.model is not None:
            return
        ok, why = self.available()
        if not ok:
            raise TranscriptionError(why)
        from faster_whisper import WhisperModel
        self.model_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.model = WhisperModel(self.size, device="cpu", compute_type="int8",
                                      cpu_threads=self.threads, num_workers=1,
                                      download_root=str(self.model_dir))
        except Exception as e:
            raise TranscriptionError(
                f"не удалось загрузить модель «{self.size}»: {type(e).__name__}: {e}. "
                "Нужен интернет при первом запуске (модель скачивается один раз с huggingface.co). "
                "Если сеть закрыта, модель можно положить вручную в папку " + str(self.model_dir)) from e

    def transcribe(self, samples, language="ru", should_stop=None):
        """Вернуть сегменты [{'start','end','text'}]. should_stop() вызывается между сегментами."""
        self.load()
        segments, _info = self.model.transcribe(
            samples, language=language or None, beam_size=1, vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 700}, condition_on_previous_text=False)
        out = []
        for seg in segments:          # генератор: следующий сегмент считается только по запросу
            if should_stop is not None:
                should_stop(float(seg.end))      # позиция в секундах — для процента готовности
            text = seg.text.strip()
            if text:
                out.append({"start": round(float(seg.start), 2), "end": round(float(seg.end), 2), "text": text})
        return out


# ---------------------------------------------------------------------------
# Расшифровка одного файла
# ---------------------------------------------------------------------------
def transcribe_file(wav, engine, language="ru", should_stop=None, progress=None, title=""):
    """Расшифровать запись, записать .txt и .transcript.json. Вернуть путь к .txt."""
    wav = Path(wav)
    mine, theirs = read_channels(wav)
    duration = max(len(mine), len(theirs)) / RATE
    parts = []
    total = (0 if is_silent(mine) else len(mine)) + (0 if is_silent(theirs) else len(theirs))
    done = 0
    started = time.time()
    for samples in (mine, theirs):
        if is_silent(samples):
            parts.append([])
            continue

        def check(pos=None, base=done, n=len(samples)):
            if should_stop is not None:
                should_stop()
            if progress is not None and total and pos is not None:
                progress(min(0.99, (base + min(n, pos * RATE)) / total))
        parts.append(engine.transcribe(samples, language, check))
        done += len(samples)
        if progress is not None and total:
            progress(min(0.99, done / total))
    segments = merge(parts[0], parts[1])
    if len(theirs) == 0:
        # Одна дорожка (загруженный файл): кто говорит, неизвестно — не выдаём всё за «Вы».
        import importer
        if importer.is_imported(wav):
            for s in segments:
                s["speaker"] = importer.SPEAKER
    txt, js = output_paths(wav)
    meta = {"file": wav.name, "engine": getattr(engine, "name", "?"), "model": getattr(engine, "size", ""),
            "language": language, "duration_sec": round(duration, 1),
            "processing_sec": round(time.time() - started, 1), "segments": segments}
    tmp = js.with_name(js.name + ".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, js)
    txt.write_text(format_text(segments, title or f"Расшифровка {wav.stem}"), encoding="utf-8")
    if progress is not None:
        progress(1.0)
    return txt


# ---------------------------------------------------------------------------
# Очередь в фоне
# ---------------------------------------------------------------------------
class TranscriptionQueue:
    """Фоновая очередь расшифровки с паузой на время встречи и отменой.

    status: {путь: {'state': queued|downloading|loading|running|paused|done|error|cancelled,
                    'progress': 0..1, 'error': str, 'downloaded_mb': float}}
    """

    def __init__(self, engine_factory, language="ru", on_done=None, log=None):
        self.engine_factory = engine_factory
        self.language = language
        self.on_done = on_done
        self.log = log
        self.items = queue.Queue()
        self.status = {}
        self.paused = threading.Event()
        self.stop_event = threading.Event()
        self.cancel_current = threading.Event()
        self.current = None
        self.engine = None
        self.thread = threading.Thread(target=self._run, name="transcriber", daemon=True)
        self.thread.start()

    def add(self, wav):
        key = str(wav)
        state = self.status.get(key, {}).get("state")
        if state in ("queued", "downloading", "loading", "running", "paused"):
            return False
        self.status[key] = {"state": "queued", "progress": 0.0, "error": ""}
        self.items.put(key)
        return True

    def set_paused(self, value):
        if value:
            self.paused.set()
        else:
            self.paused.clear()

    def cancel(self, wav=None):
        if wav is None or str(wav) == self.current:
            self.cancel_current.set()
        elif str(wav) in self.status and self.status[str(wav)]["state"] == "queued":
            self.status[str(wav)]["state"] = "cancelled"

    def close(self):
        self.stop_event.set()
        self.cancel_current.set()
        self.items.put(None)

    def busy(self):
        return any(s["state"] in ("queued", "downloading", "loading", "running", "paused")
                   for s in self.status.values())

    def _should_stop(self, key):
        def check(_pos=None):
            while self.paused.is_set() and not self.stop_event.is_set() and not self.cancel_current.is_set():
                self.status[key]["state"] = "paused"
                time.sleep(0.5)
            if self.cancel_current.is_set() or self.stop_event.is_set():
                raise Cancelled()
            self.status[key]["state"] = "running"
        return check

    def _prepare(self, engine, st, check):
        """Загрузить модель заранее; при первой загрузке показывать, сколько скачано."""
        load = getattr(engine, "load", None)
        if load is None or getattr(engine, "model", True) is not None:
            return
        present = getattr(engine, "model_present", lambda: True)()
        total = MODEL_MB.get(getattr(engine, "size", ""), 0)
        st["state"] = "downloading" if not present else "loading"
        done = threading.Event()

        def watch():
            while not done.wait(1.0):
                if total and hasattr(engine, "downloaded_mb"):
                    st["downloaded_mb"] = engine.downloaded_mb()
                    st["progress"] = min(0.99, st["downloaded_mb"] / total)
        if not present:
            threading.Thread(target=watch, daemon=True).start()
        try:
            load()
        finally:
            done.set()
        check()

    def _run(self):
        _lower_priority()
        while not self.stop_event.is_set():
            key = self.items.get()
            if key is None or self.stop_event.is_set():
                break
            st = self.status.get(key)
            if not st or st["state"] != "queued":
                continue
            self.current = key
            self.cancel_current.clear()
            check = self._should_stop(key)
            try:
                check()                                    # ждём конца встречи до старта
                if self.engine is None:
                    st["state"] = "loading"
                    self.engine = self.engine_factory()
                self._prepare(self.engine, st, check)
                st["state"] = "running"
                st["progress"] = 0.0
                txt = transcribe_file(key, self.engine, self.language, check,
                                      lambda p: st.__setitem__("progress", p))
                st.update(state="done", progress=1.0, result=str(txt))
            except Cancelled:
                st["state"] = "cancelled"
            except Exception as e:
                st.update(state="error", error=f"{type(e).__name__}: {e}" if not isinstance(e, TranscriptionError)
                          else str(e))
                if self.log:
                    self.log.exception("Ошибка расшифровки %s", key)
            finally:
                self.current = None
            if self.on_done:
                try:
                    self.on_done(key, dict(st))
                except Exception:
                    pass


def _lower_priority():
    """Поток расшифровки — с пониженным приоритетом, чтобы не мешать работе."""
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.GetCurrentThread.restype = ctypes.c_void_p
        k.SetThreadPriority(ctypes.c_void_p(k.GetCurrentThread()), -1)   # THREAD_PRIORITY_BELOW_NORMAL
    except Exception:
        pass
