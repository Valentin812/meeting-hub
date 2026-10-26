# -*- coding: utf-8 -*-
import os
import shutil
import sys
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import player  # noqa: E402


class FakeOut:
    def __init__(self):
        self.started = 0
        self.pull = None

    def start(self, rate, pull, player=None):
        self.started += 1
        self.pull = pull

    def stop(self):
        self.pull = None

    def close(self):
        pass


def make(path, seconds=2.0, rate=16000):
    n = int(seconds * rate)
    left = np.full(n, 20000, dtype=np.int16)
    right = np.full(n, 20000, dtype=np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(np.stack([left, right], axis=1).tobytes())


class PlayerTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.wav = self.dir / "a.wav"
        make(self.wav)
        self.out = FakeOut()
        self.p = player.Player(lambda: self.out)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_mono_mix_without_clipping(self):
        data, rate = player.load_mono(self.wav)
        self.assertEqual(rate, 16000)
        self.assertEqual(len(data), 32000)
        self.assertLessEqual(int(data.max()), 32767)
        self.assertGreater(int(data.max()), 30000)

    def test_play_pull_seek_end(self):
        self.p.load(self.wav)
        self.assertAlmostEqual(self.p.duration, 2.0)
        self.p.play()
        self.assertTrue(self.p.playing)
        data, more = self.out.pull(1600)
        self.assertEqual(len(data), 3200)
        self.assertTrue(more)
        self.assertAlmostEqual(self.p.position, 0.1)
        self.p.seek(1.95)
        data, more = self.out.pull(1600)
        self.assertEqual(len(data), 3200)              # хвост дополнен тишиной
        self.assertFalse(more)
        self.assertFalse(self.p.playing)
        self.p.play()                                  # с конца — снова с начала
        self.assertAlmostEqual(self.p.position, 0.0)

    def test_pause_toggle_and_bounds(self):
        self.p.load(self.wav)
        self.p.toggle()
        self.assertTrue(self.p.playing)
        self.p.toggle()
        self.assertFalse(self.p.playing)
        self.p.seek(-5)
        self.assertEqual(self.p.position, 0.0)
        self.p.seek(99)
        self.assertAlmostEqual(self.p.position, 2.0)

    def test_resampled_stereo_output(self):
        self.p.load(self.wav)
        data, more = self.p.pull_converted(4800, 48000, 2)       # 0,1 с при 48 кГц стерео
        self.assertEqual(len(data), 4800 * 2 * 2)
        self.assertTrue(more)
        self.assertAlmostEqual(self.p.position, 0.1, places=2)

    def test_falls_back_to_second_backend(self):
        class Broken:
            name = "broken"

            def start(self, *a, **k):
                raise RuntimeError("Kaspersky")

            def close(self):
                pass
        p = player.Player(backends=[Broken, lambda: self.out])
        p.load(self.wav)
        p.play()
        self.assertTrue(p.playing)
        self.assertIs(p.backend, self.out)
        self.assertIn("Kaspersky", p.errors[0])

    def test_all_backends_fail(self):
        class Broken:
            def __init__(self):
                raise RuntimeError("нет динамиков")
        p = player.Player(backends=[Broken])
        p.load(self.wav)
        with self.assertRaises(player.PlayerError):
            p.play()
        self.assertFalse(p.playing)

    def test_nothing_loaded(self):
        with self.assertRaises(player.PlayerError):
            self.p.play()


if __name__ == "__main__":
    unittest.main()
