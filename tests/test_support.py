# -*- coding: utf-8 -*-
"""Данные пользователя, перенос из старых версий, диагностика записи, отчёт для поддержки."""
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import support  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        # resolve(): на Windows временная папка бывает в коротком виде (RUNNER~1), а программа — в полном.
        self.dir = Path(tempfile.mkdtemp()).resolve()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class DataDir(Base):
    def test_override_env(self):
        with mock.patch.dict(os.environ, {"MEETINGHUB_DATA": str(self.dir / "d")}):
            self.assertEqual(support.data_dir(), self.dir / "d")
            self.assertTrue((self.dir / "d").is_dir())

    def test_frozen_uses_localappdata(self):
        exe = self.dir / "app" / "MeetingHub.exe"
        exe.parent.mkdir()
        exe.write_bytes(b"")
        env = {k: v for k, v in os.environ.items() if k != "MEETINGHUB_DATA"}
        env["LOCALAPPDATA"] = str(self.dir / "local")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", str(exe)):
            self.assertEqual(support.data_dir(), self.dir / "local" / "Meeting Hub")
            (exe.parent / "portable.txt").write_text("1")
            self.assertEqual(support.data_dir(), exe.parent)


class Import(Base):
    def make_old(self):
        old = self.dir / "old"
        (old / "recordings").mkdir(parents=True)
        (old / "meeting_hub_training.json").write_text('{"schema": 2, "meeting_patterns": [{"pattern": "x"}]}',
                                                      encoding="utf-8")
        (old / "recordings" / "20261023_100000.wav").write_bytes(b"RIFF")
        (old / "recordings" / "20261023_100000.json").write_text("{}")
        return old

    def test_import_copies_without_touching_source(self):
        old = self.make_old()
        new = self.dir / "new"
        new.mkdir()
        done = support.import_from(old, new)
        self.assertIn("meeting_hub_training.json", done)
        self.assertIn("записей: 1", done)
        self.assertTrue((new / "recordings" / "20261023_100000.wav").exists())
        self.assertTrue((old / "meeting_hub_training.json").exists())
        self.assertTrue(support.has_user_data(new))

    def test_import_never_overwrites(self):
        old = self.make_old()
        new = self.dir / "new"
        new.mkdir()
        (new / "meeting_hub_training.json").write_text("mine", encoding="utf-8")
        support.import_from(old, new)
        self.assertEqual((new / "meeting_hub_training.json").read_text(encoding="utf-8"), "mine")
        self.assertEqual(support.import_from(old, new), [])

    def test_same_folder_is_noop(self):
        old = self.make_old()
        self.assertEqual(support.import_from(old, old), [])

    def test_find_old_versions_in_downloads(self):
        old = self.dir / "Downloads" / "MeetingHub-v5.2.2-exe" / "MeetingHub-v5.2.2-exe"
        old.mkdir(parents=True)
        (old / "meeting_hub_training.json").write_text("{}")
        with mock.patch.object(Path, "home", return_value=self.dir):
            self.assertEqual(support.find_old_versions(), [old])

    def test_empty_folder_has_no_data(self):
        self.assertFalse(support.has_user_data(self.dir))


OK = {"device": "dev", "error": "", "peak": 0.3}


class Diagnose(unittest.TestCase):
    def test_all_good(self):
        self.assertEqual(support.diagnose({"mic": OK, "system": OK}), (None, None))

    def test_kaspersky_wins(self):
        res = {"mic": {"error": "invalid argument"}, "system": {"error": "invalid argument"}}
        code, guide = support.diagnose(res, kaspersky_loaded=True, privacy="Allow", security=["Kaspersky"])
        self.assertEqual(code, "kaspersky")
        self.assertIn("Доверенные", support.guide_text(guide))

    def test_privacy_denied(self):
        res = {"mic": {"error": "доступ запрещён (Error 0x80070005)"}, "system": OK}
        self.assertEqual(support.diagnose(res, privacy="Allow")[0], "privacy")
        self.assertEqual(support.diagnose({"mic": {"error": "x"}, "system": OK}, privacy="Deny")[0], "privacy")

    def test_no_device(self):
        res = {"mic": {"error": "устройство не найдено (Error 0x80070490)"}, "system": {"error": "x"}}
        self.assertEqual(support.diagnose(res)[0], "no_device")

    def test_other_antivirus_named_in_guide(self):
        res = {"mic": {"error": "invalid argument"}, "system": {"error": "invalid argument"}}
        code, guide = support.diagnose(res, security=["Dr.Web"])
        self.assertEqual(code, "other_av")
        self.assertIn("Dr.Web", guide["title"])
        self.assertTrue(all("{vendor}" not in s for s in guide["steps"]))
        self.assertIn("{vendor}", support.GUIDES["other_av"]["title"])   # шаблон не испорчен

    def test_only_system_sound_missing(self):
        self.assertEqual(support.diagnose({"mic": OK, "system": {"error": "x"}})[0], "partial_system")

    def test_unknown(self):
        self.assertEqual(support.diagnose({"mic": {"error": "x"}, "system": {"error": "y"}})[0], "unknown")


class Bundle(Base):
    def test_bundle_has_logs_and_no_audio(self):
        data = self.dir / "data"
        (data / "recordings").mkdir(parents=True)
        (data / "recordings" / "20261023_100000.wav").write_bytes(b"RIFF" * 100)
        (data / "meeting_hub.log").write_text("log line", encoding="utf-8")
        (data / "meeting_hub_training.json").write_text(json.dumps({
            "schema": 2, "meeting_patterns": [{"pattern": "Видеовстреча - "}],
            "journal": [{"time": "t", "prev": "IDLE", "new": "COUNTDOWN", "event": "e", "initiator": "auto",
                         "title": "Секретное совещание - Google Chrome", "label": "Google Meet"}]}),
            encoding="utf-8")
        report = self.dir / "meeting_hub_audio_report.txt"
        report.write_text("report", encoding="utf-8")
        out = support.support_bundle(self.dir / "s.zip", data, "5.3.0", report)
        with zipfile.ZipFile(out) as z:
            names = set(z.namelist())
            info = json.loads(z.read("system.json").decode("utf-8"))
            dump = z.read("system.json").decode("utf-8")
        self.assertEqual(names, {"system.json", "meeting_hub.log", "meeting_hub_audio_report.txt", "README.txt"})
        self.assertEqual(info["recordings"][0]["name"], "20261023_100000.wav")
        self.assertEqual(info["journal_summary"]["patterns"], 1)
        self.assertNotIn("Секретное совещание", dump)    # заголовки окон в отчёт не попадают


if __name__ == "__main__":
    unittest.main()
