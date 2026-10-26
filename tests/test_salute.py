import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import salute  # noqa: E402

RESULT = [{"results": [{"text": "привет мир", "normalized_text": "Привет, мир.", "start": "0.500s", "end": "1.800s"}],
           "eou": True, "channel": 0, "processed_audio_start": "0s", "processed_audio_end": "2s"},
          {"results": [{"text": "пока", "start": "3s", "end": "3.5s"}], "channel": 0}]


class Fake(BaseHTTPRequestHandler):
    mode = "ok"
    calls = []

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        Fake.calls.append((self.path, self.headers.get("Authorization", "")[:10], len(body)))
        if self.path == "/oauth":
            if Fake.mode == "badkey":
                return self._send(401, {"message": "bad"})
            return self._send(200, {"access_token": "T", "expires_at": 9999999999999})
        if self.path == "/v1/data:upload":
            return self._send(200, {"status": 200, "result": {"request_file_id": "F1"}})
        if self.path == "/v1/speech:async_recognize":
            Fake.opts = json.loads(body)
            return self._send(200, {"status": 200, "result": {"id": "TASK", "status": "NEW"}})
        self._send(404, {})

    def do_GET(self):
        Fake.calls.append((self.path, self.headers.get("Authorization", "")[:10], 0))
        if self.path.startswith("/v1/task:get"):
            if Fake.mode == "taskerr":
                return self._send(200, {"result": {"status": "ERROR", "error": "boom"}})
            return self._send(200, {"result": {"status": "DONE", "response_file_id": "R1"}})
        if self.path.startswith("/v1/data:download"):
            return self._send(200, RESULT)
        self._send(404, {})


class LocalStub:
    size = "tiny"
    model = object()

    def __init__(self):
        self.used = 0

    def load(self): pass
    def model_present(self): return True
    def downloaded_mb(self): return 0

    def transcribe(self, samples, language="ru", should_stop=None):
        self.used += 1
        return [{"start": 0.0, "end": 1.0, "text": "локально"}]


class SaluteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Fake)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def client(self, key="KEY", port=None):
        base = f"http://127.0.0.1:{port or self.port}"
        return salute.SaluteClient(key, oauth_url=base + "/oauth", api_url=base + "/v1", poll_sec=0.01, timeout=3)

    def setUp(self):
        Fake.mode = "ok"; Fake.calls = []

    def test_parse_result(self):
        seg = salute.parse_result(RESULT)
        self.assertEqual(seg[0], {"start": 0.5, "end": 1.8, "text": "Привет, мир."})
        self.assertEqual(seg[1]["text"], "пока")
        self.assertEqual(salute.parse_result([]), [])
        self.assertEqual(salute.parse_result("мусор"), [])

    def test_full_flow(self):
        seg = self.client().transcribe_pcm(b"\0\0" * 16000, "ru")
        self.assertEqual(seg[0]["text"], "Привет, мир.")
        self.assertEqual(Fake.opts["options"]["language"], "ru-RU")
        self.assertEqual(Fake.opts["options"]["audio_encoding"], "PCM_S16LE")
        self.assertEqual(Fake.opts["request_file_id"], "F1")
        self.assertTrue(any(c[0] == "/v1/data:upload" and c[2] == 32000 for c in Fake.calls))

    def test_check_and_bad_key(self):
        self.assertIn("работает", self.client().check())
        Fake.mode = "badkey"
        with self.assertRaises(salute.SaluteError):
            self.client().check()
        with self.assertRaises(salute.SaluteError):
            self.client("").check()

    def test_fallback_on_bad_key_and_cooldown(self):
        Fake.mode = "badkey"
        local, notes, now = LocalStub(), [], [1000.0]
        eng = salute.SaluteWithFallback(self.client(), local, notify=notes.append, clock=lambda: now[0])
        samples = np.zeros(16000, dtype=np.float32)
        self.assertEqual(eng.transcribe(samples)[0]["text"], "локально")
        self.assertEqual(len(notes), 1)
        self.assertIn("401", eng.fallback_reason)
        calls = len(Fake.calls)
        eng.transcribe(samples)                     # пауза: облако не дёргаем
        self.assertEqual(len(Fake.calls), calls)
        self.assertEqual(local.used, 2)
        Fake.mode = "ok"; now[0] += salute.COOLDOWN_SEC + 1
        self.assertEqual(eng.transcribe(samples)[0]["text"], "Привет, мир.")
        self.assertEqual(eng.name, "salutespeech")

    def test_fallback_on_task_error_and_no_server(self):
        Fake.mode = "taskerr"
        eng = salute.SaluteWithFallback(self.client(), LocalStub(), notify=lambda r: None)
        self.assertEqual(eng.transcribe(np.zeros(16000, dtype=np.float32))[0]["text"], "локально")
        self.assertIn("boom", eng.fallback_reason)
        eng2 = salute.SaluteWithFallback(self.client(port=1), LocalStub(), notify=lambda r: None)
        self.assertEqual(eng2.transcribe(np.zeros(16000, dtype=np.float32))[0]["text"], "локально")
        self.assertIn("нет связи", eng2.fallback_reason)

    def test_cancel_is_not_swallowed(self):
        class Stop(Exception):
            pass

        def stop(_p=None):
            raise Stop()
        eng = salute.SaluteWithFallback(self.client(), LocalStub(), notify=lambda r: None)
        with self.assertRaises(Stop):
            eng.transcribe(np.zeros(16000, dtype=np.float32), "ru", stop)


if __name__ == "__main__":
    unittest.main()
