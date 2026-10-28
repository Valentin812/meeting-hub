# -*- coding: utf-8 -*-
"""Meeting Hub — расшифровка моделью GigaAM (Сбер, открытая, только русский язык).

Модель работает локально через sherpa-onnx (библиотека уже есть в программе для распознавания голосов),
звук никуда не уходит. Запись режется по паузам на реплики до ~20 секунд — так модель работает лучше всего.
Модель скачивается один раз (около 230 МБ). Если GigaAM не загрузилась или упала, GigaAMWithFallback
сам переходит на Whisper, а для не-русского языка сразу использует Whisper.
"""
import os
import shutil
import tempfile
import time
import urllib.request
from pathlib import Path

import numpy as np

import transcriber as tr

RATE = 16000
MODEL_NAME = "sherpa-onnx-nemo-transducer-giga-am-v2-russian-2025-04-19"
MODEL_URL = f"https://huggingface.co/csukuangfj/{MODEL_NAME}/resolve/main"
FILES = ("encoder.int8.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt")
MIN_BYTES = {"encoder.int8.onnx": 100_000_000, "decoder.onnx": 500_000, "joiner.onnx": 200_000, "tokens.txt": 50}
EXPECTED_BYTES = {"encoder.int8.onnx": 236_000_000, "decoder.onnx": 3_300_000, "joiner.onnx": 1_400_000,
                  "tokens.txt": 200}
COOLDOWN_SEC = 600
MAX_PIECE_SEC = 20.0


class GigaAMError(Exception):
    pass


def available():
    try:
        import sherpa_onnx  # noqa: F401
        return True, ""
    except Exception as e:
        return False, f"модуль sherpa-onnx не установлен ({type(e).__name__}: {e})"


def model_dir(base):
    return Path(base) / "gigaam" / MODEL_NAME


def model_ready(base):
    d = model_dir(base)
    try:
        return all((d / f).is_file() and (d / f).stat().st_size >= MIN_BYTES[f] for f in FILES)
    except OSError:
        return False


def download_model(base, progress=None, url=MODEL_URL, opener=urllib.request.urlopen):
    """Скачать файлы модели (около 240 МБ) по одному, без распаковки архивов. Возвращает папку модели."""
    dest = model_dir(base)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=str(dest.parent), prefix="unpack-"))
    sizes = dict(EXPECTED_BYTES)
    total, got = sum(sizes.values()), 0
    try:
        for name in FILES:
            with opener(f"{url}/{name}", timeout=60) as resp, open(staging / (name + ".part"), "wb") as out:
                while True:
                    block = resp.read(1 << 18)
                    if not block:
                        break
                    out.write(block)
                    got += len(block)
                    if progress is not None:
                        progress(min(1.0, got / total))
            part = staging / (name + ".part")
            if part.stat().st_size < MIN_BYTES[name]:
                raise GigaAMError("модель скачалась не полностью")
            os.replace(part, staging / name)
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        os.replace(staging, dest)
    except GigaAMError:
        raise
    except OSError as e:
        raise GigaAMError(f"не удалось скачать модель GigaAM: {e}. Файлы можно положить вручную в папку {dest}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return dest


# ---------------------------------------------------------------------------
# Нарезка по паузам
# ---------------------------------------------------------------------------
def split_speech(samples, rate=RATE, frame_sec=0.03, min_pause=0.45, max_len=MAX_PIECE_SEC, pad=0.15,
                 min_len=0.2):
    """Реплики по паузам: [(от, до)] в отсчётах. Длинные куски режутся в самом тихом месте."""
    x = np.asarray(samples, dtype=np.float32)
    frame = int(frame_sec * rate)
    n = len(x) // frame
    if n == 0:
        return []
    rms = np.sqrt((x[:n * frame].reshape(n, frame) ** 2).mean(axis=1))
    thr = max(0.004, 0.12 * float(np.percentile(rms, 90)))
    voiced = np.flatnonzero(rms > thr)
    if len(voiced) == 0:
        return []
    gap = int(min_pause / frame_sec)
    runs, start, prev = [], voiced[0], voiced[0]
    for i in voiced[1:]:
        if i - prev > gap:
            runs.append((start, prev))
            start = i
        prev = i
    runs.append((start, prev))
    padf, maxf = int(pad / frame_sec), int(max_len / frame_sec)
    pieces = []
    for a, b in runs:
        a, b = max(0, a - padf), min(n, b + 1 + padf)
        while b - a > maxf:                                  # слишком длинная реплика: режем по тишине
            lo = a + maxf // 2
            cut = lo + int(rms[lo:a + maxf].argmin())
            pieces.append((a, cut))
            a = cut
        pieces.append((a, b))
    return [(a * frame, min(len(x), b * frame)) for a, b in pieces if (b - a) * frame_sec >= min_len]


def polish(text):
    """GigaAM v2 отдаёт строчные буквы без знаков: делаем читаемее (заглавная буква и точка)."""
    t = " ".join((text or "").split())
    if not t:
        return ""
    t = t[0].upper() + t[1:]
    return t if t[-1] in ".!?…" else t + "."


# ---------------------------------------------------------------------------
# Движок
# ---------------------------------------------------------------------------
class GigaAM:
    name = "gigaam"
    size = "gigaam"

    def __init__(self, base_dir, threads=2):
        self.base_dir, self.threads = Path(base_dir), threads
        self.model = None
        self._part = None

    def model_present(self):
        return model_ready(self.base_dir)

    def downloaded_mb(self):
        root = model_dir(self.base_dir).parent
        try:
            return sum(f.stat().st_size for f in root.rglob("*") if f.is_file()) / (1024 * 1024)
        except OSError:
            return 0.0

    def load(self):
        if self.model is not None:
            return
        ok, why = available()
        if not ok:
            raise GigaAMError(why)
        if not model_ready(self.base_dir):
            download_model(self.base_dir)
        d = model_dir(self.base_dir)
        import sherpa_onnx
        args = dict(encoder=str(d / FILES[0]), decoder=str(d / FILES[1]), joiner=str(d / FILES[2]),
                    tokens=str(d / FILES[3]), num_threads=self.threads, sample_rate=RATE,
                    decoding_method="greedy_search", model_type="nemo_transducer")
        try:
            try:
                self.model = sherpa_onnx.OfflineRecognizer.from_transducer(feature_dim=64, **args)
            except TypeError:                                  # другая версия библиотеки
                self.model = sherpa_onnx.OfflineRecognizer.from_transducer(**args)
        except Exception as e:
            raise GigaAMError(f"не удалось загрузить модель GigaAM: {type(e).__name__}: {e}") from e

    def transcribe(self, samples, language="ru", should_stop=None):
        self.load()
        x = np.asarray(samples, dtype=np.float32)
        out = []
        for a, b in split_speech(x):
            if should_stop is not None:
                should_stop(b / RATE)
            stream = self.model.create_stream()
            stream.accept_waveform(RATE, x[a:b])
            self.model.decode_stream(stream)
            text = polish(stream.result.text)
            if text:
                out.append({"start": round(a / RATE, 2), "end": round(b / RATE, 2), "text": text})
        return out


class GigaAMWithFallback:
    """Основной движок GigaAM; при ошибке (нет модели, нет сети, сбой) — Whisper. Не-русский язык — сразу Whisper."""

    def __init__(self, primary, fallback, notify=None, clock=time.time):
        self.primary, self.fallback, self.notify, self.clock = primary, fallback, notify, clock
        self.name = "gigaam"
        self.fallback_reason = ""
        self._skip_until = 0.0
        self._primary_ok = False

    @property
    def size(self):
        return self.primary.size if self._using_primary() else self.fallback.size

    @property
    def model(self):
        return self.primary.model if self._using_primary() else self.fallback.model

    def _using_primary(self):
        return self.clock() >= self._skip_until

    def model_present(self):
        return self.primary.model_present() if self._using_primary() else self.fallback.model_present()

    def downloaded_mb(self):
        return self.primary.downloaded_mb() if self._using_primary() else self.fallback.downloaded_mb()

    def _fail(self, reason):
        self._skip_until = self.clock() + COOLDOWN_SEC
        self.fallback_reason = reason
        self.name = "local (запасной)"
        if self.notify:
            self.notify(reason)

    def load(self):
        if self._using_primary():
            try:
                self.primary.load()
                return
            except Exception as e:
                if isinstance(e, tr.Cancelled):
                    raise
                self._fail(str(e))
        self.fallback.load()

    def transcribe(self, samples, language="ru", should_stop=None):
        if language not in ("ru", "", None):
            self.name = "local"
            return self.fallback.transcribe(samples, language, should_stop)
        if self._using_primary():
            try:
                result = self.primary.transcribe(samples, language, should_stop)
                self.name = "gigaam"
                return result
            except Exception as e:
                if isinstance(e, tr.Cancelled):
                    raise
                self._fail(str(e) if isinstance(e, GigaAMError) else f"{type(e).__name__}: {e}")
        self.name = "local (запасной)"
        return self.fallback.transcribe(samples, language, should_stop)
