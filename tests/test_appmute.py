import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import appmute  # noqa: E402


class AppMuteTests(unittest.TestCase):
    def test_parse_names(self):
        self.assertEqual(appmute.parse_names("Telegram.exe, WhatsApp;viber\nSlack.exe,telegram.exe"),
                         ["telegram.exe", "whatsapp.exe", "viber.exe", "slack.exe"])
        self.assertEqual(appmute.parse_names(""), [])

    def test_should_mute(self):
        names = appmute.parse_names(appmute.DEFAULT_LIST)
        self.assertTrue(appmute.should_mute("telegram.exe", names))
        self.assertFalse(appmute.should_mute("chrome.exe", names))
        self.assertFalse(appmute.should_mute("telegram.exe", names, keep=("Telegram.exe",)))   # приложение встречи
        self.assertFalse(appmute.should_mute("", names))
        self.assertTrue(appmute.should_mute("spotify.exe", names, all_mode=True))
        self.assertFalse(appmute.should_mute("chrome.exe", names, all_mode=True))                # звук встречи не трогаем
        self.assertFalse(appmute.should_mute("teams.exe", names, all_mode=True))

    def test_mute_pass_and_unmute(self):
        state = {1: ("telegram.exe", False), 2: ("chrome.exe", False), 3: ("outlook.exe", True)}   # pid → (имя, mute)

        def fake_each(visit):
            for pid, (proc, _m) in list(state.items()):
                visit(pid, proc, pid)

        def get_mute(pid):
            return state[pid][1]

        def set_mute(pid, value):
            state[pid] = (state[pid][0], value)

        with tempfile.TemporaryDirectory() as d, mock.patch.object(appmute, "_each_session", fake_each), \
                mock.patch.object(appmute, "_get_mute", get_mute), mock.patch.object(appmute, "_set_mute", set_mute):
            m = appmute.AppMuter(Path(d) / "b.json", appmute.parse_names(appmute.DEFAULT_LIST))
            m.mute_pass()
            self.assertTrue(state[1][1])            # мессенджер заглушён
            self.assertFalse(state[2][1])           # браузер — нет
            self.assertEqual(m.muted, {1: "telegram.exe"})      # outlook уже был выключен пользователем
            self.assertTrue((Path(d) / "b.json").exists())
            state[4] = ("telegram.exe", False)      # новый сеанс после звука уведомления
            m.mute_pass()
            self.assertTrue(state[4][1])
            m.unmute()
            self.assertFalse(state[1][1])
            self.assertTrue(state[3][1])            # то, что выключил пользователь, остаётся выключенным
            self.assertFalse((Path(d) / "b.json").exists())

    def test_restore_backup_without_windows(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(appmute.restore_backup(Path(d) / "none.json"), 0)


if __name__ == "__main__":
    unittest.main()
