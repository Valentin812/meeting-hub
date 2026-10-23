# -*- coding: utf-8 -*-
"""Запись встречи на поддельных источниках звука (без реальных устройств)."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import recorder as rec_mod  # noqa: E402
from recorder import AudioRecorder, merge_tracks, recover_unfinished, list_recordings, RecorderError  # noqa: E402

RATE = 8000


class ToneSource:
    """Отдаёт блоки в реальном времени с постоянным уровнем."""

    def __init__(self, value, device="fake"):
        self.value = value
        self.device = device
        self.closed = False

    def read(self, frames):
        time.sleep(frames / RATE)
        return np.full(frames, self.value, dtype=np.float32)

    def close(self):
        self.closed = True


class SilentLoopback:
    """Как loopback Windows без воспроизведения: данных нет, чтение висит."""

    def __init__(self):
        self.device = "loopback"
        self.release = threading.Event()

    def read(self, frames):
        self.release.wait(10)
        return None

    def close(self):
        pass


class Backend:
    def __init__(self, sources, fail=()):
        self.sources = sources
        self.fail = set(fail)

    def open(self, name, rate):
        if name in self.fail:
            raise OSError("устройство не найдено")
        return self.sources[name]


def read_stereo(path):
    with wave.open(str(path), "rb") as r:
        assert r.getnchannels() == 2 and r.getsampwidth() == 2
        data = np.frombuffer(r.readframes(r.getnframes()), dtype="<i2").reshape(-1, 2)
        return data, r.getframerate()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class Realtime(Base):
    def test_both_sources_recorded_to_stereo(self):
        rec = AudioRecorder(self.dir, Backend({"mic": ToneSource(0.5), "system": ToneSource(-0.25)}),
                            rate=RATE, block_sec=0.1)
        rec.start({"meeting": "Google Meet"})
        time.sleep(1.2)
        health = rec.health()
        self.assertTrue(health["mic"]["started"] and health["system"]["started"])
        self.assertGreater(health["mic"]["level"], 0)
        result = rec.stop()
        data, rate = read_stereo(result["path"])
        self.assertEqual(rate, RATE)
        self.assertAlmostEqual(len(data) / RATE, result["duration"], delta=0.01)
        self.assertGreater(result["duration"], 1.0)
        mid = data[len(data) // 4: len(data) * 3 // 4]
        self.assertTrue(np.all(mid[:, 0] > 15000))      # левый — микрофон
        self.assertTrue(np.all(mid[:, 1] < -7000))      # правый — системный звук
        self.assertEqual(list(self.dir.glob("*.part.wav")), [])
        meta = json.loads(result["path"].with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "saved")
        self.assertEqual(meta["meeting"], "Google Meet")

    def test_silent_loopback_is_padded_and_stop_is_prompt(self):
        loop = SilentLoopback()
        rec = AudioRecorder(self.dir, Backend({"mic": ToneSource(0.3), "system": loop}), rate=RATE, block_sec=0.1)
        rec.start()
        time.sleep(1.0)
        started = time.time()
        result = rec.stop()
        self.assertLess(time.time() - started, 5)
        loop.release.set()
        data, _ = read_stereo(result["path"])
        self.assertGreater(len(data), RATE * 0.9)
        self.assertTrue(np.all(data[:, 1] == 0))
        self.assertGreater(np.abs(data[:, 0]).mean(), 5000)

    def test_one_device_missing_still_records_other(self):
        rec = AudioRecorder(self.dir, Backend({"mic": ToneSource(0.3), "system": None}, fail={"system"}),
                            rate=RATE, block_sec=0.1)
        rec.start()
        time.sleep(0.6)
        self.assertIn("звук компьютера", rec.health()["system"]["error"])
        result = rec.stop()
        self.assertTrue(result["errors"])
        data, _ = read_stereo(result["path"])
        self.assertGreater(np.abs(data[:, 0]).mean(), 5000)

    def test_cut_at_trims_tail_after_meeting(self):
        rec = AudioRecorder(self.dir, Backend({"mic": ToneSource(0.3), "system": ToneSource(0.1)}),
                            rate=RATE, block_sec=0.1)
        rec.start()
        time.sleep(1.5)
        result = rec.stop(cut_at=rec.t0 + 0.5)
        self.assertAlmostEqual(result["duration"], 0.5, delta=0.01)
        self.assertGreater(result["trimmed"], 0.8)

    def test_double_start_refused(self):
        rec = AudioRecorder(self.dir, Backend({"mic": ToneSource(0.1), "system": ToneSource(0.1)}),
                            rate=RATE, block_sec=0.1)
        rec.start()
        with self.assertRaises(RecorderError):
            rec.start()
        rec.stop()
        self.assertIsNone(rec.stop())


class GapFilling(Base):
    def test_gap_is_filled_with_silence_by_clock(self):
        clock = [100.0]
        rec = AudioRecorder(self.dir, Backend({}), clock=lambda: clock[0], rate=RATE, block_sec=0.1)
        rec.t0 = 100.0
        rec.tracks = {n: rec_mod._Track(self.dir / f"x.{n}.part.wav", RATE) for n in rec_mod.SOURCES}
        block = np.full(800, 0.5, dtype=np.float32)
        rec.write_block("system", 100.1, block)           # 0.0–0.1 с
        rec.write_block("system", 102.1, block)           # пропуск 1.9 с без данных
        self.assertEqual(rec.tracks["system"].frames, int(2.1 * RATE))
        rec.write_block("system", 102.15, block)          # небольшое опоздание — без вставки тишины
        self.assertEqual(rec.tracks["system"].frames, int(2.2 * RATE))
        for t in rec.tracks.values():
            t.close()


class MergeAndRecovery(Base):
    def _mono(self, path, frames, value):
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
            w.writeframes(np.full(frames, value, dtype="<i2").tobytes())

    def test_merge_pads_shorter_track(self):
        self._mono(self.dir / "a.wav", RATE * 3, 1000)
        self._mono(self.dir / "b.wav", RATE, -1000)
        frames = merge_tracks(self.dir / "a.wav", self.dir / "b.wav", self.dir / "out.wav", None, RATE)
        self.assertEqual(frames, RATE * 3)
        data, _ = read_stereo(self.dir / "out.wav")
        self.assertTrue(np.all(data[:, 0] == 1000))
        self.assertTrue(np.all(data[:RATE, 1] == -1000))
        self.assertTrue(np.all(data[RATE:, 1] == 0))

    def test_recover_after_crash(self):
        stem = "20261023_101500"
        self._mono(self.dir / f"{stem}.mic.part.wav", RATE * 2, 500)
        self._mono(self.dir / f"{stem}.system.part.wav", RATE * 2, 700)
        (self.dir / f"{stem}.json").write_text(json.dumps({"meeting": "Телемост", "status": "recording"}),
                                               encoding="utf-8")
        recovered = recover_unfinished(self.dir, RATE)
        self.assertEqual([p.name for p in recovered], [f"{stem}.wav"])
        self.assertEqual(list(self.dir.glob("*.part.wav")), [])
        items = list_recordings(self.dir)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "recovered")
        self.assertEqual(items[0]["meeting"], "Телемост")
        self.assertAlmostEqual(items[0]["duration_sec"], 2.0, delta=0.01)

    def test_recover_with_only_one_track(self):
        stem = "20261023_101600"
        self._mono(self.dir / f"{stem}.mic.part.wav", RATE, 500)
        self.assertEqual(len(recover_unfinished(self.dir, RATE)), 1)


class Guards(Base):
    def test_low_disk_space_refused(self):
        old = rec_mod.MIN_FREE_BYTES
        rec_mod.MIN_FREE_BYTES = 10 ** 18
        try:
            rec = AudioRecorder(self.dir, Backend({}), rate=RATE)
            with self.assertRaises(RecorderError):
                rec.start()
            self.assertEqual(list(self.dir.iterdir()), [])
        finally:
            rec_mod.MIN_FREE_BYTES = old


class Helpers(Base):
    def test_downsample_by_three(self):
        x = np.array([0, 3, 6, 9, 12, 15, 1], dtype=np.float32)
        self.assertEqual(rec_mod.downsample(x, 3).tolist(), [3.0, 12.0])
        self.assertIs(rec_mod.downsample(x, 1), x)

    def test_describe_error_hints(self):
        text = rec_mod.describe_error(RuntimeError("Error 0x800401f0"))
        self.assertIn("COM", text)
        self.assertIn("устройство не найдено", rec_mod.describe_error(RuntimeError("Error 0x80070490")))

    def test_thread_hooks_are_called_in_each_capture_thread(self):
        calls = []

        class HookBackend(Backend):
            def thread_enter(self):
                calls.append(("enter", threading.current_thread().name))
                return "token"

            def thread_exit(self, token):
                calls.append(("exit", token))

        rec = AudioRecorder(self.dir, HookBackend({"mic": ToneSource(0.1), "system": ToneSource(0.1)}),
                            rate=RATE, block_sec=0.1)
        rec.start()
        time.sleep(0.3)
        rec.stop()
        self.assertEqual(sorted(c[1] for c in calls if c[0] == "enter"), ["rec-mic", "rec-system"])
        self.assertEqual([c[1] for c in calls if c[0] == "exit"], ["token", "token"])

    def test_self_test_reports_levels_and_errors(self):
        backend = Backend({"mic": ToneSource(0.5), "system": None}, fail={"system"})
        old = rec_mod.RATE
        rec_mod.RATE = RATE
        try:
            res = rec_mod.self_test(0.6, backend)
        finally:
            rec_mod.RATE = old
        self.assertGreater(res["mic"]["peak"], 0.4)
        self.assertGreater(res["mic"]["seconds"], 0.3)
        self.assertIn("не найдено", res["system"]["error"])


class FormatFallback(Base):
    def test_resample_non_integer_ratio(self):
        x = np.sin(np.linspace(0, 20, 4410)).astype(np.float32)
        y = rec_mod.resample(x, 44100, 16000)
        self.assertEqual(len(y), 1600)
        self.assertEqual(y.dtype, np.float32)

    def test_backend_tries_next_format_when_windows_rejects(self):
        attempts = []

        class FakeRec:
            def __init__(self, rate, channels):
                self.rate, self.channels = rate, channels

            def __enter__(self):
                attempts.append((self.rate, self.channels))
                if self.channels == 1:
                    raise RuntimeError("invalid argument")
                return self

            def __exit__(self, *a):
                pass

            def record(self, numframes):
                return np.full((numframes, self.channels), 0.5, dtype=np.float32)

        class FakeDev:
            name = "Микрофон (тест)"

            def recorder(self, samplerate, channels, blocksize=None):
                return FakeRec(samplerate, channels)

        backend = rec_mod.SoundcardBackend.__new__(rec_mod.SoundcardBackend)
        backend.device = lambda name: FakeDev()
        src = backend.open("mic", 16000)
        self.assertEqual(attempts[:2], [(48000, 1), (48000, 2)])
        self.assertIn("48000", src.config)
        self.assertEqual(len(src.tried), 1)
        block = src.read(4000)
        self.assertEqual(len(block), 4000)
        self.assertAlmostEqual(float(block.mean()), 0.5, places=4)

    def test_backend_reports_all_failures(self):
        class BadDev:
            name = "Сломанный"

            def recorder(self, samplerate, channels, blocksize=None):
                raise RuntimeError("invalid argument")

        backend = rec_mod.SoundcardBackend.__new__(rec_mod.SoundcardBackend)
        backend.device = lambda name: BadDev()
        with self.assertRaises(RecorderError) as ctx:
            backend.open("mic", 16000)
        # одна причина для всех форматов — показываем её один раз
        self.assertEqual(str(ctx.exception), "Сломанный: RuntimeError: invalid argument")

    def test_backend_lists_different_failures(self):
        class PickyDev:
            name = "Капризный"

            def recorder(self, samplerate, channels, blocksize=None):
                raise RuntimeError(f"bad {samplerate}")

        backend = rec_mod.SoundcardBackend.__new__(rec_mod.SoundcardBackend)
        backend.device = lambda name: PickyDev()
        with self.assertRaises(RecorderError) as ctx:
            backend.open("mic", 16000)
        self.assertIn("ни один формат", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
