import os
import sys
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dictation  # noqa: E402


class Src:
    def __init__(self, value):
        self.value = value

    def read(self, frames):
        time.sleep(frames / dictation.RATE)
        return np.full(frames, self.value, dtype=np.float32)

    def close(self):
        pass


class Backend:
    def __init__(self, value=0.3):
        self.value = value

    def open(self, name, rate):
        assert name == "mic"
        return Src(self.value)


class Engine:
    def transcribe(self, samples, language="ru"):
        return [{"text": " привет  "}, {"text": ""}, {"text": "мир."}]


class DictationTests(unittest.TestCase):
    def test_parse_hotkey(self):
        self.assertEqual(dictation.parse_hotkey("Ctrl+Alt+Space"), (0x3, 0x20))
        self.assertEqual(dictation.parse_hotkey("ctrl + shift + d"), (0x6, ord("D")))
        self.assertEqual(dictation.parse_hotkey("Alt+F9"), (0x1, 0x78))
        for bad in ("", "Space", "Ctrl+Alt", "Ctrl+A+B", "Ctrl+Ж"):
            with self.assertRaises(dictation.DictationError):
                dictation.parse_hotkey(bad)

    def test_capture_and_transcribe(self):
        cap = dictation.MicCapture(Backend(0.3)).start()
        time.sleep(0.7)
        samples = cap.stop()
        self.assertGreater(len(samples), 0.4 * dictation.RATE)
        self.assertEqual(dictation.transcribe(Engine(), samples), "привет мир.")

    def test_silence_and_too_short(self):
        cap = dictation.MicCapture(Backend(0.0)).start()
        time.sleep(0.6)
        with self.assertRaises(dictation.DictationError):
            dictation.transcribe(Engine(), cap.stop())
        self.assertEqual(dictation.transcribe(Engine(), np.zeros(100, dtype=np.float32)), "")


if __name__ == "__main__":
    unittest.main()
