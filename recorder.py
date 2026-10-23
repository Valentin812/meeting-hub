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
    """Микрофон и loopback устройства вывода через пакет soundcard (WASAPI).

    Важно для Windows: каждый поток, работающий со звуком, должен сам подключиться
    к COM (CoInitializeEx). Главный поток Tkinter работает в однопоточном режиме COM,
    поэтому потоки записи без собственной инициализации получают ошибку.
    """

    def __init__(self):
        try:
            import soundcard  # noqa: F401
        except Exception as e:  # пакет не установлен или не загрузился
            raise RecorderError(f"модуль записи не установлен ({e}). Запустите setup.bat") from e

    @staticmethod
    def thread_enter():
        if os.name != "nt":
            return False
        import ctypes
        hr = ctypes.windll.ole32.CoInitializeEx(None, 0x0)  # COINIT_MULTITHREADED
        return hr in (0, 1)   # S_OK / S_FALSE — нужно парное CoUninitialize

    @staticmethod
    def thread_exit(token):
        if token:
            import ctypes
            ctypes.windll.ole32.CoUninitialize()

    # Варианты открытия устройства: если Windows отвергает формат (например «invalid argument»),
    # пробуем следующий. Первым — то, что работало в 1.2.0.
    CONFIGS = [(48000, 1, None), (48000, 2, None), (44100, 1, None), (44100, 2, None),
               (48000, 1, 4800), (48000, 2, 4800), (16000, 1, None), (96000, 2, None)]

    def device(self, name):
        import soundcard as sc
        if name == "mic":
            return sc.default_microphone()
        speaker = sc.default_speaker()
        return sc.get_microphone(id=str(speaker.id), include_loopback=True)

    def open(self, name, rate):
        dev = self.device(name)
        errors = []
        # 1) Родной формат устройства без преобразований Windows: его WASAPI в общем режиме
        #    принимает всегда. Нужен для Windows 10, где запрос «48 кГц моно» с автоконвертацией
        #    у некоторых USB-устройств отклоняется с «invalid argument».
        for buffer_hns in (2_000_000, 0):
            try:
                rec = native_recorder(dev, buffer_hns)
                rec.__enter__()
            except Exception as e:
                errors.append(f"родной формат, буфер {buffer_hns // 10000} мс: {describe_error(e)}")
                continue
            src = _SoundcardSource(rec, getattr(dev, "name", name), rec.samplerate, rate)
            src.config = f"родной формат устройства: {rec.samplerate} Гц, каналов {len(rec.channelmap)}"
            src.tried = errors
            return src
        # 2) Запасной путь — форматы с автоконвертацией внутри Windows (так работала 1.2.0).
        for dev_rate, channels, blocksize in self.CONFIGS:
            try:
                rec = dev.recorder(samplerate=dev_rate, channels=channels, blocksize=blocksize)
                rec.__enter__()
            except Exception as e:
                errors.append(f"{dev_rate}/{channels}/{blocksize}: {describe_error(e)}")
                continue
            src = _SoundcardSource(rec, getattr(dev, "name", name), dev_rate, rate)
            src.config = f"{dev_rate} Гц, каналов {channels}, блок {blocksize or 'по умолчанию'}"
            src.tried = errors
            return src
        reasons = {e.split(": ", 1)[1] for e in errors}
        if len(reasons) == 1:   # одна и та же причина для всех форматов — формат ни при чём
            raise RecorderError(f"{getattr(dev, 'name', name)}: {reasons.pop()}")
        raise RecorderError(f"{getattr(dev, 'name', name)}: ни один формат не подошёл — " + " | ".join(errors[:3]))


def native_recorder(dev, buffer_hns=2_000_000):
    """Рекордер soundcard, инициализированный родным форматом устройства (GetMixFormat без изменений)."""
    from soundcard import mediafoundation as mf

    class NativeRecorder(mf._Recorder):
        def __init__(self, ptr, isloopback):
            self._ptr = ptr
            pp = mf._ffi.new("WAVEFORMATEXTENSIBLE**")
            hr = ptr[0][0].lpVtbl.GetMixFormat(ptr[0], pp)
            mf._com.check_error(hr)
            try:
                fmt = pp[0][0].Format
                if fmt.wBitsPerSample != 32:
                    raise RecorderError(f"родной формат {fmt.wBitsPerSample} бит не поддерживается")
                self.samplerate = int(fmt.nSamplesPerSec)
                self.channelmap = list(range(int(fmt.nChannels)))
                flags = 0x00020000 if isloopback else 0   # AUDCLNT_STREAMFLAGS_LOOPBACK
                hr = ptr[0][0].lpVtbl.Initialize(ptr[0], mf._ole32.AUDCLNT_SHAREMODE_SHARED, flags,
                                                 buffer_hns, 0, pp[0], mf._ffi.NULL)
                mf._com.check_error(hr)
            finally:
                mf._ole32.CoTaskMemFree(pp[0])
            self._idle_start_time = None

    return NativeRecorder(dev._audio_client(), getattr(dev, "isloopback", False))


class _SoundcardSource:
    def __init__(self, rec, device, dev_rate, rate):
        self.rec = rec
        self.device = device
        self.dev_rate = dev_rate
        self.rate = rate

    def read(self, frames):
        need = int(round(frames * self.dev_rate / self.rate))
        data = np.asarray(self.rec.record(numframes=need), dtype=np.float32)
        data = data.mean(axis=1) if data.ndim == 2 else data
        return resample(data, self.dev_rate, self.rate)

    def close(self):
        try:
            self.rec.__exit__(None, None, None)
        except Exception:
            pass


def resample(samples, src_rate, dst_rate):
    """Перевести блок в нужную частоту: целое отношение — усреднением, иначе — линейной интерполяцией."""
    if src_rate == dst_rate or not len(samples):
        return samples.astype(np.float32, copy=False)
    if src_rate % dst_rate == 0:
        return downsample(samples, src_rate // dst_rate)
    n = int(round(len(samples) * dst_rate / src_rate))
    x_new = np.linspace(0, len(samples) - 1, n)
    return np.interp(x_new, np.arange(len(samples)), samples).astype(np.float32)


def downsample(samples, factor):
    """Понизить частоту в целое число раз усреднением (для речи достаточно)."""
    if factor <= 1:
        return samples
    usable = len(samples) - len(samples) % factor
    return samples[:usable].reshape(-1, factor).mean(axis=1).astype(np.float32)


def describe_error(e):
    """Понятный текст ошибки soundcard/Windows."""
    text = f"{type(e).__name__}: {e}".strip()
    hints = {
        "0x800401f0": "COM не инициализирован в потоке записи",
        "0x80070490": "устройство не найдено — проверьте устройства по умолчанию в «Параметры → Звук»",
        "0x88890004": "устройство отключено или занято",
        "0x8889000a": "устройство занято другим приложением в монопольном режиме",
        "0x88890008": "формат записи не поддерживается устройством",
        "0x80070005": "доступ к микрофону запрещён — «Параметры → Конфиденциальность → Микрофон»",
    }
    low = text.lower()
    for code, hint in hints.items():
        if code in low:
            return f"{hint} ({text})"
    return text


class PortAudioBackend:
    """Запись через PyAudioWPatch (PortAudio): микрофон через WASAPI/MME/DirectSound,
    звук компьютера — WASAPI loopback. Работает в режиме обратного вызова, чтение никогда не зависает.
    """

    def __init__(self):
        try:
            import pyaudiowpatch as pyaudio
        except Exception as e:
            raise RecorderError(f"PyAudioWPatch не установлен ({e}). Запустите setup.bat") from e
        self.pyaudio = pyaudio
        self.pa = pyaudio.PyAudio()
        self.lock = threading.Lock()

    def _mic_candidates(self):
        pa, p = self.pyaudio, self.pa
        seen = set()
        for api_type, api_name in ((pa.paWASAPI, "WASAPI"), (pa.paMME, "MME"), (pa.paDirectSound, "DirectSound")):
            try:
                api = p.get_host_api_info_by_type(api_type)
                info = p.get_device_info_by_index(api["defaultInputDevice"])
            except Exception:
                continue
            if info["index"] not in seen and info.get("maxInputChannels", 0) > 0:
                seen.add(info["index"])
                yield api_name, info

    def _loopback_candidates(self):
        try:
            yield "WASAPI loopback", self.pa.get_default_wasapi_loopback()
        except Exception:
            pass

    def open(self, name, rate):
        errors = []
        cands = list(self._mic_candidates() if name == "mic" else self._loopback_candidates())
        if not cands:
            raise RecorderError("устройство по умолчанию не найдено" if name == "mic"
                                else "не найден loopback для динамиков по умолчанию")
        for api, info in cands:
            try:
                with self.lock:
                    src = _PortAudioSource(self, info, rate)
                src.config = f"PortAudio {api}: {src.dev_rate} Гц, каналов {src.channels}"
                src.tried = errors
                return src
            except Exception as e:
                errors.append(f"{api}: {describe_error(e)}")
        raise RecorderError(f"{cands[0][1].get('name', name)}: " + " | ".join(errors))

    def close(self):
        try:
            self.pa.terminate()
        except Exception:
            pass


class _PortAudioSource:
    def __init__(self, backend, info, rate):
        pa = backend.pyaudio
        self.device = info.get("name", "")
        self.channels = max(1, min(int(info.get("maxInputChannels", 1)), 2))
        self.dev_rate = int(info.get("defaultSampleRate", 48000))
        self.rate = rate
        self.q = queue.Queue(maxsize=600)

        def callback(in_data, frame_count, time_info, status):
            try:
                self.q.put_nowait(in_data)
            except queue.Full:
                pass
            return (None, pa.paContinue)

        self.stream = backend.pa.open(format=pa.paFloat32, channels=self.channels, rate=self.dev_rate,
                                      input=True, input_device_index=info["index"],
                                      frames_per_buffer=max(256, self.dev_rate // 20),
                                      stream_callback=callback)
        self.stream.start_stream()

    def read(self, frames):
        try:
            data = self.q.get(timeout=0.5)
        except queue.Empty:
            return None          # тишина в loopback: данных нет — заполнит выравнивание по часам
        arr = np.frombuffer(data, dtype=np.float32)
        if self.channels > 1:
            arr = arr.reshape(-1, self.channels).mean(axis=1)
        return resample(arr, self.dev_rate, self.rate)

    def close(self):
        try:
            self.stream.stop_stream()
            self.stream.close()
        except Exception:
            pass


class AutoBackend:
    """Пробует PortAudio, затем soundcard — для каждого источника отдельно."""

    def __init__(self):
        self.backends = []
        self.init_errors = []
        for cls in (PortAudioBackend, SoundcardBackend):
            try:
                self.backends.append(cls())
            except RecorderError as e:
                self.init_errors.append(str(e))
        if not self.backends:
            raise RecorderError("; ".join(self.init_errors))

    def thread_enter(self):
        return SoundcardBackend.thread_enter()

    def thread_exit(self, token):
        SoundcardBackend.thread_exit(token)

    def open(self, name, rate):
        errors = []
        for b in self.backends:
            try:
                return b.open(name, rate)
            except Exception as e:
                errors.append(f"[{type(b).__name__.replace('Backend', '')}] "
                              + (str(e) if isinstance(e, RecorderError) else describe_error(e)))
        raise RecorderError(" ; ".join(errors))

    def close(self):
        for b in self.backends:
            close = getattr(b, "close", None)
            if close:
                close()


def available():
    """(True, '') если запись возможна, иначе (False, причина)."""
    try:
        AutoBackend().close()
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
            self.backend = AutoBackend()
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
        token = None
        enter = getattr(self.backend, "thread_enter", None)
        if enter is not None:
            try:
                token = enter()
            except Exception:
                token = None
        try:
            self._capture_source(name)
        finally:
            leave = getattr(self.backend, "thread_exit", None)
            if leave is not None:
                try:
                    leave(token)
                except Exception:
                    pass

    def _capture_source(self, name):
        track = self.tracks[name]
        try:
            source = self.backend.open(name, self.rate)
        except Exception as e:
            text = str(e) if isinstance(e, RecorderError) else describe_error(e)
            track.error = f"не удалось открыть {SOURCE_LABELS[name]}: {text}"
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
            track.error = f"{SOURCE_LABELS[name]}: запись прервалась ({describe_error(e)})"
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
        close = getattr(self.backend, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass
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


def self_test(seconds=3.0, backend=None):
    """Короткая пробная запись в памяти: что слышно с каждого источника. Для кнопки «Проверить запись»."""
    backend = backend or AutoBackend()
    results = {}

    def run(name):
        token = None
        enter = getattr(backend, "thread_enter", None)
        if enter:
            try:
                token = enter()
            except Exception:
                pass
        info = {"device": "", "error": "", "seconds": 0.0, "peak": 0.0, "rms": 0.0}
        try:
            src = backend.open(name, RATE)
            info["device"] = getattr(src, "device", "")
            info["config"] = getattr(src, "config", "")
            got, sq, peak = 0, 0.0, 0.0
            deadline = time.time() + seconds
            while time.time() < deadline:
                block = src.read(int(RATE * 0.25))
                if block is None or not len(block):
                    continue
                got += len(block)
                sq += float(np.sum(np.square(block, dtype=np.float64)))
                peak = max(peak, float(np.max(np.abs(block))))
            src.close()
            info.update(seconds=got / RATE, peak=peak, rms=(sq / got) ** 0.5 if got else 0.0)
        except Exception as e:
            info["error"] = str(e) if isinstance(e, RecorderError) else describe_error(e)
        finally:
            leave = getattr(backend, "thread_exit", None)
            if leave:
                try:
                    leave(token)
                except Exception:
                    pass
        results[name] = info

    threads = [threading.Thread(target=run, args=(n,), daemon=True) for n in SOURCES]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=seconds + 8)
    close = getattr(backend, "close", None)
    if close is not None:
        try:
            close()
        except Exception:
            pass
    for n in SOURCES:
        results.setdefault(n, {"device": "", "error": "данных нет — во время проверки на компьютере ничего не звучало?",
                               "seconds": 0.0, "peak": 0.0, "rms": 0.0})
    return results
