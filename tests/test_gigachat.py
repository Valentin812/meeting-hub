import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gigachat  # noqa: E402
from salute import SaluteError  # noqa: E402


class Giga(BaseHTTPRequestHandler):
    mode = "ok"
    seen = []

    def _json(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode())

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path.endswith("/oauth"):
            return self._json(200, {"access_token": "T", "expires_at": 9999999999999})
        req = json.loads(body)
        Giga.seen.append((self.headers.get("Authorization"), req))
        if Giga.mode == "fail":
            return self._json(402, {"message": "Payment Required"})
        if req.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for piece in ("Договорились ", "на пятницу."):
                self.wfile.write(("data: " + json.dumps({"choices": [{"delta": {"content": piece}}]}) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        answer = json.dumps({"summary": ["Итог"], "decisions": [], "tasks": []}, ensure_ascii=False)
        self._json(200, {"choices": [{"message": {"content": "Вот JSON:\n" + answer}}]})

    def log_message(self, *a):
        pass


def start():
    srv = HTTPServer(("127.0.0.1", 0), Giga)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    return srv, gigachat.GigaClient("KEY", oauth_url=base + "/oauth", api_url=base + "/api/v1", timeout=10)


class ClientTests(unittest.TestCase):
    def setUp(self):
        Giga.mode, Giga.seen = "ok", []
        self.srv, self.client = start()

    def tearDown(self):
        self.srv.shutdown()

    def test_complete_and_stream(self):
        text = self.client.complete([{"role": "user", "content": "hi"}], "GigaChat-2-Pro")
        self.assertIn("summary", text)
        self.assertEqual(Giga.seen[0][0], "Bearer T")
        self.assertEqual(Giga.seen[0][1]["model"], "GigaChat-2-Pro")
        got = []
        full = self.client.stream([{"role": "user", "content": "q"}], "GigaChat-2", got.append)
        self.assertEqual(full, "Договорились на пятницу.")
        self.assertEqual(got, ["Договорились ", "на пятницу."])

    def test_http_error(self):
        Giga.mode = "fail"
        with self.assertRaises(SaluteError):
            self.client.complete([{"role": "user", "content": "hi"}])


class RouterTests(unittest.TestCase):
    def setUp(self):
        Giga.mode, Giga.seen = "ok", []
        self.srv, self.client = start()

    def tearDown(self):
        self.srv.shutdown()

    def test_cloud_json(self):
        r = gigachat.LlmRouter("qwen", self.client)
        data = r.chat_json("sys", "user")
        self.assertEqual(data["summary"], ["Итог"])
        self.assertEqual(r.name, "gigachat")

    def test_fallback_to_local_with_cooldown(self):
        Giga.mode = "fail"
        notes, calls = [], []
        t = [1000.0]
        r = gigachat.LlmRouter("qwen", self.client, notify=notes.append, clock=lambda: t[0])
        orig = gigachat.analyzer.ollama_chat
        gigachat.analyzer.ollama_chat = lambda model, s, u, c=None: calls.append(model) or {"summary": ["local"]}
        try:
            self.assertEqual(r.chat_json("s", "u")["summary"], ["local"])
            self.assertEqual(len(notes), 1)
            n = len(Giga.seen)
            r.chat_json("s", "u")                              # пауза: в облако не ходим
            self.assertEqual(len(Giga.seen), n)
            t[0] += gigachat.COOLDOWN_SEC + 1
            Giga.mode = "ok"
            self.assertEqual(r.chat_json("s", "u")["summary"], ["Итог"])
        finally:
            gigachat.analyzer.ollama_chat = orig
        self.assertEqual(calls, ["qwen", "qwen"])

    def test_no_client_goes_local(self):
        calls = []
        orig = gigachat.analyzer.ollama_chat
        gigachat.analyzer.ollama_chat = lambda model, s, u, c=None: calls.append(model) or {"summary": []}
        try:
            r = gigachat.LlmRouter("qwen", None)
            r.chat_json("s", "u")
        finally:
            gigachat.analyzer.ollama_chat = orig
        self.assertEqual(calls, ["qwen"])
        self.assertEqual(r.name, "ollama")

    def test_stream_through_router(self):
        r = gigachat.LlmRouter("qwen", self.client)
        got = []
        self.assertEqual(r.stream([{"role": "user", "content": "q"}], got.append), "Договорились на пятницу.")


if __name__ == "__main__":
    unittest.main()
