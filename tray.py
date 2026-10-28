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


def make_app_icon(size=256):
    """Значок программы для ярлыков и exe: синий градиент, белая «M». Рисуется крупно и уменьшается."""
    from PIL import Image, ImageDraw
    big = size * 4
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    grad = Image.new("RGBA", (big, big))
    gd = ImageDraw.Draw(grad)
    top, bottom = (52, 130, 232), (20, 70, 150)
    for y in range(big):
        t = y / (big - 1)
        gd.line([(0, y), (big, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,))
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle((big * .03, big * .03, big * .97, big * .97), radius=big * .22, fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    w = big
    pts = [(w * .24, w * .72), (w * .24, w * .30), (w * .5, w * .55), (w * .76, w * .30), (w * .76, w * .72)]
    d.line(pts, fill=(255, 255, 255, 255), width=int(w * .1), joint="curve")
    for x, y in (pts[0], pts[1], pts[3], pts[4]):
        r = w * .05
        d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, 255))
    # три «звуковые» точки под буквой — намёк на запись голоса
    for k, x in enumerate((.38, .5, .62)):
        r = w * (.028 if k != 1 else .035)
        d.ellipse((w * x - r, w * .83 - r, w * x + r, w * .83 + r), fill=(190, 220, 255, 255))
    return img.resize((size, size), Image.LANCZOS)


class Tray:
    """Колбэки вызываются из потока значка — приложение само переносит их в поток интерфейса."""

    def __init__(self, on_open, on_record, on_quit, is_recording=lambda: False, elapsed=lambda: 0.0,
                 on_mute=lambda: None, is_muted=lambda: False,
                 on_mute_system=lambda: None, is_system_muted=lambda: False):
        self.on_open, self.on_record, self.on_quit = on_open, on_record, on_quit
        self.on_mute, self.is_muted = on_mute, is_muted
        self.on_mute_system, self.is_system_muted = on_mute_system, is_system_muted
        self.is_recording = is_recording
        self.elapsed = elapsed
        self.icon = None
        self._recording = None
        self._menu_key = None

    @staticmethod
    def rec_label(seconds):
        m = int(seconds // 60)
        return f"● ИДЁТ ЗАПИСЬ ВСТРЕЧИ · {m // 60}:{m % 60:02d}" if m >= 60 else f"● ИДЁТ ЗАПИСЬ ВСТРЕЧИ · {m} мин"

    @staticmethod
    def mute_label(muted):
        return "🎙 Включить мой микрофон в записи" if muted else "🔇 Выключить мой микрофон в записи"

    @staticmethod
    def system_label(muted):
        return "🔊 Снова писать звук компьютера" if muted else "🔇 Не писать звук компьютера (и собеседников)"

    @staticmethod
    def left_click_opens_menu(icon):
        """Щелчок левой кнопкой по значку показывает то же меню, что и правой. False — не вышло (останется
        стандартное действие: открыть окно)."""
        try:
            from pystray._util import win32
            handlers = icon._message_handlers
            orig = handlers[win32.WM_NOTIFY]
            left, right = win32.WM_LBUTTONUP, win32.WM_RBUTTONUP
        except Exception:
            return False

        def handler(wparam, lparam):
            return orig(wparam, right if lparam == left else lparam)
        handlers[win32.WM_NOTIFY] = handler
        return True

    def start(self):
        import pystray
        rec = lambda _i=None: bool(self.is_recording())
        menu = pystray.Menu(
            pystray.MenuItem(lambda _i: self.rec_label(self.elapsed()), lambda *_: None, enabled=False, visible=rec),
            pystray.MenuItem(lambda _i: self.mute_label(self.is_muted()), lambda *_: self.on_mute(), visible=rec),
            pystray.MenuItem(lambda _i: self.system_label(self.is_system_muted()), lambda *_: self.on_mute_system(),
                             visible=rec),
            pystray.MenuItem("■ Остановить запись", lambda *_: self.on_record(), visible=rec),
            pystray.MenuItem("Открыть Meeting Hub", lambda *_: self.on_open(), default=True),
            pystray.MenuItem("● Начать запись", lambda *_: self.on_record(), visible=lambda _i: not rec()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", lambda *_: self.on_quit()),
        )
        self.icon = pystray.Icon("MeetingHub", make_image(), "Meeting Hub", menu)
        self.left_menu = self.left_click_opens_menu(self.icon)
        self.icon.run_detached()
        return self

    def update(self, title):
        if self.icon is None:
            return
        try:
            rec = bool(self.is_recording())
            muted = bool(rec and self.is_muted())
            sys_muted = bool(rec and self.is_system_muted())
            key = (rec, int(self.elapsed() // 60) if rec else None, muted, sys_muted)
            if key != self._menu_key:               # меню Windows строится заранее — обновляем при смене
                self._menu_key = key
                self.icon.update_menu()
            if rec != self._recording:
                self._recording = rec
                self.icon.icon = make_image(rec)
            self.icon.title = (("● ИДЁТ ЗАПИСЬ" + (" (микрофон выключен)" if muted else "")
                                               + (" (звук компьютера не пишется)" if sys_muted else "") + " · " + title)
                               if rec else title)[:120]
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
