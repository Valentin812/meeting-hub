import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import modern  # noqa: E402
import theme  # noqa: E402


class ModernTests(unittest.TestCase):
    def test_rounded_corners_and_fill(self):
        rows = modern.rounded_rows(40, 30, 9, "#ffffff", "#000000", "#808080")
        self.assertEqual(rows[0][0], "#808080")              # угол — цвет фона
        self.assertEqual(rows[15][20], "#ffffff")            # середина — заливка
        self.assertNotEqual(rows[15][0], "#808080")          # край — рамка
        self.assertEqual((len(rows), len(rows[0])), (30, 40))

    def test_check_has_mark(self):
        rows = modern.check_rows(18, True, "#3b5bdb", "#3b5bdb", "#e8ecf2", "#ffffff")
        self.assertTrue(any(c == "#ffffff" for row in rows for c in row))
        plain = modern.check_rows(18, False, "#ffffff", "#888888", "#e8ecf2", "#3b5bdb")
        self.assertFalse(any(c == "#3b5bdb" for row in plain for c in row))

    def test_all_flat_themes_build_rows(self):
        for name, p in theme.THEMES.items():
            if p["relief"] != "flat":
                continue
            rows = modern.thumb_rows(14, 28, 4, p["border"], p["bg"])
            self.assertEqual(len(rows[0]), 14)
            self.assertEqual(rows[5][0], p["bg"])
            self.assertIn("{", modern.to_data(modern.underline_tab_rows(10, 8, p["bg"], p["border"], p["accent"])))

    def test_images_kept_at_module_level(self):
        self.assertIsInstance(modern._ALIVE, list)      # иначе PhotoImage удаляются и элементы рисуются пустыми

    def test_retro_themes_untouched(self):
        class Boom:
            def __getattr__(self, _n):
                raise AssertionError("не должно вызываться")
        self.assertEqual(modern.install(Boom(), Boom(), {"relief": "win95"}), [])


if __name__ == "__main__":
    unittest.main()
