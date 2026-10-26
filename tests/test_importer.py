import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import importer  # noqa: E402
import report  # noqa: E402
import transcriber as tr  # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def make_wav(path, seconds=3, rate=44100, channels=2):
    t = np.arange(int(rate * seconds)) / rate
    tone = (np.sin(2 * np.pi * 440 * t) * 8000).astype("<i2")
    data = np.repeat(tone[:, None], channels, axis=1) if channels > 1 else tone
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(data.tobytes())


class FakeEngine:
    name, size = "fake", "x"

    def transcribe(self, samples, language, check):
        check(0.0)
        return [{"start": 0.0, "end": 1.0, "text": "привет"}]


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.out = self.tmp / "rec"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @unittest.skipUnless(HAVE_FFMPEG, "нужен ffmpeg")
    def test_wav_is_converted_to_mono_16k(self):
        src = self.tmp / "Планерка_по_проекту.wav"
        make_wav(src)
        wav = importer.import_file(src, self.out, description="обсуждали бюджет")
        with wave.open(str(wav), "rb") as w:
            self.assertEqual((w.getnchannels(), w.getframerate()), (1, 16000))
            self.assertAlmostEqual(w.getnframes() / 16000, 3.0, delta=0.2)
        meta = json.loads(wav.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(meta["title"], "Планерка по проекту")
        self.assertTrue(meta["imported"])
        self.assertEqual(meta["description"], "обсуждали бюджет")
        self.assertTrue(src.exists())           # исходник не тронут
        self.assertFalse(list(self.out.glob("*.part.wav")))

    @unittest.skipUnless(HAVE_FFMPEG, "нужен ffmpeg")
    def test_video_audio_track(self):
        src = self.tmp / "meeting.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x120:d=3",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-shortest",
                        "-pix_fmt", "yuv420p", str(src)], check=True)
        wav = importer.import_file(src, self.out, title="Видео-встреча")
        with wave.open(str(wav), "rb") as w:
            self.assertEqual(w.getframerate(), 16000)
            self.assertGreater(w.getnframes(), 16000 * 2)
        self.assertEqual(json.loads(wav.with_suffix(".json").read_text(encoding="utf-8"))["title"], "Видео-встреча")

    @unittest.skipUnless(HAVE_FFMPEG, "нужен ffmpeg")
    def test_video_without_sound(self):
        src = self.tmp / "mute.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x120:d=2",
                        "-pix_fmt", "yuv420p", str(src)], check=True)
        with self.assertRaises(importer.ImportFailed):
            importer.import_file(src, self.out)
        self.assertFalse(list(self.out.glob("*.wav")))

    def test_not_media(self):
        src = self.tmp / "text.mp3"
        src.write_text("это не звук", encoding="utf-8")
        with self.assertRaises(Exception):
            importer.import_file(src, self.out)
        self.assertFalse(list(self.out.glob("*.wav")))

    def test_same_second_files_do_not_clash(self):
        a = self.tmp / "a.wav"
        make_wav(a, rate=16000, channels=1)
        w1 = importer.import_file(a, self.out)
        w2 = importer.import_file(a, self.out)
        self.assertNotEqual(w1, w2)

    def test_write_meta_keeps_other_fields_and_is_used_by_protocol(self):
        a = self.tmp / "a.wav"
        make_wav(a, rate=16000, channels=1)
        wav = importer.import_file(a, self.out)
        meta = importer.write_meta(wav, {"title": "Новое имя", "description": "О планах"})
        self.assertTrue(meta["imported"])
        html = report.build_html(wav, meta, None, [])
        self.assertIn("Новое имя", html)
        self.assertIn("О планах", html)

    def test_imported_speakers_are_neutral(self):
        a = self.tmp / "a.wav"
        make_wav(a, rate=16000, channels=1)
        wav = importer.import_file(a, self.out)
        tr.transcribe_file(wav, FakeEngine())
        seg = json.loads(tr.output_paths(wav)[1].read_text(encoding="utf-8"))["segments"]
        self.assertEqual(seg[0]["speaker"], importer.SPEAKER)

    def test_recorded_mono_keeps_you_label(self):
        wav = self.out / "20261026_100000.wav"
        self.out.mkdir(parents=True)
        make_wav(wav, rate=16000, channels=1)
        tr.transcribe_file(wav, FakeEngine())
        seg = json.loads(tr.output_paths(wav)[1].read_text(encoding="utf-8"))["segments"]
        self.assertEqual(seg[0]["speaker"], tr.SPEAKERS[0])


if __name__ == "__main__":
    unittest.main()
