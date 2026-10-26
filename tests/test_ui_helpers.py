# -*- coding: utf-8 -*-
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import ui_main  # noqa: E402
except ImportError:          # нет tkinter (например, в минимальном Linux)
    ui_main = None


@unittest.skipIf(ui_main is None, "tkinter недоступен")
class Helpers(unittest.TestCase):
    def test_day_label(self):
        today = date(2026, 10, 26)
        self.assertEqual(ui_main.day_label("20261026_100500", today), "Сегодня")
        self.assertEqual(ui_main.day_label("20261025_100500", today), "Вчера")
        self.assertEqual(ui_main.day_label("20261001_100500", today), "1 октября")
        self.assertEqual(ui_main.day_label("20251231_100500", today), "31 декабря 2025")
        self.assertEqual(ui_main.day_label("запись", today), "Без даты")

    def test_durations_and_titles(self):
        self.assertEqual(ui_main.dur_text(42), "42 с")
        self.assertEqual(ui_main.dur_text(2520), "42 мин")
        self.assertEqual(ui_main.dur_text(3725), "1 ч 2 мин")
        self.assertEqual(ui_main.clock("20261026_152923"), "15:29")
        self.assertEqual(ui_main.meeting_title({"window": "Планёрка | MRM meet", "meeting": "MRM"}),
                         "Планёрка | MRM meet")
        self.assertEqual(ui_main.meeting_title({}), "Запись без названия")


if __name__ == "__main__":
    unittest.main()
