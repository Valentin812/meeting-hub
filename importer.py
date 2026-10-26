# -*- coding: utf-8 -*-
"""Свои файлы: аудио и видео из любого места превращаются в обычную запись Meeting Hub.

Из видео берётся только звуковая дорожка (16 кГц, моно, WAV рядом с остальными записями).
Исходный файл не меняется и не копируется; видеоряд не сохраняется.
Декодирует PyAV (входит в faster-whisper, значит есть в установленной программе); если его нет —
ffmpeg из PATH.
"""
import json
import os
import re
import shutil
import subprocess
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

RATE = 16000
AUDIO_EXT = (".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wma", ".amr", ".aiff")
VIDEO_EXT = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".wmv", ".flv", ".m4v", ".ts", ".3gp", ".mpg", ".mpeg")
SPEAKER = "Участники"


class ImportFailed(Exception):
    pass


class Cancelled(Exception):
    pass


def filetypes():
    media = " ".join("*" + e for e in AUDIO_EXT + VIDEO_EXT)
    return [("Аудио и видео", media), ("Все файлы", "*.*")]


def is_media(path):
    return Path(path).suffix.lower() in AUDIO_EXT + VIDEO_EXT


def _chunks_av(path, progress, should_stop):
    import av
    try:
        container = av.open(str(path))
    except Exception as e:
        raise ImportFailed(f"файл не открывается: {e}")
    try:
        streams = container.streams.audio
        if not streams:
            raise ImportFailed("в файле нет звуковой дорожки")
        stream = streams[0]
        total = float(container.duration) / 1_000_000 if container.duration else 0.0
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        for frame in container.decode(stream):
            if should_stop is not None:
                should_stop()
            for out in resampler.resample(frame):
                yield out.to_ndarray().reshape(-1).astype("<i2", copy=False).tobytes()
            if progress is not None and total and frame.time is not None:
                progress(min(0.99, frame.time / total))
        for out in resampler.resample(None) or []:
            yield out.to_ndarray().reshape(-1).astype("<i2", copy=False).tobytes()
    finally:
        container.close()


def _chunks_ffmpeg(path, progress, should_stop):
    exe = shutil.which("ffmpeg")
    if not exe:
        raise ImportFailed("не найден декодер звука (ffmpeg)")
    proc = subprocess.Popen([exe, "-v", "error", "-nostdin", "-i", str(path), "-vn", "-ac", "1",
                             "-ar", str(RATE), "-f", "s16le", "-"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        got = False
        while True:
            if should_stop is not None:
                should_stop()
            block = proc.stdout.read(RATE * 2 * 30)
            if not block:
                break
            got = True
            yield block
        proc.wait()
        if not got:
            err = proc.stderr.read().decode("utf-8", "replace").strip().splitlines()
            raise ImportFailed("в файле нет звука или он не читается" + (f": {err[-1]}" if err else ""))
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()


def decode_chunks(path, progress=None, should_stop=None):
    """Куски 16-битного моно PCM 16 кГц (bytes)."""
    try:
        import av  # noqa: F401
    except ImportError:
        return _chunks_ffmpeg(path, progress, should_stop)
    return _chunks_av(path, progress, should_stop)


def _stem(folder, when):
    base = when.strftime("%Y%m%d_%H%M%S")
    stem, n = base, 1
    while (folder / f"{stem}.wav").exists() or (folder / f"{stem}.json").exists():
        n += 1
        stem = f"{base}_{n}"
    return stem


def title_from_name(path):
    name = Path(path).stem.replace("_", " ").strip()
    return re.sub(r"\s+", " ", name) or "Загруженный файл"


def import_file(src, folder, title="", description="", progress=None, should_stop=None):
    """Добавить файл в папку записей. Возвращает путь к новому WAV."""
    src, folder = Path(src), Path(folder)
    if not src.is_file():
        raise ImportFailed("файл не найден")
    folder.mkdir(parents=True, exist_ok=True)
    try:
        when = datetime.fromtimestamp(src.stat().st_mtime)
    except (OSError, ValueError):
        when = datetime.now()
    stem = _stem(folder, when)
    part = folder / f"{stem}.part.wav"
    wav = folder / f"{stem}.wav"
    frames = 0
    try:
        with wave.open(str(part), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            for block in decode_chunks(src, progress, should_stop):
                block = block[: len(block) // 2 * 2]
                w.writeframes(block)
                frames += len(block) // 2
        if frames < RATE // 2:
            raise ImportFailed("в файле нет звука")
        os.replace(part, wav)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise
    meta = {"title": title.strip() or title_from_name(src), "description": description.strip(),
            "imported": True, "source_file": src.name, "duration_sec": round(frames / RATE, 1),
            "meeting": "Файл", "recorded_at": when.isoformat(timespec="seconds"),
            "added_at": datetime.now().isoformat(timespec="seconds")}
    write_meta(wav, meta)
    if progress is not None:
        progress(1.0)
    return wav


def write_meta(wav, fields):
    """Дописать поля в JSON рядом с записью (название, описание…). Остальное сохраняется."""
    path = Path(wav).with_suffix(".json")
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    meta.update(fields)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return meta


def is_imported(wav):
    try:
        return bool(json.loads(Path(wav).with_suffix(".json").read_text(encoding="utf-8")).get("imported"))
    except (OSError, ValueError):
        return False
