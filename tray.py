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
    # Во время записи значок целиком красный — заметно и на 16 px.
    d.rounded_rectangle((2, 2, size - 3, size - 3), radius=size // 5,
                        fill=(200, 30, 30, 255) if recording else (26, 95, 180, 255))
    # «M» из линий — читается и на 16 px.
    w = size
    pts = [(w * .22, w * .74), (w * .22, w * .28), (w * .5, w * .56), (w * .78, w * .28), (w * .78, w * .74)]
    d.line(pts, fill=(255, 255, 255, 255), width=max(2, size // 9), joint="curve")
    return img


class Tray:
    """Колбэки вызываются из потока значка — приложение само переносит их в поток интерфейса."""

    def __init__(self, on_open, on_record, on_quit, is_recording=lambda: False, elapsed=lambda: 0.0):
        self.on_open, self.on_record, self.on_quit = on_open, on_record, on_quit
        self.is_recording = is_recording
        self.elapsed = elapsed
        self.icon = None
        self._recording = None
        self._menu_key = None

    @staticmethod
    def rec_label(seconds):
        m = int(seconds // 60)
        return f"● ИДЁТ ЗАПИСЬ ВСТРЕЧИ · {m // 60}:{m % 60:02d}" if m >= 60 else f"● ИДЁТ ЗАПИСЬ ВСТРЕЧИ · {m} мин"

    def start(self):
        import pystray
        rec = lambda _i=None: bool(self.is_recording())
        menu = pystray.Menu(
            pystray.MenuItem(lambda _i: self.rec_label(self.elapsed()), lambda *_: None, enabled=False, visible=rec),
            pystray.MenuItem("■ Остановить запись", lambda *_: self.on_record(), visible=rec),
            pystray.MenuItem("Открыть Meeting Hub", lambda *_: self.on_open(), default=True),
            pystray.MenuItem("● Начать запись", lambda *_: self.on_record(), visible=lambda _i: not rec()),
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
            key = (rec, int(self.elapsed() // 60) if rec else None)
            if key != self._menu_key:               # меню Windows строится заранее — обновляем при смене
                self._menu_key = key
                self.icon.update_menu()
            if rec != self._recording:
                self._recording = rec
                self.icon.icon = make_image(rec)
            self.icon.title = ("● ИДЁТ ЗАПИСЬ · " + title if rec else title)[:120]
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
