import io
import json
import os
import tempfile
from pathlib import Path
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import crm_sync  # noqa: E402


class FakeCRM:
    def __init__(self):
        self.calls, self.valid, self.logins = [], set(), 0

    def __call__(self, req, timeout=30):
        path = req.full_url.split("example.ru", 1)[1]
        self.calls.append((req.get_method(), path, dict(req.header_items())))
        if path == "/api/auth/login":
            body = json.loads(req.data)
            if body["password"] != "secret":
                raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b'{"error":"bad login"}'))
            self.logins += 1
            tok = f"t{self.logins}"
            self.valid.add(tok)
            return io.BytesIO(json.dumps({"token": tok, "expiresIn": 28800, "operator": {"name": "Иван"}}).encode())
        auth = req.get_header("Authorization", "")
        if auth.replace("Bearer ", "") not in self.valid:
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b'{"error":"expired"}'))
        if path == "/api/products":
            return io.BytesIO(json.dumps([{"id": 1, "name": "Электронная регистрация и аккредитивы"},
                                          {"id": 2, "name": "Ремонты"}]).encode())
        return io.BytesIO(json.dumps({"id": "101", "name": "Иван Примеров", "product_id": 1}).encode())


class CrmTests(unittest.TestCase):
    def test_login_and_me_with_product_header(self):
        crm = FakeCRM()
        c = crm_sync.Client("example.ru", 7, "ivan", "secret", opener=crm)
        self.assertEqual(c.me(), "Иван Примеров")
        method, path, headers = crm.calls[-1]
        self.assertEqual((method, path), ("GET", "/api/me"))
        self.assertEqual(headers.get("X-product-id"), "7")
        self.assertTrue(c.base.startswith("https://"))

    def test_relogin_on_401_and_after_expiry(self):
        crm, now = FakeCRM(), [0.0]
        c = crm_sync.Client("https://example.ru", 7, "ivan", "secret", opener=crm, clock=lambda: now[0])
        c.me()
        crm.valid.clear()                         # сессию отозвали
        self.assertEqual(c.me(), "Иван Примеров")
        self.assertEqual(crm.logins, 2)
        now[0] += 9 * 3600                        # прошло больше 8 часов
        c.me()
        self.assertEqual(crm.logins, 3)

    def test_info_default_project_and_builtin_address(self):
        crm = FakeCRM()
        c = crm_sync.Client("", "", "ivan", "secret", opener=lambda req, timeout=30: crm(
            type("R", (), {**{k: getattr(req, k) for k in ("data", "get_method", "header_items", "get_header")},
                           "full_url": req.full_url.replace(crm_sync.DEFAULT_URL, "https://example.ru")})(), timeout))
        self.assertEqual(c.base, crm_sync.DEFAULT_URL)
        name, default, products = c.info()
        self.assertEqual((name, default), ("Иван Примеров", "1"))
        self.assertEqual(products[1], ("2", "Ремонты"))
        self.assertNotIn("X-product-id", crm.calls[-1][2])                   # без проекта — сервер берёт ваш

    def test_bad_password(self):
        c = crm_sync.Client("https://example.ru", 7, "ivan", "wrong", opener=FakeCRM())
        with self.assertRaises(crm_sync.CrmError):
            c.me()

    def _meeting(self, d):
        wav = Path(d) / "20261029_093000.wav"
        wav.write_bytes(b"")
        wav.with_suffix(".json").write_text(json.dumps({"title": "Планирование работ"}), encoding="utf-8")
        wav.with_suffix(".txt").write_text("[00:00:12] Иван: смета", encoding="utf-8")
        (Path(d) / "20261029_093000.summary.json").write_text(json.dumps({
            "summary": ["Обсудили смету"], "decisions": [{"text": "Подготовить смету"}],
            "tasks": [{"task": "Подготовить смету", "owner": "Анна", "deadline": "05.11"}],
            "open_questions": [{"question": "Кто согласует?"}]}, ensure_ascii=False), encoding="utf-8")
        return wav

    def test_build_protocol(self):
        with tempfile.TemporaryDirectory() as d:
            body = crm_sync.build_protocol(self._meeting(d))
            self.assertEqual(body["source"], "upload")
            self.assertIn("29.10.2026", body["title"])
            self.assertIn("ПОРУЧЕНИЯ\n- Подготовить смету; ответственный: Анна; срок: 05.11", body["protocol"])
            self.assertIn("РЕШЕНИЯ", body["protocol"])
            self.assertEqual(body["transcript"], "")                        # расшифровка — только если разрешено
            self.assertRegex(body["requestKey"], r"^[A-Za-z0-9_-]{12,128}$")
            self.assertIn("смета", crm_sync.build_protocol(self._meeting(d), True)["transcript"])

    def test_push_creates_then_updates_with_version(self):
        server = ProtoServer()
        with tempfile.TemporaryDirectory() as d:
            wav = self._meeting(d)
            c = crm_sync.Client("https://example.ru", 7, "ivan", "secret", opener=server)
            self.assertEqual(crm_sync.push(c, wav), "501")
            self.assertEqual(server.posts, 1)
            server.protocols["501"]["version"] = 5                   # кто-то поправил протокол в CRM
            self.assertEqual(crm_sync.push(c, wav), "501")           # 409 → перечитали версию → обновили
            self.assertEqual(server.posts, 1)
            self.assertEqual(server.protocols["501"]["version"], 6)
            meta = json.loads(wav.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual((meta["crm_protocol_id"], meta["crm_protocol_version"]), ("501", 6))


class ProtoServer(FakeCRM):
    def __init__(self):
        super().__init__()
        self.protocols, self.posts = {}, 0

    def __call__(self, req, timeout=30):
        path = req.full_url.split("example.ru", 1)[1]
        if not path.startswith("/api/protocols"):
            return super().__call__(req, timeout)
        method = req.get_method()
        body = json.loads(req.data) if req.data else {}
        if method == "POST":
            self.posts += 1
            item = {"id": "501", "version": 1, **body}
            self.protocols["501"] = item
            return io.BytesIO(json.dumps({"item": item, "duplicate": False}).encode())
        pid = path.rsplit("/", 1)[1]
        item = self.protocols[pid]
        if method == "GET":
            return io.BytesIO(json.dumps({"item": item}).encode())
        if body.get("version") != item["version"]:
            raise urllib.error.HTTPError(req.full_url, 409, "c", {}, io.BytesIO(b'{"error":"version"}'))
        item.update(body)
        item["version"] += 1
        return io.BytesIO(json.dumps({"item": item}).encode())


if __name__ == "__main__":
    unittest.main()
