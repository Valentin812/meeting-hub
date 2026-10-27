import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import chat  # noqa: E402


class ContextTests(unittest.TestCase):
    def test_short_transcript_kept_whole(self):
        ctx = chat.build_context("Планёрка", "", "Итоги: релиз в пятницу", "Вы: привет\nИван: релиз в пятницу", "когда релиз")
        self.assertIn("Итоги: релиз в пятницу", ctx)
        self.assertIn("Иван: релиз в пятницу", ctx)

    def test_long_transcript_picks_relevant_lines(self):
        lines = [f"Вы: пустая реплика номер {i}" for i in range(2000)]
        lines[1500] = "Иван: бюджет на рекламу составит триста тысяч"
        ctx = chat.build_context("", "", "", "\n".join(lines), "какой бюджет на рекламу", limit=2000)
        self.assertLessEqual(len(ctx), 2200)
        self.assertIn("бюджет на рекламу", ctx)

    def test_messages_trim_history(self):
        hist = [{"role": "user", "content": f"q{i}"} for i in range(20)]
        msgs = chat.build_messages("ctx", hist, "вопрос")
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(len(msgs), 1 + chat.MAX_HISTORY + 1)
        self.assertEqual(msgs[-1]["content"], "вопрос")


if __name__ == "__main__":
    unittest.main()
