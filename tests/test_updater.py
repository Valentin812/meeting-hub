# -*- coding: utf-8 -*-
import hashlib
import io
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import updater  # noqa: E402

DATA = b"MZ" + os.urandom(300_000)


class Resp(io.BytesIO):
    headers = {}


def opener(_url):
    return Resp(DATA)


class Download(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def asset(self, **kw):
        a = {"name": "MeetingHubSetup-v9.0.0.exe", "url": "u", "size": len(DATA),
             "digest": "sha256:" + hashlib.sha256(DATA).hexdigest()}
        a.update(kw)
        return a

    def test_ok_with_progress(self):
        seen = []
        path = updater.download(self.asset(), self.dir, progress=lambda d, t: seen.append((d, t)), opener=opener,
                                chunk=65536)
        self.assertEqual(path.read_bytes(), DATA)
        self.assertEqual(seen[-1], (len(DATA), len(DATA)))
        self.assertGreater(len(seen), 3)
        self.assertFalse(list(self.dir.glob("*.part")))

    def test_bad_digest_and_size(self):
        with self.assertRaises(updater.UpdateError):
            updater.download(self.asset(digest="sha256:" + "0" * 64), self.dir, opener=opener)
        with self.assertRaises(updater.UpdateError):
            updater.download(self.asset(size=len(DATA) + 5), self.dir, opener=opener)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_cancel(self):
        ev = threading.Event()
        ev.set()
        with self.assertRaises(updater.UpdateError):
            updater.download(self.asset(), self.dir, cancel=ev, opener=opener)

    def test_network_error(self):
        def broken(_url):
            raise OSError("нет сети")
        with self.assertRaises(updater.UpdateError):
            updater.download(self.asset(), self.dir, opener=broken)

    def test_asset_and_mode(self):
        rel = {"assets": [{"name": "MeetingHub-v1-portable.zip"},
                          {"name": "MeetingHubSetup-v1.exe", "browser_download_url": "x", "size": 5}]}
        self.assertEqual(updater.installer_asset(rel)["url"], "x")
        self.assertIsNone(updater.installer_asset({"assets": []}))
        self.assertFalse(updater.can_self_update(self.dir, frozen=False))
        (self.dir / "unins000.exe").write_bytes(b"")
        self.assertEqual(updater.can_self_update(self.dir, frozen=True), os.name == "nt")
        (self.dir / "portable.txt").write_text("1")
        self.assertFalse(updater.can_self_update(self.dir, frozen=True))


if __name__ == "__main__":
    unittest.main()
