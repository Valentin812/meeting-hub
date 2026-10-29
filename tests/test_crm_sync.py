import io
import json
import os
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
        return io.BytesIO(json.dumps({"id": "101", "name": "Иван Примеров"}).encode())


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

    def test_bad_password(self):
        c = crm_sync.Client("https://example.ru", 7, "ivan", "wrong", opener=FakeCRM())
        with self.assertRaises(crm_sync.CrmError):
            c.me()

    def test_push_waits_for_protocol_api(self):
        self.assertFalse(crm_sync.ready())
        with self.assertRaises(crm_sync.CrmError):
            crm_sync.push(None, "x.wav")


if __name__ == "__main__":
    unittest.main()
