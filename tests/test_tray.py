import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tray  # noqa: E402

try:
    from PIL import Image  # noqa: F401
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


class TrayTests(unittest.TestCase):
    @unittest.skipUnless(HAVE_PIL, "нет Pillow")
    def test_recording_icon_is_red(self):
        idle = tray.make_image(False, 32).getpixel((16, 4))
        rec = tray.make_image(True, 32).getpixel((16, 4))
        self.assertGreater(idle[2], idle[0])        # синий
        self.assertGreater(rec[0], rec[2] + 100)    # красный

    def test_label(self):
        self.assertEqual(tray.Tray.rec_label(0), "● ИДЁТ ЗАПИСЬ ВСТРЕЧИ · 0 мин")
        self.assertEqual(tray.Tray.rec_label(12 * 60 + 5), "● ИДЁТ ЗАПИСЬ ВСТРЕЧИ · 12 мин")
        self.assertEqual(tray.Tray.rec_label(3725), "● ИДЁТ ЗАПИСЬ ВСТРЕЧИ · 1:02")


class MuteLabelTests(unittest.TestCase):
    def test_labels(self):
        self.assertIn("Выключить", tray.Tray.mute_label(False))
        self.assertIn("Включить", tray.Tray.mute_label(True))
        self.assertIn("Не писать", tray.Tray.system_label(False))
        self.assertIn("Снова писать", tray.Tray.system_label(True))
        self.assertIn("Снять", tray.Tray.confidential_label(True))
        self.assertIn("конфиденциальная", tray.Tray.confidential_label(False))


class LeftClickTests(unittest.TestCase):
    def test_falls_back_safely(self):
        class Fake:
            pass
        self.assertFalse(tray.Tray.left_click_opens_menu(Fake()))     # без pystray/Windows — обычное поведение


if __name__ == "__main__":
    unittest.main()
