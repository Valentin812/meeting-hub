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

    def test_combo_from_key_press(self):
        self.assertEqual(dictation.combo_from_event(0x4 | 0x20000, "space"), "Ctrl+Alt+Space")
        self.assertEqual(dictation.combo_from_event(0x4 | 0x1, "d"), "Ctrl+Shift+D")
        self.assertEqual(dictation.combo_from_event(0x20000, "F9"), "Alt+F9")
        self.assertIsNone(dictation.combo_from_event(0x4, "Control_L"))        # ждём основную клавишу
        with self.assertRaises(dictation.DictationError):
            dictation.combo_from_event(0, "space")                              # без модификатора нельзя
        with self.assertRaises(dictation.DictationError):
            dictation.combo_from_event(0x4, "Cyrillic_ve")
        self.assertEqual(dictation.combo_from_event(0x4 | 0x1, "Cyrillic_ve", True, 0x44), "Ctrl+Shift+D")  # русская раскладка
        self.assertIsNone(dictation.combo_from_event(0x4, "??", True, 0x11))
        self.assertTrue(dictation.is_free("Ctrl+Alt+Space"))                   # не Windows — свободно

    def test_ai_modes_and_guard(self):
        raw = "ну вот значит завтра созвон в десять"
        self.assertEqual(dictation.ai_messages("clean", raw)[1]["content"], raw)
        self.assertIn("слова-паразиты", dictation.ai_messages("clean", raw)[0]["content"])
        self.assertEqual(dictation.accept_ai(raw, "«Завтра созвон в 10:00.»", "clean"), "Завтра созвон в 10:00.")
        self.assertEqual(dictation.accept_ai(raw, "", "clean"), raw)
        self.assertEqual(dictation.accept_ai(raw, "Конечно! Вот исправленный текст: " * 5, "clean"), raw)
        self.assertEqual(dictation.accept_ai(raw, "Завтра.", "punct"), raw)          # слова пропали — не берём
        self.assertNotIn("raw", dictation.PROMPTS)

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
