import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import importer  # noqa: E402
import titles  # noqa: E402

TALK = ("Нужно обсудить сроки релиза. Сроки поджимают, релиз нельзя переносить. Бюджет на релиз утверждён, "
        "бюджет не меняем. Сроки по интеграции уточнит Иван, интеграция идёт по плану. ") * 3


class TitleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.wav = self.tmp / "20261028_152923.wav"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def meta(self):
        return json.loads(self.wav.with_suffix(".json").read_text(encoding="utf-8"))

    def put(self, **fields):
        self.wav.with_suffix(".json").write_text(json.dumps(fields, ensure_ascii=False), encoding="utf-8")

    def transcript(self, text):
        segs = [{"start": 0, "end": 1, "text": text}]
        self.wav.with_name(self.wav.stem + ".transcript.json").write_text(json.dumps({"segments": segs}),
                                                                           encoding="utf-8")

    def test_with_date(self):
        self.assertEqual(titles.with_date("Планёрка", "20261028_152923"), "Планёрка · 28.10.2026")
        self.assertEqual(titles.with_date("Планёрка · 01.01.2025", "20261028_1"), "Планёрка · 28.10.2026")
        self.assertEqual(titles.with_date("Итоги 28.10.2026", "20261028_1"), "Итоги 28.10.2026")
        self.assertEqual(titles.with_date("Планёрка", "без даты"), "Планёрка")

    def test_from_window(self):
        f = titles.from_window
        self.assertEqual(f("Планёрка по релизу | Microsoft Teams"), "Планёрка по релизу")
        self.assertEqual(f("Room | MRM meet"), "Room")
        self.assertEqual(f("Квартальный план - Google Chrome"), "Квартальный план")
        for generic in ("Zoom Meeting", "Meet - abc-defg-hij", "Microsoft Teams", "", "Телемост", "Zoom"):
            self.assertEqual(f(generic, "Zoom"), "", generic)

    def test_from_words(self):
        t = titles.from_words(TALK)
        self.assertTrue(t.startswith("Обсуждение: "))
        self.assertIn("релиз", t)
        self.assertEqual(titles.from_words("Привет, как дела"), "")

    def test_window_name_then_ai_replaces_then_user_wins(self):
        self.put(window="Планёрка по релизу | Microsoft Teams", meeting="Microsoft Teams")
        self.assertEqual(titles.apply_initial_name(self.wav), "Планёрка по релизу · 28.10.2026")
        m = self.meta()
        self.assertEqual((m["title_auto"], m["title_source"]), (True, "window"))
        importer.auto_name(self.wav, {"title": "Релиз в ноябре", "description": "Сроки."})
        self.assertEqual(self.meta()["title"], "Релиз в ноябре · 28.10.2026")
        self.assertEqual(titles.apply_initial_name(self.wav), "")              # ИИ-название не затирается
        importer.write_meta(self.wav, {"title": "Моё", "title_by_user": True})
        importer.auto_name(self.wav, {"title": "Другое"})
        self.assertEqual(self.meta()["title"], "Моё")

    def test_words_when_window_is_generic(self):
        self.put(window="Zoom Meeting", meeting="Zoom")
        self.transcript(TALK)
        title = titles.apply_initial_name(self.wav)
        self.assertTrue(title.startswith("Обсуждение: ") and title.endswith("· 28.10.2026"), title)
        self.assertEqual(self.meta()["title_source"], "words")

    def test_nothing_to_go_on_and_imported(self):
        self.put(window="Zoom Meeting", meeting="Zoom")
        self.assertEqual(titles.apply_initial_name(self.wav), "")
        self.assertNotIn("title", self.meta())
        self.put(imported=True, title="Файл.mp3")
        self.transcript(TALK)
        self.assertEqual(titles.apply_initial_name(self.wav), "")


if __name__ == "__main__":
    unittest.main()
