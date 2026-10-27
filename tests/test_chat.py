import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

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


class FakeOllama(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen = body
        self.send_response(200)
        self.end_headers()
        for piece in ("Договорились ", "на пятницу."):
            self.wfile.write((json.dumps({"message": {"content": piece}, "done": False}) + "\n").encode())
        self.wfile.write((json.dumps({"message": {"content": ""}, "done": True}) + "\n").encode())

    def log_message(self, *a):
        pass


class StreamTests(unittest.TestCase):
    def test_stream_answer(self):
        srv = HTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            got = []
            text = chat.stream_answer("m", chat.build_messages("ctx", [], "когда?"), got.append,
                                      url=f"http://127.0.0.1:{srv.server_port}/api/chat")
        finally:
            srv.shutdown()
        self.assertEqual(text, "Договорились на пятницу.")
        self.assertEqual(got, ["Договорились ", "на пятницу."])
        self.assertTrue(srv.seen["stream"])
        self.assertNotIn("format", srv.seen)                     # обычный текст, не JSON

    def test_error_from_model(self):
        class Bad(FakeOllama):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"error": "model not found"}\n')
        srv = HTTPServer(("127.0.0.1", 0), Bad)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(RuntimeError):
                chat.stream_answer("m", [], lambda t: None, url=f"http://127.0.0.1:{srv.server_port}/x")
        finally:
            srv.shutdown()


class SummaryTests(unittest.TestCase):
    def test_summary_text(self):
        r = {"summary": ["Релиз в пятницу"], "tasks": [{"task": "Подготовить инструкцию", "owner": "Ольга"}],
             "decisions": [{"text": "Выпускаем"}]}
        t = chat.summary_text(r)
        self.assertIn("Релиз в пятницу", t)
        self.assertIn("Подготовить инструкцию (Ольга)", t)
        self.assertIn("Выпускаем", t)

    def test_all_meetings_prefers_relevant(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for name, text in (("2026-10-01_10-00", "закупка серверов"), ("2026-10-02_10-00", "дизайн логотипа")):
                (d / f"{name}.summary.json").write_text(json.dumps({"summary": [text]}), encoding="utf-8")
            ctx = chat.all_meetings_context(d, "что с логотипом", limit=120)
            self.assertIn("логотипа", ctx)
            self.assertNotIn("серверов", ctx)


if __name__ == "__main__":
    unittest.main()
