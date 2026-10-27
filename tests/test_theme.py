import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import theme  # noqa: E402

HEX = re.compile(r"^#[0-9a-f]{6}$")


def lum(c):
    r, g, b = (int(c[i:i + 2], 16) / 255 for i in (1, 3, 5))
    f = lambda v: v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class ThemeTests(unittest.TestCase):
    def test_all_colors_valid(self):
        for name, p in theme.THEMES.items():
            for key, val in p.items():
                if key == "pill":
                    for bg, fg in val.values():
                        self.assertRegex(bg, HEX, name); self.assertRegex(fg, HEX, name)
                elif key in ("bg", "surface", "text", "muted", "faint", "border", "accent", "accent_fg",
                             "accent_hover", "select", "select_fg", "hover", "card", "btn", "me", "they"):
                    self.assertRegex(val, HEX, f"{name}.{key}")
        self.assertEqual(set(theme.ORDER), set(theme.THEMES))

    def test_text_is_readable(self):
        for name, p in theme.THEMES.items():
            pairs = [("text", "bg"), ("text", "surface"), ("muted", "bg"), ("muted", "surface"),
                     ("select_fg", "select"), ("accent_fg", "accent"), ("text", "btn"), ("text", "hover"),
                     ("me", "surface"), ("they", "surface")]
            for fg, bg in pairs:
                self.assertGreaterEqual(contrast(p[fg], p[bg]), 4.5, f"{name}: {fg} на {bg}")
            for kind, (bg, fg) in p["pill"].items():
                self.assertGreaterEqual(contrast(fg, bg), 4.0, f"{name}: метка {kind}")

    def test_light_not_glaring_dark_not_black(self):
        self.assertLess(lum(theme.THEMES["light"]["bg"]), 0.84)         # заметно мягче белого
        self.assertLess(lum(theme.THEMES["light"]["surface"]), 0.93)
        self.assertGreater(lum(theme.THEMES["dark"]["bg"]), 0.025)       # не почти чёрный
        cap = theme.THEMES["cappuccino"]
        self.assertTrue(int(cap["bg"][1:3], 16) > int(cap["bg"][5:7], 16) + 20, "капучино должно быть тёплым")

    def test_resolve(self):
        self.assertEqual(theme.resolve("latte"), "latte")
        self.assertIn(theme.resolve("system"), ("light", "dark"))
        self.assertIn(theme.resolve("нет такой"), ("light", "dark"))


if __name__ == "__main__":
    unittest.main()
