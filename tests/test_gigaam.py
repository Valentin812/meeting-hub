import io
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gigaam  # noqa: E402
import transcriber as tr  # noqa: E402

R = 16000


def tone(sec, amp=0.2):
    t = np.arange(int(sec * R)) / R
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def silence(sec):
    return np.zeros(int(sec * R), dtype=np.float32)


class FakeStream:
    def __init__(self, rec):
        self.rec, self.n = rec, 0
        self.result = type("R", (), {"text": ""})()

    def accept_waveform(self, rate, x):
        self.n = len(x)


class FakeRecognizer:
    def __init__(self):
        self.calls = 0

    def create_stream(self):
        return FakeStream(self)

    def decode_stream(self, s):
        self.calls += 1
        s.result.text = "привет мир" if s.n > R // 2 else ""


class Primary:
    name, size, model = "gigaam", "gigaam", None

    def __init__(self, fail=None):
        self.fail, self.loaded = fail, 0

    def model_present(self): return True
    def downloaded_mb(self): return 0
    def load(self):
        self.loaded += 1
        if self.fail == "load":
            raise gigaam.GigaAMError("нет сети")

    def transcribe(self, samples, language="ru", should_stop=None):
        if self.fail == "cancel":
            raise tr.Cancelled()
        if self.fail == "run":
            raise RuntimeError("упала")
        return [{"start": 0, "end": 1, "text": "гига"}]


class Whisper:
    name, size, model = "local", "small", object()

    def __init__(self): self.used = 0
    def model_present(self): return True
    def downloaded_mb(self): return 0
    def load(self): pass

    def transcribe(self, samples, language="ru", should_stop=None):
        self.used += 1
        return [{"start": 0, "end": 1, "text": "виспер"}]


class SplitTests(unittest.TestCase):
    def test_split_by_pauses_and_long_cut(self):
        x = np.concatenate([silence(1), tone(3), silence(1.2), tone(2), silence(1)])
        parts = gigaam.split_speech(x)
        self.assertEqual(len(parts), 2)
        self.assertAlmostEqual(parts[0][0] / R, 0.85, delta=0.1)
        self.assertGreater(parts[1][0], parts[0][1])
        long = np.concatenate([tone(15), silence(0.2), tone(30)])        # без настоящей паузы
        pieces = gigaam.split_speech(long)
        self.assertTrue(all((b - a) / R <= gigaam.MAX_PIECE_SEC + 0.01 for a, b in pieces))
        self.assertEqual(gigaam.split_speech(silence(5)), [])
        self.assertEqual(gigaam.split_speech(np.zeros(10, dtype=np.float32)), [])

    def test_polish(self):
        self.assertEqual(gigaam.polish("привет  мир"), "Привет мир.")
        self.assertEqual(gigaam.polish("Уже готово?"), "Уже готово?")
        self.assertEqual(gigaam.polish("  "), "")


class EngineTests(unittest.TestCase):
    def engine(self):
        import tempfile
        e = gigaam.GigaAM(tempfile.mkdtemp())
        e.model = FakeRecognizer()
        return e

    def test_transcribe_segments_and_progress(self):
        e, seen = self.engine(), []
        x = np.concatenate([silence(1), tone(2), silence(1), tone(2)])
        seg = e.transcribe(x, "ru", seen.append)
        self.assertEqual([s["text"] for s in seg], ["Привет мир."] * 2)
        self.assertLess(seg[0]["end"], seg[1]["start"])
        self.assertEqual(len(seen), 2)
        self.assertEqual(e.model.calls, 2)

    def test_download_files(self):
        import tempfile
        asked = []

        class Resp(io.BytesIO):
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def opener(url, timeout=0):
            asked.append(url.rsplit("/", 1)[1])
            return Resp(b"\0" * gigaam.MIN_BYTES[asked[-1]])

        base = Path(tempfile.mkdtemp())
        self.assertFalse(gigaam.model_ready(base))
        marks = []
        gigaam.download_model(base, marks.append, opener=opener)
        self.assertEqual(asked, list(gigaam.FILES))
        self.assertTrue(gigaam.model_ready(base))
        self.assertEqual(sorted(p.name for p in gigaam.model_dir(base).parent.iterdir()), [gigaam.MODEL_NAME])
        self.assertEqual(sorted(p.name for p in gigaam.model_dir(base).iterdir()), sorted(gigaam.FILES))
        with self.assertRaises(gigaam.GigaAMError):                      # обрезанный файл не принимаем
            gigaam.download_model(Path(tempfile.mkdtemp()), opener=lambda url, timeout=0: Resp(b"junk"))
        def broken(url, timeout=0):
            raise OSError("нет сети")
        b2 = Path(tempfile.mkdtemp())
        with self.assertRaises(gigaam.GigaAMError):
            gigaam.download_model(b2, opener=broken)
        self.assertFalse(gigaam.model_ready(b2))


class FallbackTests(unittest.TestCase):
    def make(self, fail=None):
        notes, now = [], [1000.0]
        p, w = Primary(fail), Whisper()
        return gigaam.GigaAMWithFallback(p, w, notify=notes.append, clock=lambda: now[0]), p, w, notes, now

    def test_primary_used(self):
        e, p, w, notes, _ = self.make()
        e.load()
        self.assertEqual(e.transcribe(np.zeros(10, dtype=np.float32))[0]["text"], "гига")
        self.assertEqual((w.used, notes, e.name, e.size), (0, [], "gigaam", "gigaam"))

    def test_load_failure_falls_back_to_whisper(self):
        e, p, w, notes, now = self.make("load")
        e.load()
        self.assertEqual(len(notes), 1)
        self.assertEqual(e.transcribe(np.zeros(10, dtype=np.float32))[0]["text"], "виспер")
        self.assertEqual(e.size, "small")
        now[0] += gigaam.COOLDOWN_SEC + 1                    # пауза прошла — снова пробуем GigaAM
        self.assertEqual(e.size, "gigaam")

    def test_run_failure_cooldown(self):
        e, p, w, notes, now = self.make("run")
        self.assertEqual(e.transcribe(np.zeros(10, dtype=np.float32))[0]["text"], "виспер")
        self.assertIn("упала", e.fallback_reason)
        e.transcribe(np.zeros(10, dtype=np.float32))
        self.assertEqual((len(notes), w.used), (1, 2))

    def test_cancel_not_swallowed_and_language(self):
        e, p, w, notes, _ = self.make("cancel")
        with self.assertRaises(tr.Cancelled):
            e.transcribe(np.zeros(10, dtype=np.float32))
        self.assertEqual(notes, [])
        e2, p2, w2, _, _ = self.make()
        self.assertEqual(e2.transcribe(np.zeros(10, dtype=np.float32), "en")[0]["text"], "виспер")


if __name__ == "__main__":
    unittest.main()
