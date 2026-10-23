# -*- coding: utf-8 -*-
"""Расшифровка: каналы, склейка, очередь с паузой и отменой (на фейковом движке)."""
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

import transcriber as tr  # noqa: E402


def make_wav(path, mine, theirs, rate=16000):
    n = max(len(mine), len(theirs))
    data = np.zeros((n, 2), dtype=np.int16)
    data[:len(mine), 0] = (mine * 32767).astype(np.int16)
    data[:len(theirs), 1] = (theirs * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(data.tobytes())


class Fake:
    name = "fake"
    size = "x"

    def __init__(self):
        self.calls = []

    def transcribe(self, samples, language, should_stop=None):
        self.calls.append(len(samples))
        if should_stop:
            should_stop()
        return [{"start": 0.0, "end": 1.0, "text": f"реплика {len(self.calls)}"}]


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()).resolve()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class Pure(Base):
    def test_merge_orders_and_glues(self):
        mine = [{"start": 0, "end": 1, "text": "а"}, {"start": 1.5, "end": 2, "text": "б"}]
        theirs = [{"start": 0.5, "end": 1.2, "text": "в"}]
        m = tr.merge(mine, theirs)
        self.assertEqual([s["speaker"] for s in m], ["Вы", "Собеседники", "Вы"])
        m = tr.merge([{"start": 0, "end": 1, "text": "а"}, {"start": 1.2, "end": 2, "text": "б"}], [])
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0]["text"], "а б")

    def test_format(self):
        txt = tr.format_text([{"start": 3725, "end": 3730, "text": "привет", "speaker": "Вы"}], "T")
        self.assertIn("[01:02:05] Вы: привет", txt)

    def test_silence(self):
        self.assertTrue(tr.is_silent(np.zeros(100, dtype=np.float32)))
        self.assertFalse(tr.is_silent(np.full(100, 0.3, dtype=np.float32)))

    def test_read_channels_and_resample(self):
        w = self.dir / "a.wav"
        make_wav(w, np.full(8000, 0.5, dtype=np.float32), np.zeros(8000, dtype=np.float32), rate=8000)
        mine, theirs = tr.read_channels(w)
        self.assertEqual(len(mine), 16000)
        self.assertGreater(float(mine.max()), 0.4)
        self.assertTrue(tr.is_silent(theirs))


class File(Base):
    def test_skips_silent_channel_and_writes_outputs(self):
        w = self.dir / "20261023_100000.wav"
        make_wav(w, np.full(16000, 0.3, dtype=np.float32), np.zeros(16000, dtype=np.float32))
        eng = Fake()
        txt = tr.transcribe_file(w, eng)
        self.assertEqual(len(eng.calls), 1)
        self.assertTrue(tr.has_transcript(w))
        self.assertIn("Вы: реплика 1", txt.read_text(encoding="utf-8"))
        meta = json.loads(tr.output_paths(w)[1].read_text(encoding="utf-8"))
        self.assertEqual(meta["engine"], "fake")


class Queue(Base):
    def wav(self, name="a.wav"):
        w = self.dir / name
        make_wav(w, np.full(16000, 0.3, dtype=np.float32), np.full(16000, 0.3, dtype=np.float32))
        return w

    def wait(self, q, key, states, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if q.status[str(key)]["state"] in states:
                return
            time.sleep(0.05)
        self.fail(f"{q.status[str(key)]} not in {states}")

    def test_runs_and_notifies(self):
        done = []
        q = tr.TranscriptionQueue(Fake, on_done=lambda k, s: done.append(s["state"]))
        w = self.wav()
        self.assertTrue(q.add(w))
        self.wait(q, w, ("done",))
        time.sleep(0.1)
        self.assertEqual(done, ["done"])
        self.assertTrue(tr.has_transcript(w))
        q.close()

    def test_pause_blocks_start_until_resume(self):
        q = tr.TranscriptionQueue(Fake)
        q.set_paused(True)
        w = self.wav()
        q.add(w)
        time.sleep(1.2)
        self.assertEqual(q.status[str(w)]["state"], "paused")
        self.assertFalse(tr.has_transcript(w))
        q.set_paused(False)
        self.wait(q, w, ("done",))
        q.close()

    def test_cancel_current(self):
        q = tr.TranscriptionQueue(Fake)
        q.set_paused(True)
        w = self.wav()
        q.add(w)
        time.sleep(0.8)
        q.cancel(w)
        self.wait(q, w, ("cancelled",))
        self.assertFalse(tr.has_transcript(w))
        q.close()

    def test_engine_error_is_reported(self):
        def bad():
            raise tr.TranscriptionError("нет модели")
        q = tr.TranscriptionQueue(bad)
        w = self.wav()
        q.add(w)
        self.wait(q, w, ("error",))
        self.assertEqual(q.status[str(w)]["error"], "нет модели")
        # после ошибки можно поставить снова
        self.assertTrue(q.add(w))
        q.close()

    def test_no_duplicates(self):
        q = tr.TranscriptionQueue(Fake)
        q.set_paused(True)
        w = self.wav()
        self.assertTrue(q.add(w))
        self.assertFalse(q.add(w))
        q.close()


if __name__ == "__main__":
    unittest.main()
