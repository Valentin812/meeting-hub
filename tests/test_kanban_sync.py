import json
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kanban_sync as ks  # noqa: E402

TOKEN = "mh_" + "A" * 43


class Fake(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        Fake.seen.append((self.path, dict(self.headers), body))
        if self.path.startswith("/auth/v1/token"):
            if body.get("password") != "pw":
                return self._send(400, {"error_description": "Invalid login credentials"})
            return self._send(200, {"access_token": "JWT"})
        if self.path == "/functions/v1/meeting-intake":
            if body["action"] == "issue_token":
                if self.headers.get("Authorization") != "Bearer JWT":
                    return self._send(401, {"error": "Sign in again"})
                return self._send(200, {"token": TOKEN, "email": "a@b.c"})
            if self.headers.get("x-meeting-token") != TOKEN:
                return self._send(401, {"error": "Invalid token"})
            if body["action"] == "check":
                return self._send(200, {"ok": True, "email": "a@b.c"})
            if body["action"] == "push":
                return self._send(200, {"ok": True, "id": "uuid-1"})
        self._send(404, {})


class KanbanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Fake)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def setUp(self):
        Fake.seen = []
        self.tmp = Path(tempfile.mkdtemp())
        self.wav = self.tmp / "20261028_152923.wav"
        self.wav.write_bytes(b"x")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_connect_check_and_bad_password(self):
        token, email = ks.connect("a@b.c", "pw", base_url=self.url)
        self.assertEqual((token, email), (TOKEN, "a@b.c"))
        self.assertEqual(ks.check(TOKEN, base_url=self.url), "a@b.c")
        with self.assertRaises(ks.KanbanError) as cm:
            ks.connect("a@b.c", "wrong", base_url=self.url)
        self.assertIn("Invalid login", str(cm.exception))
        with self.assertRaises(ks.KanbanError):
            ks.check("mh_bad", base_url=self.url)
        with self.assertRaises(ks.KanbanError):
            ks.connect("", "", base_url=self.url)
        with self.assertRaises(ks.KanbanError):
            ks.check("", base_url=self.url)
        self.assertNotIn("pw", json.dumps([s[1] for s in Fake.seen]))     # пароль не уходит в заголовках функции

    def test_payload_and_push(self):
        self.wav.with_suffix(".json").write_text(json.dumps(
            {"title": "Планёрка · 28.10.2026", "meeting": "Zoom", "duration_sec": 600}), encoding="utf-8")
        self.wav.with_name(self.wav.stem + ".summary.json").write_text(json.dumps(
            {"summary": ["Итог"], "tasks": [{"task": "Сделать", "owner": "Иван"}], "decisions": []}),
            encoding="utf-8")
        self.wav.with_suffix(".txt").write_text("[00:00:03] Вы: привет", encoding="utf-8")
        p = ks.build_payload(self.wav)
        self.assertEqual((p["external_id"], p["title"], p["meeting_app"]), ("20261028_152923", "Планёрка · 28.10.2026", "Zoom"))
        self.assertTrue(p["held_at"].startswith("2026-10-28T15:29"))
        self.assertEqual(p["tasks"][0]["owner"], "Иван")
        self.assertEqual(ks.push(TOKEN, self.wav, base_url=self.url), "uuid-1")
        sent = [s for s in Fake.seen if s[2].get("action") == "push"][0]
        self.assertEqual(sent[2]["meeting"]["transcript"], "[00:00:03] Вы: привет")

    def test_nothing_to_send_and_bad_token(self):
        with self.assertRaises(ks.KanbanError):
            ks.push(TOKEN, self.wav, base_url=self.url)                   # нет ни расшифровки, ни итогов
        self.wav.with_suffix(".txt").write_text("текст", encoding="utf-8")
        with self.assertRaises(ks.KanbanError):
            ks.push("mh_wrong", self.wav, base_url=self.url)
        with self.assertRaises(ks.KanbanError):
            ks.push(TOKEN, self.wav, base_url="http://127.0.0.1:1")      # нет связи


if __name__ == "__main__":
    unittest.main()
