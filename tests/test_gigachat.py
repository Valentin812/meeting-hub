import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gigachat  # noqa: E402


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
        req = json.loads(body)
        Giga.seen.append((self.headers.get("x-hub-token"), req))
        if self.headers.get("x-hub-token") != "CODE":
            return self._json(401, {"error": "неверный код доступа"})
        if Giga.mode.startswith("busy"):                      # busy2: два отказа 429, потом успех
            left = int(Giga.mode[4:] or 99)
            if left > 0:
                Giga.mode = "busy" + str(left - 1) if left < 99 else Giga.mode
                return self._json(429, {"message": "Too Many Requests"})
            Giga.mode = "ok"
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

    def do_GET(self):
        if self.headers.get("x-hub-token") != "CODE":
            return self._json(401, {"error": "неверный код доступа"})
        self._json(200, {"ok": True, "user": "Тест"})

    def log_message(self, *a):
        pass


def start():
    srv = HTTPServer(("127.0.0.1", 0), Giga)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    return srv, gigachat.ProxyClient("CODE", url=base + "/giga-proxy", timeout=10)


class ClientTests(unittest.TestCase):
    def setUp(self):
        Giga.mode, Giga.seen = "ok", []
        self.srv, self.client = start()

    def tearDown(self):
        self.srv.shutdown()

    def test_complete_and_stream(self):
        text = self.client.complete([{"role": "user", "content": "hi"}], "GigaChat-2-Pro")
        self.assertIn("summary", text)
        self.assertEqual(Giga.seen[0][0], "CODE")
        self.assertEqual(Giga.seen[0][1]["model"], "GigaChat-2-Pro")
        got = []
        full = self.client.stream([{"role": "user", "content": "q"}], "GigaChat-2", got.append)
        self.assertEqual(full, "Договорились на пятницу.")
        self.assertEqual(got, ["Договорились ", "на пятницу."])

    def test_check_and_wrong_code(self):
        self.assertIn("код принят", self.client.check())
        bad = gigachat.ProxyClient("WRONG", url=self.client.url)
        with self.assertRaises(gigachat.GigaError) as cm:
            bad.check()
        self.assertIn("401", str(cm.exception))
        with self.assertRaises(gigachat.GigaError):
            gigachat.ProxyClient("").check()

    def test_http_error(self):
        Giga.mode = "fail"
        with self.assertRaises(gigachat.GigaError):
            self.client.complete([{"role": "user", "content": "hi"}])


class QueueTests(unittest.TestCase):
    def setUp(self):
        Giga.mode, Giga.seen = "ok", []
        self.srv, self.client = start()
        self.client.retry_delays = (0.01, 0.01, 0.01)

    def tearDown(self):
        self.srv.shutdown()

    def test_retries_when_busy(self):
        Giga.mode = "busy2"
        text = self.client.complete([{"role": "user", "content": "hi"}])
        self.assertIn("summary", text)
        self.assertEqual(len(Giga.seen), 3)                     # два отказа и успех

    def test_gives_up_after_retries(self):
        Giga.mode = "busy"
        with self.assertRaises(gigachat.GigaError):
            self.client.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(len(Giga.seen), 4)                     # 1 + 3 повтора

    def test_other_errors_not_retried(self):
        Giga.mode = "fail"
        with self.assertRaises(gigachat.GigaError):
            self.client.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(len(Giga.seen), 1)

    def test_requests_are_serialized(self):
        active, peak = [0], [0]
        orig = self.client._complete

        def slow(*a):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            time.sleep(0.1)
            try:
                return orig(*a)
            finally:
                active[0] -= 1
        self.client._complete = slow
        threads = [threading.Thread(target=lambda: self.client.complete([{"role": "user", "content": "q"}]))
                   for _ in range(3)]
        [x.start() for x in threads]
        [x.join() for x in threads]
        self.assertEqual(peak[0], 1)


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
