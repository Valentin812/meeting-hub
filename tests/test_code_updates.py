# -*- coding: utf-8 -*-
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packaging"))

import daily  # noqa: E402
import launcher  # noqa: E402
import make_code_zip  # noqa: E402
import runtime_tool  # noqa: E402
import updater  # noqa: E402


def make_state(data, active="7.14.1", previous="7.14.0", pending=True, attempts=0, runtime=1, versions=None):
    root = Path(data) / "code"
    for v in (versions or [active, previous]):
        if v:
            (root / v).mkdir(parents=True, exist_ok=True)
            (root / v / "code.json").write_text(json.dumps({"version": v, "runtime": runtime}))
    (root / "state.json").write_text(json.dumps(
        {"active": active, "previous": previous, "pending": pending, "attempts": attempts}))


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class ChooseCode(Base):
    def test_no_state_means_built_in_code(self):
        self.assertIsNone(launcher.choose_code(self.dir, "7.14.0", 1))

    def test_active_version_used_and_attempt_counted(self):
        make_state(self.dir)
        self.assertEqual(launcher.choose_code(self.dir, "7.14.0", 1).name, "7.14.1")
        state = json.loads((self.dir / "code" / "state.json").read_text())
        self.assertEqual(state["attempts"], 1)

    def test_rollback_after_two_failed_starts(self):
        make_state(self.dir, attempts=2)
        folder = launcher.choose_code(self.dir, "7.14.0", 1)
        self.assertIsNone(folder)                          # прежняя 7.14.0 не новее вшитой — берём вшитую
        state = json.loads((self.dir / "code" / "state.json").read_text())
        self.assertEqual(state["active"], "7.14.0")
        self.assertIn("7.14.1", state["bad"])

    def test_rollback_to_previous_newer_than_base(self):
        make_state(self.dir, active="7.15.0", previous="7.14.5", attempts=2)
        self.assertEqual(launcher.choose_code(self.dir, "7.14.0", 1).name, "7.14.5")

    def test_good_version_has_no_attempts(self):
        make_state(self.dir, pending=False, attempts=0)
        launcher.choose_code(self.dir, "7.14.0", 1)
        self.assertEqual(json.loads((self.dir / "code" / "state.json").read_text())["attempts"], 0)

    def test_not_used_when_older_or_needs_newer_base(self):
        make_state(self.dir)
        self.assertIsNone(launcher.choose_code(self.dir, "7.14.1", 1))      # exe уже не старше кода
        self.assertIsNone(launcher.choose_code(self.dir, "7.14.0", 0))
        make_state(self.dir, runtime=2)
        self.assertIsNone(launcher.choose_code(self.dir, "7.14.0", 1))      # код требует новой основы

    def test_broken_folder_ignored(self):
        make_state(self.dir)
        shutil.rmtree(self.dir / "code" / "7.14.1")
        self.assertIsNone(launcher.choose_code(self.dir, "7.14.0", 1))

    def test_force_rollback(self):
        make_state(self.dir, pending=False)
        launcher.force_rollback(self.dir)
        state = json.loads((self.dir / "code" / "state.json").read_text())
        self.assertTrue(state["pending"])
        self.assertEqual(state["attempts"], launcher.MAX_ATTEMPTS)


class Finder(Base):
    def test_module_comes_from_update_folder(self):
        (self.dir / "mh_fake_mod.py").write_text("VALUE = 'from-update'\n", encoding="utf-8")
        finder = launcher.CodeFinder(self.dir)
        sys.meta_path.insert(0, finder)
        try:
            mod = importlib.import_module("mh_fake_mod")
            self.assertEqual(mod.VALUE, "from-update")
            self.assertTrue(mod.__file__.startswith(str(self.dir)))
        finally:
            sys.meta_path.remove(finder)
            sys.modules.pop("mh_fake_mod", None)

    def test_other_modules_untouched(self):
        finder = launcher.CodeFinder(self.dir)
        self.assertIsNone(finder.find_spec("json"))
        self.assertIsNone(finder.find_spec("os.path", ["x"]))


class Assets(Base):
    def test_code_asset(self):
        rel = {"assets": [{"name": "MeetingHubSetup-v7.14.1.exe", "browser_download_url": "i"},
                          {"name": "MeetingHubCode-v7.14.1-r2.zip", "browser_download_url": "c", "size": 5,
                           "digest": "sha256:ab"}]}
        a = updater.code_asset(rel)
        self.assertEqual((a["version"], a["runtime"], a["url"]), ("7.14.1", 2, "c"))
        self.assertIsNone(updater.code_asset({"assets": [{"name": "MeetingHub-v1-portable.zip"}]}))

    def test_can_update_code(self):
        a = {"runtime": 1, "url": "u"}
        self.assertTrue(updater.can_update_code(a, 1))
        self.assertFalse(updater.can_update_code(a, 0))                 # запуск не из exe
        self.assertFalse(updater.can_update_code({"runtime": 2, "url": "u"}, 1))
        self.assertFalse(updater.can_update_code(None, 1))

    def test_check_update_carries_code_asset(self):
        rel = [{"tag_name": "v9.0.0", "html_url": "p", "assets": [
            {"name": "MeetingHubSetup-v9.0.0.exe", "browser_download_url": "i"},
            {"name": "MeetingHubCode-v9.0.0-r1.zip", "browser_download_url": "c"}]}]
        version, asset, page = daily.check_update("7.0.0", fetch=lambda: rel)
        self.assertEqual(asset["url"], "i")
        self.assertEqual(asset["code"]["url"], "c")
        rel[0]["assets"].pop(0)                                          # установщика нет, есть только код
        _v, asset, _p = daily.check_update("7.0.0", fetch=lambda: rel)
        self.assertEqual(asset["code"]["version"], "9.0.0")


class InstallCode(Base):
    def zip(self, version="7.14.1", extra=None, code=None, runtime=1):
        buf = self.dir / "in.zip"
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("app.py", "APP_VERSION = 'x'\n")
            z.writestr("code.json", json.dumps(code or {"version": version, "runtime": runtime}))
            for name, text in (extra or {}).items():
                z.writestr(name, text)
        return buf

    def test_real_archive_installs_and_rolls_state(self):
        data = self.dir / "data"
        z = make_code_zip.build("7.14.1", self.dir / "out")
        self.assertRegex(z.name, r"^MeetingHubCode-v7\.14\.1-r\d+\.zip$")
        with zipfile.ZipFile(z) as zf:
            names = zf.namelist()
        self.assertIn("app.py", names)
        self.assertNotIn("launcher.py", names)                           # запускающий файл не обновляется
        folder = updater.install_code(z, data, "7.14.1", runtime=runtime_tool.load()["runtime"])
        self.assertTrue((folder / "app.py").exists())
        state = json.loads((data / "code" / "state.json").read_text())
        self.assertEqual((state["active"], state["pending"]), ("7.14.1", True))
        z2 = make_code_zip.build("7.14.2", self.dir / "out")
        updater.install_code(z2, data, "7.14.2", runtime=runtime_tool.load()["runtime"])
        state = json.loads((data / "code" / "state.json").read_text())
        self.assertEqual((state["active"], state["previous"]), ("7.14.2", "7.14.1"))
        z3 = make_code_zip.build("7.14.3", self.dir / "out")
        updater.install_code(z3, data, "7.14.3", runtime=runtime_tool.load()["runtime"])
        self.assertFalse((data / "code" / "7.14.1").exists())             # старше предыдущей — удалена
        self.assertTrue(updater.mark_code_good(data))
        self.assertFalse(updater.mark_code_good(data))

    def test_rejects_bad_archives(self):
        data = self.dir / "data"
        bad = [
            (self.zip(extra={"../evil.py": "x"}), "7.14.1", 1),
            (self.zip(extra={"sub/mod.py": "x"}), "7.14.1", 1),
            (self.zip(extra={"run.exe": "MZ"}), "7.14.1", 1),
            (self.zip(extra={"broken.py": "def (:\n"}), "7.14.1", 1),
            (self.zip(version="7.14.9"), "7.14.1", 1),                    # версия не совпала
            (self.zip(runtime=2), "7.14.1", 1),                           # нужна новая основа
        ]
        for path, version, runtime in bad:
            with self.assertRaises(updater.UpdateError, msg=str(path)):
                updater.install_code(path, data, version, runtime=runtime)
            self.assertFalse((data / "code" / "state.json").exists())
            self.assertFalse(list((data / "code").glob("*.new")))

    def test_requires_app_module(self):
        p = self.dir / "noapp.zip"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("code.json", json.dumps({"version": "7.14.1", "runtime": 1}))
        with self.assertRaises(updater.UpdateError):
            updater.install_code(p, self.dir / "d", "7.14.1", runtime=1)


class RuntimeContract(unittest.TestCase):
    def test_exe_contains_everything_the_code_imports(self):
        """Если код начал импортировать новое, старый exe его не потянет: поднимите runtime —
        python packaging/runtime_tool.py --write (такое обновление придёт обычным установщиком)."""
        self.assertEqual(runtime_tool.missing(), [])

    def test_runtime_is_positive(self):
        self.assertGreaterEqual(runtime_tool.load()["runtime"], 1)


if __name__ == "__main__":
    unittest.main()
