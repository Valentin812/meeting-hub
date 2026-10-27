import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai_setup  # noqa: E402


def server(handler):
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


class Tags(BaseHTTPRequestHandler):
    names = []

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"models": [{"name": n} for n in self.names]}).encode())

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.end_headers()
        for part in ({"status": "pulling", "total": 100, "completed": 40},
                     {"status": "pulling", "total": 100, "completed": 100}, {"status": "success"}):
            self.wfile.write((json.dumps(part) + "\n").encode())
        Tags.names = ["qwen2.5:3b"]

    def log_message(self, *a):
        pass


class RecommendTests(unittest.TestCase):
    def test_ladder(self):
        self.assertEqual(ai_setup.recommend(4)[0], "qwen2.5:3b")
        self.assertEqual(ai_setup.recommend(8)[0], "qwen2.5:3b")
        self.assertEqual(ai_setup.recommend(16)[0], "qwen2.5:7b")
        self.assertEqual(ai_setup.recommend(24)[0], "qwen2.5:7b")
        self.assertEqual(ai_setup.recommend(32)[0], "qwen2.5:14b")
        self.assertEqual(ai_setup.recommend(None)[0], "qwen2.5:3b")      # не смогли определить — безопасная

    def test_upgrade_only_from_ladder(self):
        self.assertEqual(ai_setup.upgrade_for("qwen2.5:3b", 24)[0], "qwen2.5:7b")
        self.assertIsNone(ai_setup.upgrade_for("qwen2.5:7b", 24))
        self.assertIsNone(ai_setup.upgrade_for("qwen2.5:7b", 8))          # не понижаем
        self.assertIsNone(ai_setup.upgrade_for("llama3:8b", 24))          # свою модель не трогаем

    def test_ram_detected_here(self):
        r = ai_setup.ram_gb()
        self.assertTrue(r is None or r > 0.5)


class StatusTests(unittest.TestCase):
    def test_has_model(self):
        self.assertTrue(ai_setup.has_model(["qwen2.5:3b"], "qwen2.5:3b"))
        self.assertTrue(ai_setup.has_model(["llama3:latest"], "llama3"))
        self.assertFalse(ai_setup.has_model(["llama3:8b"], "qwen2.5:3b"))

    def test_status_values(self):
        Tags.names = []
        srv, base = server(Tags)
        try:
            self.assertEqual(ai_setup.status("qwen2.5:3b", base), "no_model")
            Tags.names = ["qwen2.5:3b"]
            self.assertEqual(ai_setup.status("qwen2.5:3b", base), "ok")
        finally:
            srv.shutdown()
        self.assertEqual(ai_setup.status("m", "http://127.0.0.1:9", exe=""), "not_installed")
        self.assertEqual(ai_setup.status("m", "http://127.0.0.1:9", exe="C:/ollama.exe"), "not_running")

    def test_pull_reports_progress(self):
        Tags.names = []
        srv, base = server(Tags)
        seen = []
        try:
            ai_setup.pull("qwen2.5:3b", lambda d, t, s: seen.append((d, t)), base=base)
        finally:
            srv.shutdown()
        self.assertIn((40, 100), seen)
        self.assertEqual(Tags.names, ["qwen2.5:3b"])


class FakeResponse(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class DownloadTests(unittest.TestCase):
    def test_download_ok_and_progress(self):
        with tempfile.TemporaryDirectory() as d:
            seen = []
            path = ai_setup.download_installer(d, lambda a, b: seen.append((a, b)),
                                               opener=lambda u: FakeResponse(b"x" * 3_000_000))
            self.assertEqual(path.stat().st_size, 3_000_000)
            self.assertEqual(seen[-1], (3_000_000, 3_000_000))
            self.assertFalse(list(Path(d).glob("*.part")))

    def test_tiny_file_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ai_setup.SetupError):
                ai_setup.download_installer(d, opener=lambda u: FakeResponse(b"<html>blocked</html>"))
            self.assertFalse(list(Path(d).iterdir()))

    def test_cancel(self):
        ev = threading.Event()
        ev.set()
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ai_setup.SetupError):
                ai_setup.download_installer(d, cancel=ev, opener=lambda u: FakeResponse(b"x" * 10))


if __name__ == "__main__":
    unittest.main()
