# -*- coding: utf-8 -*-
"""Миграция и сохранность данных обучения на реальном файле v4.9 из репозитория."""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from training_store import TrainingStore  # noqa: E402
from meeting_detector import MeetingDetector  # noqa: E402

V49 = ROOT / "meeting_hub_training.json"
V49_SNAPSHOT = ROOT / "tests" / "data" / "meeting_hub_training_v49.json"


class Migration(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "meeting_hub_training.json"
        shutil.copy(V49_SNAPSHOT, self.path)
        self.original = json.loads(V49_SNAPSHOT.read_text(encoding="utf-8"))

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_v49_is_migrated_with_backup_and_history_intact(self):
        store = TrainingStore(self.path)
        backups = list(self.dir.glob("meeting_hub_training.v4-backup-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8")), self.original)
        legacy = store.data["legacy_v49"]
        self.assertEqual(legacy["events"], self.original["events"])
        self.assertEqual(legacy["feedback"], self.original["feedback"])
        self.assertEqual(legacy["app_rules"], self.original["app_rules"])
        self.assertEqual(len(legacy["events"]), 27)
        self.assertEqual(len(legacy["feedback"]), 8)
        self.assertEqual(store.data["schema"], 2)
        self.assertTrue(store.notices)

    def test_second_load_does_not_migrate_again(self):
        TrainingStore(self.path)
        TrainingStore(self.path)
        self.assertEqual(len(list(self.dir.glob("*.v4-backup-*.json"))), 1)

    def test_patterns_survive_restart_and_drive_detector(self):
        store = TrainingStore(self.path)
        self.assertTrue(store.add_pattern("Видеовстреча - ", "Корпоративный сервис"))
        self.assertFalse(store.add_pattern("видеовстреча – ", "дубль с другим тире"))
        reloaded = TrainingStore(self.path)
        self.assertEqual(len(reloaded.meeting_patterns()), 1)
        det = MeetingDetector(reloaded.meeting_patterns(), reloaded.ignore_patterns())
        self.assertIsNotNone(det.match_title("Видеовстреча - Комната 1 - Google Chrome"))

    def test_journal_labels_persist(self):
        store = TrainingStore(self.path)
        entry = store.log({"prev": "IDLE", "new": "COUNTDOWN", "event": "x", "reason": "y", "initiator": "auto"})
        store.set_label(entry["id"], "wrong")
        again = TrainingStore(self.path)
        self.assertEqual(again.data["journal"][-1]["user_label"], "wrong")

    def test_broken_file_is_kept_not_lost(self):
        self.path.write_text("{ не json", encoding="utf-8")
        store = TrainingStore(self.path)
        self.assertEqual(len(list(self.dir.glob("*.broken-*.json"))), 1)
        self.assertEqual(store.data["schema"], 2)

    def test_export_import_roundtrip_merges_without_loss(self):
        store = TrainingStore(self.path)
        store.add_pattern("Видеовстреча - ", "A")
        exported = self.dir / "export.json"
        store.export(exported)
        other_path = self.dir / "other" / "meeting_hub_training.json"
        other_path.parent.mkdir()
        other = TrainingStore(other_path)
        other.add_pattern("Совещание - ", "B")
        added = other.import_file(exported)
        self.assertGreaterEqual(added, 1)
        patterns = {p["pattern"] for p in other.meeting_patterns()}
        self.assertEqual(patterns, {"Видеовстреча - ", "Совещание - "})
        self.assertEqual(other.data["legacy_v49"]["events"], self.original["events"])


class RepositoryFile(unittest.TestCase):
    def test_repository_training_file_still_loads(self):
        """Файл в репозитории — либо исходный v4.9, либо уже схема 2; оба должны читаться."""
        data = json.loads(V49.read_text(encoding="utf-8"))
        self.assertTrue("events" in data or data.get("schema") == 2)


if __name__ == "__main__":
    unittest.main()
