# -*- coding: utf-8 -*-
"""Значок Meeting Hub в области уведомлений Windows (pystray). Без pystray программа работает как раньше."""
import threading


def available():
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
        return True
    except Exception:
        return False


def make_image(recording=False, size=64):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, size - 3, size - 3), radius=size // 5, fill=(26, 95, 180, 255))
    # «M» из линий — читается и на 16 px.
    w = size
    pts = [(w * .22, w * .74), (w * .22, w * .28), (w * .5, w * .56), (w * .78, w * .28), (w * .78, w * .74)]
    d.line(pts, fill=(255, 255, 255, 255), width=max(2, size // 9), joint="curve")
    if recording:
        r = size // 4
        d.ellipse((size - r - 2, 2, size - 2, r + 2), fill=(220, 40, 40, 255), outline=(255, 255, 255, 255))
    return img


class Tray:
    """Колбэки вызываются из потока значка — приложение само переносит их в поток интерфейса."""

    def __init__(self, on_open, on_record, on_quit, is_recording=lambda: False):
        self.on_open, self.on_record, self.on_quit = on_open, on_record, on_quit
        self.is_recording = is_recording
        self.icon = None
        self._recording = None

    def start(self):
        import pystray
        menu = pystray.Menu(
            pystray.MenuItem("Открыть Meeting Hub", lambda *_: self.on_open(), default=True),
            pystray.MenuItem(lambda _i: "■ Остановить запись" if self.is_recording() else "● Записать",
                             lambda *_: self.on_record()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", lambda *_: self.on_quit()),
        )
        self.icon = pystray.Icon("MeetingHub", make_image(), "Meeting Hub", menu)
        self.icon.run_detached()
        return self

    def update(self, title):
        if self.icon is None:
            return
        try:
            rec = bool(self.is_recording())
            if rec != self._recording:
                self._recording = rec
                self.icon.icon = make_image(rec)
            self.icon.title = title[:120]
        except Exception:
            pass

    def notify(self, text, title="Meeting Hub"):
        try:
            self.icon.notify(text, title)
        except Exception:
            pass

    def stop(self):
        if self.icon is not None:
            icon, self.icon = self.icon, None
            threading.Thread(target=icon.stop, daemon=True).start()
