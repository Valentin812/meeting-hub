import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import winsounds  # noqa: E402


class FakeReg:
    """Минимальный двойник winreg: дерево ключей и значения по умолчанию."""
    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2

    class Key:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def __init__(self, values):
        self.values = dict(values)      # путь ключа -> (значение, тип)

    def _children(self, path):
        pre = path + "\\"
        return sorted({p[len(pre):].split("\\")[0] for p in self.values if p.startswith(pre)})

    def OpenKey(self, base, path, *a):
        path = base.path + "\\" + path if isinstance(base, self.Key) else path
        if path not in self.values and not self._children(path):
            raise OSError("нет ключа")
        return self.Key(path)

    def EnumKey(self, key, i):
        kids = self._children(key.path)
        if i >= len(kids):
            raise OSError
        return kids[i]

    def QueryValueEx(self, key, name):
        return self.values[key.path]

    def SetValueEx(self, key, name, _r, kind, value):
        self.values[key.path] = (value, kind)


EVENTS = {
    r"AppEvents\Schemes\Apps\.Default\Notification.Default\.Current": (r"C:\Windows\Media\a.wav", 2),
    r"AppEvents\Schemes\Apps\.Default\SystemAsterisk\.Current": (r"C:\Windows\Media\b.wav", 2),
    r"AppEvents\Schemes\Apps\Explorer\Navigating\.Current": (r"C:\Windows\Media\c.wav", 1),
    r"AppEvents\Schemes\Apps\.Default\Silent\.Current": ("", 1),
}


class WinSoundsTests(unittest.TestCase):
    def test_mute_and_restore(self):
        reg = FakeReg(EVENTS)
        with tempfile.TemporaryDirectory() as d:
            backup = Path(d) / "b.json"
            self.assertEqual(winsounds.mute(backup, reg), 3)
            self.assertTrue(winsounds.is_muted(backup))
            self.assertTrue(all(v[0] == "" for v in reg.values.values()))
            self.assertEqual(winsounds.mute(backup, reg), 0)          # повторно — копия не затирается
            self.assertEqual(winsounds.restore(backup, reg), 3)
            self.assertFalse(winsounds.is_muted(backup))
            for path, val in EVENTS.items():
                self.assertEqual(reg.values[path], val)

    def test_nothing_to_mute(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(winsounds.mute(Path(d) / "b.json", FakeReg({})), 0)
            self.assertEqual(winsounds.restore(Path(d) / "none.json", FakeReg({})), 0)


if __name__ == "__main__":
    unittest.main()
