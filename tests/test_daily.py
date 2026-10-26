# -*- coding: utf-8 -*-
"""Хранение аудио, проверка обновлений, поиск по встречам, список записей без аудио."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import daily  # noqa: E402
import recorder  # noqa: E402

DAY = 86400


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()).resolve()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def rec(self, stem, age_days, transcript=True, meta=None, text="", summary=None):
        wav = self.dir / f"{stem}.wav"
        wav.write_bytes(b"RIFF" + b"\0" * 1000)
        t = time.time() - age_days * DAY
        os.utime(wav, (t, t))
        wav.with_suffix(".json").write_text(json.dumps(meta or {"meeting": "Встреча", "duration_sec": 60}),
                                            encoding="utf-8")
        if transcript:
            wav.with_suffix(".txt").write_text(text or "[00:00:00] Вы: привет", encoding="utf-8")
            (self.dir / f"{stem}.transcript.json").write_text(json.dumps({"segments": []}), encoding="utf-8")
        if summary is not None:
            (self.dir / f"{stem}.summary.json").write_text(json.dumps(summary), encoding="utf-8")
        return wav


class Cleanup(Base):
    def test_deletes_only_old_audio_with_transcript(self):
        old = self.rec("20260901_100000", 40)
        old_no_text = self.rec("20260902_100000", 40, transcript=False)
        fresh = self.rec("20261020_100000", 2)
        removed, freed = daily.audio_cleanup(self.dir, 30)
        self.assertEqual(removed, [old])
        self.assertGreater(freed, 1000)
        self.assertFalse(old.exists())
        self.assertTrue(old_no_text.exists() and fresh.exists())
        self.assertTrue(old.with_suffix(".txt").exists())                       # текст остаётся
        self.assertTrue(json.loads(old.with_suffix(".json").read_text(encoding="utf-8"))["audio_deleted"])

    def test_keep_forever_and_protect(self):
        old = self.rec("20260901_100000", 400)
        self.assertEqual(daily.audio_cleanup(self.dir, 0), ([], 0))
        self.assertEqual(daily.audio_cleanup(self.dir, 30, protect=[old])[0], [])
        self.assertTrue(old.exists())

    def test_ignores_foreign_files(self):
        other = self.dir / "моя песня.wav"
        other.write_bytes(b"RIFF")
        t = time.time() - 100 * DAY
        os.utime(other, (t, t))
        daily.audio_cleanup(self.dir, 30)
        self.assertTrue(other.exists())

    def test_list_keeps_meeting_after_audio_removed(self):
        self.rec("20260901_100000", 40)
        self.rec("20261020_100000", 1)
        daily.audio_cleanup(self.dir, 30)
        items = recorder.list_recordings(self.dir)
        self.assertEqual([i["path"].stem for i in items], ["20261020_100000", "20260901_100000"])
        gone = items[1]
        self.assertTrue(gone["audio_deleted"])
        self.assertEqual(gone["size"], 0)
        self.assertEqual(gone["duration_sec"], 60)


class Updates(unittest.TestCase):
    REL = [{"tag_name": "v6.0.1", "assets": [], "html_url": "u1"},
           {"tag_name": "v6.2.0", "html_url": "u2",
            "assets": [{"name": "MeetingHub-v6.2.0-portable.zip", "browser_download_url": "zip"},
                       {"name": "MeetingHubSetup-v6.2.0.exe", "browser_download_url": "setup"}]},
           {"tag_name": "v7.0.0", "draft": True}]

    def test_versions(self):
        self.assertGreater(daily.parse_version("6.10.0"), daily.parse_version("6.9.9"))
        self.assertEqual(daily.parse_version("v6.1"), (6, 1, 0))

    def test_newer_found(self):
        self.assertEqual(daily.check_update("6.1.0", fetch=lambda: self.REL), ("6.2.0", "setup", "u2"))

    def test_up_to_date(self):
        self.assertIsNone(daily.check_update("6.2.0", fetch=lambda: self.REL))
        self.assertIsNone(daily.check_update("6.1.0", fetch=lambda: []))


class Search(Base):
    def test_finds_by_title_transcript_and_summary(self):
        self.rec("20261001_100000", 1, meta={"window": "Планёрка отдела продаж"})
        self.rec("20261002_100000", 1, text="[00:00:01] Собеседники: обсудим бюджет на ноябрь")
        self.rec("20261003_100000", 1, summary={"tasks": [{"task": "Подготовить смету"}]})
        self.assertEqual(daily.search(self.dir, "планёрка"), {"20261001_100000"})
        self.assertEqual(daily.search(self.dir, "Бюджет ноябрь"), {"20261002_100000"})
        self.assertEqual(daily.search(self.dir, "смету"), {"20261003_100000"})
        self.assertEqual(daily.search(self.dir, "бюджет декабрь"), set())
        self.assertIsNone(daily.search(self.dir, "  "))

    def test_escaped_json_is_searchable(self):
        wav = self.rec("20261004_100000", 1)
        wav.with_suffix(".json").write_text(json.dumps({"window": "Совещание"}, ensure_ascii=True), encoding="utf-8")
        self.assertEqual(daily.search(self.dir, "совещание"), {"20261004_100000"})


class Autostart(unittest.TestCase):
    def test_command(self):
        self.assertEqual(daily.autostart_command(r"C:\A B\MeetingHub.exe"), '"C:\\A B\\MeetingHub.exe" --minimized')

    @unittest.skipUnless(os.name == "nt", "Windows registry")
    def test_roundtrip_registry(self):
        before = daily.autostart_enabled()
        try:
            daily.set_autostart(True, r"C:\nowhere\MeetingHub.exe")
            self.assertTrue(daily.autostart_enabled())
            daily.set_autostart(False)
            self.assertFalse(daily.autostart_enabled())
        finally:
            if before:
                daily.set_autostart(True)


if __name__ == "__main__":
    unittest.main()
