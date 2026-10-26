# -*- coding: utf-8 -*-
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import report  # noqa: E402
import transcriber as tr  # noqa: E402

SEGS = [{"start": 0, "end": 2, "text": "Привет <всем>", "speaker": "Вы"},
        {"start": 3, "end": 5, "text": "Давайте начнём", "speaker": "Собеседники"}]
RESULT = {"summary": ["Обсудили релиз"], "decisions": [{"text": "Выпускаем в пятницу"}],
          "tasks": [{"task": "Подготовить сборку", "owner": "Иван", "deadline": ""}],
          "open_questions": [{"question": "Кто тестирует?"}]}


class Report(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()).resolve()
        self.wav = self.dir / "20261024_100000.wav"
        self.wav.write_bytes(b"RIFF")
        self.wav.with_suffix(".json").write_text(json.dumps({"meeting": "Планёрка", "duration_sec": 3725}),
                                                 encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def put_transcript(self):
        tr.output_paths(self.wav)[1].write_text(json.dumps({"segments": SEGS}), encoding="utf-8")

    def put_summary(self):
        (self.dir / "20261024_100000.summary.json").write_text(json.dumps(RESULT), encoding="utf-8")

    def test_nothing_to_report(self):
        self.assertIsNone(report.write_protocol(self.wav))

    def test_transcript_only(self):
        self.put_transcript()
        path = report.write_protocol(self.wav)
        text = path.read_text(encoding="utf-8")
        self.assertIn("Планёрка", text)
        self.assertIn("1:02:05", text)
        self.assertIn("ещё не подготовлены", text)
        self.assertIn("Привет &lt;всем&gt;", text)       # экранирование
        self.assertNotIn("<всем>", text)

    def test_full(self):
        self.put_transcript()
        self.put_summary()
        path = report.write_protocol(self.wav)
        text = path.read_text(encoding="utf-8")
        for needle in ("Обсудили релиз", "Выпускаем в пятницу", "Подготовить сборку", "Иван", "Кто тестирует?"):
            self.assertIn(needle, text)
        md = report.protocol_paths(self.wav)[1].read_text(encoding="utf-8")
        self.assertIn("| Подготовить сборку | Иван | — |", md)
        self.assertIn("**Вы:** Привет", md)

    @unittest.skipUnless(report.docx_available(), "нет python-docx")
    def test_word_protocol(self):
        import zipfile
        self.put_transcript()
        self.put_summary()
        path = report.write_docx(self.wav)
        self.assertEqual(path.name, "20261024_100000.protocol.docx")
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8")
        for needle in ("Планёрка", "Выпускаем в пятницу", "Подготовить сборку", "Иван", "Кто тестирует?",
                       "Привет &lt;всем&gt;"):
            self.assertIn(needle, xml)

    def test_word_protocol_nothing(self):
        self.assertIsNone(report.write_docx(self.wav))

    def test_summary_without_transcript(self):
        self.put_summary()
        self.assertIsNotNone(report.write_protocol(self.wav))


if __name__ == "__main__":
    unittest.main()
