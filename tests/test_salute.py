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
    uploads = []
    max_upload = 10 ** 9

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
            if len(body) > Fake.max_upload:
                return self._send(413, {"message": "file too large"})
            Fake.uploads.append(len(body))
            return self._send(200, {"status": 200, "result": {"request_file_id": "F1"}})
        if self.path == "/v1/speech:async_recognize":
            Fake.opts = json.loads(body)
            return self._send(200, {"status": 200, "result": {"id": "TASK", "status": "NEW"}})
        self._send(404, {})

    def do_GET(self):
        Fake.calls.append((self.path, self.headers.get("Authorization", "")[:10], 0))
        if self.path.startswith("/proxy"):
            if self.headers.get("x-hub-token") != "GOODCODE12345":
                return self._send(401, {"error": "неверный код доступа"})
            return self._send(200, {"ok": True, "user": "t", "access_token": "PT", "expires_at": 9999999999999})
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
        Fake.mode = "ok"; Fake.calls = []; Fake.uploads = []; Fake.max_upload = 10 ** 9

    def proxy_client(self, code="GOODCODE12345", **kw):
        base = f"http://127.0.0.1:{self.port}"
        src = salute.ProxyTokenSource(code, base + "/proxy", timeout=3)
        return salute.SaluteClient("", api_url=base + "/v1", poll_sec=0.01, timeout=3, token_source=src, **kw)

    def test_proxy_token_flow_and_bad_code(self):
        seg = self.proxy_client().transcribe_pcm(b"\0\0" * 16000, "ru")
        self.assertEqual(seg[0]["text"], "Привет, мир.")
        self.assertFalse(any(c[0] == "/oauth" for c in Fake.calls))      # ключ Сбера программе не нужен
        self.assertTrue(any(c[1] == "Bearer PT" for c in Fake.calls))
        with self.assertRaises(salute.SaluteError) as cm:
            self.proxy_client("WRONGCODE").check()
        self.assertIn("401", str(cm.exception))

    def test_split_pcm_cuts_in_quiet_place(self):
        rate = salute.RATE
        loud = (np.random.RandomState(1).randn(rate * 100) * 3000).astype("<i2")
        loud[rate * 55:rate * 56] = 0                                    # тишина в последней минуте куска
        parts = salute.split_pcm(loud.tobytes(), 60)
        self.assertEqual(parts[0][0], 0); self.assertEqual(parts[-1][1], len(loud))
        self.assertTrue(all(b - a <= 60 * rate for a, b in parts))
        self.assertTrue(all(parts[i][1] == parts[i + 1][0] for i in range(len(parts) - 1)))
        self.assertTrue(rate * 55 <= parts[0][1] <= rate * 56)
        self.assertEqual(salute.split_pcm(b"\0\0" * 100, 60), [(0, 100)])

    def test_long_audio_is_chunked_with_offsets(self):
        pcm = b"\0\0" * (16000 * 25)
        seg = self.proxy_client(chunk_sec=10).transcribe_pcm(pcm, "ru")
        self.assertGreaterEqual(len(Fake.uploads), 3)
        starts = [s["start"] for s in seg if s["text"] == "Привет, мир."]
        self.assertEqual(len(starts), len(Fake.uploads))
        self.assertAlmostEqual(starts[0], 0.5)
        self.assertEqual(starts, sorted(starts)); self.assertGreater(starts[-1], 10.0)
        self.assertEqual(seg, sorted(seg, key=lambda s: s["start"]))

    def test_too_big_chunk_is_halved(self):
        Fake.max_upload = 16000 * 2 * 400                                # сервер принимает до 400 с
        seg = self.proxy_client(chunk_sec=1000).transcribe_pcm(b"\0\0" * (16000 * 900), "ru")
        self.assertTrue(Fake.uploads and max(Fake.uploads) <= Fake.max_upload)
        self.assertGreaterEqual(len([s for s in seg if s["text"] == "Привет, мир."]), 3)

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
