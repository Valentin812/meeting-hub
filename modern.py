# -*- coding: utf-8 -*-
"""Современное оформление элементов: скруглённые кнопки и поля, аккуратные галочки, вкладки с подчёркиванием,
тонкая прокрутка. Элементы рисуются картинками (без сторонних библиотек) и подключаются к ttk как «image»-элементы.
Если что-то не получилось, остаётся обычное оформление темы: ошибки здесь никогда не мешают запуску.

Функции *_rows возвращают сетку цветов («#rrggbb») — их можно проверить без окна.
"""
import math


_ORIG = {}         # исходные раскладки элементов ttk: к ним возвращаемся при смене темы
_COUNT = 0
_ALIVE = []        # PhotoImage удаляется вместе с объектом — держим ссылки на уровне модуля


def _rgb(c):
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def mix(a, b, t):
    """Смесь цветов: t=0 → a, t=1 → b."""
    ra, rb = _rgb(a), _rgb(b)
    t = max(0.0, min(1.0, t))
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(ra, rb))


def _sd_round_rect(px, py, w, h, r):
    """Расстояние (со знаком) от точки до скруглённого прямоугольника; внутри — отрицательное."""
    qx = abs(px - w / 2.0) - (w / 2.0 - r)
    qy = abs(py - h / 2.0) - (h / 2.0 - r)
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r


def rounded_rows(w, h, r, fill, border, bg, bw=1.0, bevel=False):
    """Скруглённый прямоугольник с рамкой; за его пределами — цвет bg. Края сглажены."""
    r = min(r, w / 2.0, h / 2.0)
    rows = []
    for y in range(h):
        row = []
        for x in range(w):
            sd = _sd_round_rect(x + 0.5, y + 0.5, w, h, r)
            outer = max(0.0, min(1.0, 0.5 - sd))
            inner = max(0.0, min(1.0, 0.5 - (sd + bw)))
            col = mix(bg, border, outer)
            row.append(mix(col, fill, inner) if outer > 0 else bg)
        rows.append(row)
    return rows


def _upscale(rows, cell, w, h):
    big = [[c for c in row for _ in range(cell)] for row in rows for _ in range(cell)]
    big = [(r + [r[-1]] * w)[:w] for r in big]
    return (big + [big[-1]] * h)[:h]


def pixel_rows(w, h, notch, fill, border, bg, bw=2, bevel=False, cell=3):
    """Пиксельная плашка: крупные «пиксели» (cell × cell точек), ступенчатые углы, жёсткая рамка, у кнопок —
    тёмная нижняя грань. Без сглаживания."""
    if cell > 1 and w >= cell * 8 and h >= cell * 8:
        small = pixel_rows(w // cell, h // cell, 2, fill, border, bg, 1, bevel, cell=1)
        return _upscale(small, cell, w, h)
    notch, bw = int(notch), int(bw)

    def inside(x, y):
        if x < 0 or y < 0 or x >= w or y >= h:
            return False
        dx, dy = min(x, w - 1 - x), min(y, h - 1 - y)
        return dx + dy >= notch

    shade = mix(fill, "#000000", 0.28)
    rows = []
    for y in range(h):
        row = []
        for x in range(w):
            if not inside(x, y):
                row.append(bg)
            elif not all(inside(x + i, y + j) for i in range(-bw, bw + 1) for j in range(-bw, bw + 1)
                         if abs(i) + abs(j) <= bw):
                row.append(border)
            elif bevel and y >= h - bw - (3 if h > 12 else 1):
                row.append(shade)
            else:
                row.append(fill)
        rows.append(row)
    return rows


def pixel_tab_rows(w, h, notch, fill, border, bg, bw=2):
    """Вкладка-«кирпичик»: рамка сверху и по бокам, снизу открыта."""
    rows = pixel_rows(w, h + bw + 1, notch, fill, border, bg, bw)[:h]
    return rows


def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _stroke(rows, points, color, width, hard=False):
    """Нарисовать ломаную поверх сетки: сглаженную или (hard) жёсткую, по пикселям."""
    h, w = len(rows), len(rows[0])
    for y in range(h):
        for x in range(w):
            d = min(_seg_dist(x + 0.5, y + 0.5, *points[i], *points[i + 1]) for i in range(len(points) - 1))
            cov = max(0.0, min(1.0, width / 2.0 + 0.5 - d))
            if hard:
                cov = 1.0 if cov >= 0.5 else 0.0
            if cov > 0:
                rows[y][x] = mix(rows[y][x], color, cov)
    return rows


def check_rows(size, checked, fill, border, bg, mark, radius=None, box=None):
    pixel = box is pixel_rows
    rows = (box or rounded_rows)(size, size, radius if radius is not None else (2 if pixel else size * 0.28),
                                 fill, border, bg)
    if checked:
        s = size / 18.0
        _stroke(rows, [(4.6 * s, 9.4 * s), (7.7 * s, 12.5 * s), (13.4 * s, 5.8 * s)], mark,
                max(2.4 if pixel else 1.6, 2.0 * s), hard=pixel)
    return rows


def radio_rows(size, checked, fill, border, bg, mark, pixel=False):
    rows = (pixel_rows(size, size, 4, fill, border, bg) if pixel
            else rounded_rows(size, size, size / 2.0, fill, border, bg))
    if checked:
        c, rad = size / 2.0, size * 0.2
        for y in range(size):
            for x in range(size):
                d = math.hypot(x + 0.5 - c, y + 0.5 - c)
                cov = max(0.0, min(1.0, rad + 0.5 - d))
                if pixel:
                    cov = 1.0 if cov >= 0.5 else 0.0
                if cov > 0:
                    rows[y][x] = mix(rows[y][x], mark, cov)
    return rows


def underline_tab_rows(w, h, bg, base, accent=None, thick=3):
    rows = [[bg] * w for _ in range(h)]
    rows[h - 1] = [base] * w
    if accent:
        for y in range(h - thick, h):
            rows[y] = [accent] * w
    return rows


def segment_rows(w, h, inset, r, fill, border, track):
    """Сегмент переключателя: фон-дорожка и скруглённая плашка внутри с отступом inset."""
    rows = [[track] * w for _ in range(h)]
    inner = rounded_rows(w - 2 * inset, h - 2 * inset, r, fill, border, track)
    for y in range(h - 2 * inset):
        rows[y + inset][inset:w - inset] = inner[y]
    return rows


def thumb_rows(w, h, margin, color, bg, pixel=False):
    """Бегунок прокрутки: тонкая «таблетка» по центру полосы (в пикселе — квадратный блок с рамкой)."""
    rows = [[bg] * w for _ in range(h)]
    if pixel:
        inner = pixel_rows(w - 2 * margin, h, 2, color, mix(color, "#000000", 0.4), bg, bw=2)
    else:
        inner = rounded_rows(w - 2 * margin, h, (w - 2 * margin) / 2.0, color, color, bg, bw=0.0)
    for y in range(h):
        rows[y][margin:w - margin] = inner[y]
    return rows


def chevron_rows(w, h, fill, color, pixel=False):
    rows = [[fill] * w for _ in range(h)]
    cx, cy, s = w / 2.0, h / 2.0, min(w, h) / 6.0
    return _stroke(rows, [(cx - 2 * s, cy - s), (cx, cy + s), (cx + 2 * s, cy - s)], color,
                   2.6 if pixel else 1.8, hard=pixel)


def to_data(rows):
    return " ".join("{" + " ".join(row) + "}" for row in rows)


STYLES = ("TButton", "Accent.TButton", "TMenubutton", "TEntry", "Side.TEntry", "TCombobox", "TCheckbutton",
          "TRadiobutton", "TNotebook.Tab", "Vertical.TScrollbar", "Horizontal.TScrollbar")


def reset(st):
    """Вернуть стандартные раскладки. Исходные снимаются один раз, до первой замены: иначе «исходной»
    у Accent.TButton оказалась бы уже заменённая раскладка TButton с картинками прошлой темы."""
    if not _ORIG:
        for name in STYLES:
            try:
                _ORIG[name] = st.layout(name)
            except Exception:
                pass
    for name, spec in _ORIG.items():
        try:
            st.layout(name, spec)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Подключение к ttk
# ---------------------------------------------------------------------------
def install(root, st, pal):
    """Скруглённые элементы для «плоских» тем. Возвращает список подключённых элементов."""
    reset(st)                                    # вернуть стандартную раскладку: тема могла смениться «на лету»
    kind = pal.get("relief")
    if kind not in ("flat", "pixel"):
        return []
    global _COUNT
    _COUNT += 1
    P = "Mod%d." % _COUNT                        # у каждой темы свои имена элементов: старые не мешают новым
    pixel = kind == "pixel"
    import tkinter as tk
    try:
        k = max(1.0, min(2.5, float(root.tk.call("tk", "scaling")) / (96.0 / 72.0)))
    except Exception:
        k = 1.0

    def px(v):
        return max(1, int(round(v * k)))

    bg, sf, br = pal["bg"], pal["surface"], pal["border"]
    accent, ahov = pal["accent"], pal["accent_hover"]
    keep, done = [], []

    def img(rows):
        im = tk.PhotoImage(master=root, width=len(rows[0]), height=len(rows))
        im.put(to_data(rows))
        keep.append(im)
        return str(im)

    def element(name, first, *states, **opts):
        try:
            st.element_create(name, "image", first, *states, **opts)
            done.append(name)
            return True
        except Exception:
            return False

    def layout(name, spec, needs):
        if all(n in done for n in needs):
            try:
                st.layout(name, spec)
            except Exception:
                pass

    box = pixel_rows if pixel else rounded_rows
    bwid = 2 * px(1) if pixel else 1.0
    mac = "sidebar" in pal and not pixel       # Mac-темы: свои кнопки и вкладки-переключатель
    # r — и радиус, и неизменяемая рамка картинки: всё, что не однотонно, должно попасть в рамку,
    # иначе середина повторяется «плиткой». У пикселя углы и нижняя грань занимают 3 крупных пикселя.
    r, bh, bw_ = (px(9) if pixel else px(6) if mac else px(9)), px(36), px(36)
    # кнопки
    def button(prefix, fill, border, hover, press, off):
        return element(prefix, img(box(bw_, bh, r, fill, border, bg, bwid, bevel=pixel)),
                       ("disabled", img(box(bw_, bh, r, off, br, bg, bwid))),
                       ("pressed", img(box(bw_, bh, r, press, border, bg, bwid))),
                       ("active", img(box(bw_, bh, r, hover, border, bg, bwid, bevel=pixel))),
                       border=(r, r, r, r), sticky="nswe", padding=(r // 2, 2, r // 2, 2))
    button(P + "Button.border", pal["btn"], br, pal["hover"], mix(pal["hover"], br, 0.6), bg)
    button(P + "Accent.border", accent, accent, ahov, mix(ahov, "#000000", 0.15), pal["hover"])
    body = lambda ctrl: [(P + "%s.border" % ctrl, {"sticky": "nswe", "children": [
        ("Button.focus", {"sticky": "nswe", "children": [
            ("Button.padding", {"sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})]})]})]})]
    layout("TButton", body("Button"), [P + "Button.border"])
    layout("Accent.TButton", body("Accent"), [P + "Accent.border"])
    layout("TMenubutton", [(P + "Button.border", {"sticky": "nswe", "children": [
        ("Menubutton.focus", {"sticky": "nswe", "children": [
            ("Menubutton.indicator", {"side": "right", "sticky": ""}),
            ("Menubutton.padding", {"expand": "1", "sticky": "we", "children": [
                ("Menubutton.label", {"side": "left", "sticky": ""})]})]})]})], [P + "Button.border"])

    # поля ввода; для поля на боковой панели (Side.TEntry) углы закрашены цветом панели
    if True:
        fh, fr = px(34), (px(9) if pixel else px(6) if mac else px(8))
        fbw = 2 * px(1) if pixel else 1.0

        def field(name, corner):
            element(name, img(box(px(34), fh, fr, sf, br, corner, fbw)),
                    ("focus", img(box(px(34), fh, fr, sf, accent, corner, 2 * px(1) if pixel else px(1.5)))),
                    ("disabled", img(box(px(34), fh, fr, bg, br, corner, fbw))),
                    border=(fr, fr, fr, fr), sticky="nswe", padding=(px(6), px(2), px(6), px(2)))
        field(P + "Entry.field", bg)
        side = pal.get("sidebar", bg)
        field(P + "SideEntry.field", side)
        for style_name, fld in (("TEntry", P + "Entry.field"), ("Side.TEntry", P + "SideEntry.field")):
            layout(style_name, [(fld, {"sticky": "nswe", "children": [
                ("Entry.padding", {"sticky": "nswe", "children": [("Entry.textarea", {"sticky": "nswe"})]})]})],
                   [fld])
        element(P + "Chevron", img(chevron_rows(px(18), px(16), sf, pal["muted"], pixel)))
        layout("TCombobox", [(P + "Entry.field", {"sticky": "nswe", "children": [
            (P + "Chevron", {"side": "right", "sticky": ""}),
            ("Combobox.padding", {"expand": "1", "sticky": "nswe", "children": [
                ("Combobox.textarea", {"sticky": "nswe"})]})]})], [P + "Entry.field", P + "Chevron"])

    # галочки и переключатели
    cs = px(18)
    mark = pal["accent_fg"]
    ck = lambda on, f, b: img(check_rows(cs, on, f, b, bg, mark, radius=px(4) if mac else None, box=box))
    element(P + "check", ck(False, sf, pal["faint"]), ("disabled", ck(False, bg, br)),
            ("selected", "active", ck(True, ahov, ahov)), ("selected", ck(True, accent, accent)),
            ("active", ck(False, sf, accent)), sticky="")
    layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
        (P + "check", {"side": "left", "sticky": ""}),
        ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
            ("Checkbutton.label", {"sticky": "nswe"})]})]})], [P + "check"])
    element(P + "radio", img(radio_rows(cs, False, sf, pal["faint"], bg, accent, pixel)),
            ("selected", img(radio_rows(cs, True, sf, accent, bg, accent, pixel))),
            ("active", img(radio_rows(cs, False, sf, accent, bg, accent, pixel))), sticky="")
    layout("TRadiobutton", [("Radiobutton.padding", {"sticky": "nswe", "children": [
        (P + "radio", {"side": "left", "sticky": ""}),
        ("Radiobutton.focus", {"side": "left", "sticky": "w", "children": [
            ("Radiobutton.label", {"sticky": "nswe"})]})]})], [P + "radio"])

    # вкладки: линия снизу, у выбранной — акцентная
    th = px(38)
    if pixel:
        pt = lambda fill: img(pixel_tab_rows(px(30), th, px(3), fill, br, bg, 2 * px(1)))
        element(P + "tab", pt(pal["btn"]), ("selected", pt(accent)), ("active", pt(pal["hover"])),
                border=(px(9), px(9), px(9), px(1)), sticky="nswe", padding=(0, 0, 0, 0))
    elif mac:                                    # как переключатель в macOS: выбранная — белая «таблетка»
        track = pal["hover"]
        seg = lambda fill, edge: img(segment_rows(px(30), px(32), px(2), px(7), fill, edge, track))
        element(P + "tab", seg(track, track), ("selected", seg(sf, br)), ("active", seg(mix(track, sf, 0.5), track)),
                border=(px(10), px(10), px(10), px(10)), sticky="nswe", padding=(0, 0, 0, 0))
    else:
        element(P + "tab", img(underline_tab_rows(px(30), th, bg, br)),
                ("selected", img(underline_tab_rows(px(30), th, bg, br, accent, px(3)))),
                ("active", img(underline_tab_rows(px(30), th, pal["hover"], br))),
                border=(px(4), px(4), px(4), px(4)), sticky="nswe", padding=(0, 0, 0, 0))
    layout("TNotebook.Tab", [(P + "tab", {"sticky": "nswe", "children": [
        ("Notebook.padding", {"side": "top", "sticky": "nswe", "children": [
            ("Notebook.focus", {"side": "top", "sticky": "nswe", "children": [
                ("Notebook.label", {"side": "top", "sticky": ""})]})]})]})], [P + "tab"])

    # прокрутка: тонкий бегунок без стрелок
    sw, sh, sm = px(14), px(28), px(4)
    for orient, vert in (("Vertical", True), ("Horizontal", False)):
        rows_n = thumb_rows(sw, sh, sm, accent if pixel else mix(br, pal["muted"], 0.35), bg, pixel)
        rows_h = thumb_rows(sw, sh, sm, ahov if pixel else pal["muted"], bg, pixel)
        if not vert:
            rows_n = [list(c) for c in zip(*rows_n)]
            rows_h = [list(c) for c in zip(*rows_h)]
        name = P + "%s.thumb" % orient
        edge = px(8)
        element(name, img(rows_n), ("active", img(rows_h)),
                border=(0, edge, 0, edge) if vert else (edge, 0, edge, 0), sticky="nswe")
        layout(orient + ".TScrollbar", [("%s.Scrollbar.trough" % orient, {"sticky": "ns" if vert else "we",
                                                                         "children": [(name, {"expand": "1", "sticky": "nswe"})]})],
               [name])
    try:
        st.configure("TScrollbar", troughcolor=bg, background=bg, borderwidth=0)
        st.configure("TNotebook", borderwidth=0, tabmargins=(0, 0, 0, 0))
        if pixel:
            st.map("TNotebook.Tab", foreground=[("selected", pal["accent_fg"]), ("active", pal["text"])])
        else:
            st.map("TNotebook.Tab", foreground=[("selected", pal["text"] if mac else accent),
                                                ("active", pal["text"])])
    except Exception:
        pass
    _ALIVE.extend(keep)           # картинки должны жить всё время работы: иначе элементы рисуются пустыми
    return done
