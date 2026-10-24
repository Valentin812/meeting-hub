# -*- coding: utf-8 -*-
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analyzer  # noqa: E402


def lines(n):
    return "\n".join(f"[00:{i // 60:02d}:{i % 60:02d}] Вы: реплика номер {i} " + "слово " * 20 for i in range(n))


class Split(unittest.TestCase):
    def test_short_is_single(self):
        self.assertEqual(len(analyzer.split_transcript("[00:00:01] Вы: привет")), 1)

    def test_long_is_split_within_limit_and_covers_everything(self):
        text = lines(200)
        chunks = analyzer.split_transcript(text, limit=3000)
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(len(c) <= 3000 for c in chunks))
        joined = "\n".join(chunks)
        for i in range(200):
            self.assertIn(f"реплика номер {i} ", joined)

    def test_chunks_overlap(self):
        chunks = analyzer.split_transcript(lines(60), limit=2000, overlap=2)
        last = chunks[0].splitlines()[-1]
        self.assertIn(last, chunks[1])

    def test_giant_line(self):
        chunks = analyzer.split_transcript("x" * 10000, limit=3000)
        self.assertEqual(sum(len(c) for c in chunks), 10000)


class Merge(unittest.TestCase):
    def test_dedupes_and_keeps_order(self):
        a = {"summary": ["Обсудили бюджет"], "decisions": [{"text": "Утвердить бюджет"}],
             "tasks": [{"task": "Отправить смету", "owner": "Иван"}]}
        b = {"summary": ["обсудили бюджет!"], "decisions": [{"text": "утвердить  бюджет."}, {"text": "Нанять"}],
             "tasks": [{"task": "Отправить смету", "owner": ""}], "open_questions": [{"question": "Когда?"}]}
        m = analyzer.merge_results([a, b])
        self.assertEqual(m["summary"], ["Обсудили бюджет"])
        self.assertEqual([d["text"] for d in m["decisions"]], ["Утвердить бюджет", "Нанять"])
        self.assertEqual(len(m["tasks"]), 1)
        self.assertEqual(m["tasks"][0]["owner"], "Иван")
        self.assertEqual(len(m["open_questions"]), 1)

    def test_ignores_junk_items(self):
        m = analyzer.merge_results([{"decisions": ["строка", None, {"text": "ок"}]}])
        self.assertEqual(len(m["decisions"]), 1)


class Analyze(unittest.TestCase):
    def test_short_single_call(self):
        calls = []

        def llm(system, user):
            calls.append(user)
            return {"summary": ["итог"], "decisions": []}
        r = analyzer.analyze_transcript("[00:00:01] Вы: привет", llm)
        self.assertEqual(len(calls), 1)
        self.assertEqual(r["summary"], ["итог"])

    def test_long_runs_per_chunk_then_merges_summary(self):
        seen = []

        def llm(system, user):
            seen.append(system[:20])
            if system == analyzer.MERGE_PROMPT:
                return {"summary": ["общий итог"]}
            return {"summary": ["часть"], "tasks": [{"task": f"задача {len(seen)}"}]}
        r = analyzer.analyze_transcript(lines(300), llm)
        self.assertGreater(len(seen), 3)
        self.assertEqual(r["summary"], ["общий итог"])
        self.assertEqual(len(r["tasks"]), len(seen) - 1)

    def test_merge_failure_keeps_part_summaries(self):
        def llm(system, user):
            if system == analyzer.MERGE_PROMPT:
                raise RuntimeError("нет памяти")
            return {"summary": [f"часть {len(user)}"]}
        r = analyzer.analyze_transcript(lines(300), llm)
        self.assertTrue(r["summary"])

    def test_cancel(self):
        ev = threading.Event()

        def llm(system, user):
            ev.set()
            return {"summary": ["x"]}
        with self.assertRaises(InterruptedError):
            analyzer.analyze_transcript(lines(300), llm, ev)

    def test_parse_json_with_noise(self):
        self.assertEqual(analyzer.parse_json('Вот: {"a": 1} конец'), {"a": 1})
        with self.assertRaises(ValueError):
            analyzer.parse_json("совсем не json")


if __name__ == "__main__":
    unittest.main()
