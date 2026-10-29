# -*- coding: utf-8 -*-
"""Современное оформление элементов: скруглённые кнопки и поля, аккуратные галочки, вкладки с подчёркиванием,
тонкая прокрутка. Элементы рисуются картинками (без сторонних библиотек) и подключаются к ttk как «image»-элементы.
Если что-то не получилось, остаётся обычное оформление темы: ошибки здесь никогда не мешают запуску.

Функции *_rows возвращают сетку цветов («#rrggbb») — их можно проверить без окна.
"""
import math


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


def rounded_rows(w, h, r, fill, border, bg, bw=1.0):
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


def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _stroke(rows, points, color, width):
    """Нарисовать сглаженную ломаную поверх сетки."""
    h, w = len(rows), len(rows[0])
    for y in range(h):
        for x in range(w):
            d = min(_seg_dist(x + 0.5, y + 0.5, *points[i], *points[i + 1]) for i in range(len(points) - 1))
            cov = max(0.0, min(1.0, width / 2.0 + 0.5 - d))
            if cov > 0:
                rows[y][x] = mix(rows[y][x], color, cov)
    return rows


def check_rows(size, checked, fill, border, bg, mark, radius=None):
    rows = rounded_rows(size, size, radius if radius is not None else size * 0.28, fill, border, bg)
    if checked:
        s = size / 18.0
        _stroke(rows, [(4.6 * s, 9.4 * s), (7.7 * s, 12.5 * s), (13.4 * s, 5.8 * s)], mark, max(1.6, 2.0 * s))
    return rows


def radio_rows(size, checked, fill, border, bg, mark):
    rows = rounded_rows(size, size, size / 2.0, fill, border, bg)
    if checked:
        c, rad = size / 2.0, size * 0.2
        for y in range(size):
            for x in range(size):
                d = math.hypot(x + 0.5 - c, y + 0.5 - c)
                cov = max(0.0, min(1.0, rad + 0.5 - d))
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


def thumb_rows(w, h, margin, color, bg):
    """Бегунок прокрутки: тонкая «таблетка» по центру полосы."""
    rows = [[bg] * w for _ in range(h)]
    inner = rounded_rows(w - 2 * margin, h, (w - 2 * margin) / 2.0, color, color, bg, bw=0.0)
    for y in range(h):
        rows[y][margin:w - margin] = inner[y]
    return rows


def chevron_rows(w, h, fill, color):
    rows = [[fill] * w for _ in range(h)]
    cx, cy, s = w / 2.0, h / 2.0, min(w, h) / 6.0
    return _stroke(rows, [(cx - 2 * s, cy - s), (cx, cy + s), (cx + 2 * s, cy - s)], color, 1.8)


def to_data(rows):
    return " ".join("{" + " ".join(row) + "}" for row in rows)


# ---------------------------------------------------------------------------
# Подключение к ttk
# ---------------------------------------------------------------------------
def install(root, st, pal):
    """Скруглённые элементы для «плоских» тем. Возвращает список подключённых элементов."""
    if pal.get("relief") != "flat":
        return []
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

    r, bh, bw_ = px(9), px(36), px(36)
    # кнопки
    def button(prefix, fill, border, hover, press, off):
        return element(prefix, img(rounded_rows(bw_, bh, r, fill, border, bg)),
                       ("disabled", img(rounded_rows(bw_, bh, r, off, br, bg))),
                       ("pressed", img(rounded_rows(bw_, bh, r, press, border, bg))),
                       ("active", img(rounded_rows(bw_, bh, r, hover, border, bg))),
                       border=(r, r, r, r), sticky="nswe", padding=(r // 2, 2, r // 2, 2))
    button("Modern.Button.border", pal["btn"], br, pal["hover"], mix(pal["hover"], br, 0.6), bg)
    button("Modern.Accent.border", accent, accent, ahov, mix(ahov, "#000000", 0.15), pal["hover"])
    body = lambda ctrl: [("Modern.%s.border" % ctrl, {"sticky": "nswe", "children": [
        ("Button.focus", {"sticky": "nswe", "children": [
            ("Button.padding", {"sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})]})]})]})]
    layout("TButton", body("Button"), ["Modern.Button.border"])
    layout("Accent.TButton", body("Accent"), ["Modern.Accent.border"])
    layout("TMenubutton", [("Modern.Button.border", {"sticky": "nswe", "children": [
        ("Menubutton.focus", {"sticky": "nswe", "children": [
            ("Menubutton.indicator", {"side": "right", "sticky": ""}),
            ("Menubutton.padding", {"expand": "1", "sticky": "we", "children": [
                ("Menubutton.label", {"side": "left", "sticky": ""})]})]})]})], ["Modern.Button.border"])

    # поля ввода (на боковой панели другого цвета углы выдали бы себя — там оставляем прямые)
    if pal.get("sidebar", bg) == bg:
        fh, fr = px(34), px(8)
        element("Modern.Entry.field", img(rounded_rows(px(34), fh, fr, sf, br, bg)),
                ("focus", img(rounded_rows(px(34), fh, fr, sf, accent, bg, bw=px(1.5)))),
                ("disabled", img(rounded_rows(px(34), fh, fr, bg, br, bg))),
                border=(fr, fr, fr, fr), sticky="nswe", padding=(px(6), px(2), px(6), px(2)))
        layout("TEntry", [("Modern.Entry.field", {"sticky": "nswe", "children": [
            ("Entry.padding", {"sticky": "nswe", "children": [("Entry.textarea", {"sticky": "nswe"})]})]})],
               ["Modern.Entry.field"])
        element("Modern.Chevron", img(chevron_rows(px(18), px(16), sf, pal["muted"])))
        layout("TCombobox", [("Modern.Entry.field", {"sticky": "nswe", "children": [
            ("Modern.Chevron", {"side": "right", "sticky": ""}),
            ("Combobox.padding", {"expand": "1", "sticky": "nswe", "children": [
                ("Combobox.textarea", {"sticky": "nswe"})]})]})], ["Modern.Entry.field", "Modern.Chevron"])

    # галочки и переключатели
    cs = px(18)
    mark = pal["accent_fg"]
    element("Modern.check", img(check_rows(cs, False, sf, pal["faint"], bg, mark)),
            ("disabled", img(check_rows(cs, False, bg, br, bg, mark))),
            ("selected", "active", img(check_rows(cs, True, ahov, ahov, bg, mark))),
            ("selected", img(check_rows(cs, True, accent, accent, bg, mark))),
            ("active", img(check_rows(cs, False, sf, accent, bg, mark))),
            sticky="")
    layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
        ("Modern.check", {"side": "left", "sticky": ""}),
        ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
            ("Checkbutton.label", {"sticky": "nswe"})]})]})], ["Modern.check"])
    element("Modern.radio", img(radio_rows(cs, False, sf, pal["faint"], bg, accent)),
            ("selected", img(radio_rows(cs, True, sf, accent, bg, accent))),
            ("active", img(radio_rows(cs, False, sf, accent, bg, accent))), sticky="")
    layout("TRadiobutton", [("Radiobutton.padding", {"sticky": "nswe", "children": [
        ("Modern.radio", {"side": "left", "sticky": ""}),
        ("Radiobutton.focus", {"side": "left", "sticky": "w", "children": [
            ("Radiobutton.label", {"sticky": "nswe"})]})]})], ["Modern.radio"])

    # вкладки: линия снизу, у выбранной — акцентная
    th = px(38)
    element("Modern.tab", img(underline_tab_rows(px(30), th, bg, br)),
            ("selected", img(underline_tab_rows(px(30), th, bg, br, accent, px(3)))),
            ("active", img(underline_tab_rows(px(30), th, pal["hover"], br))),
            border=(px(4), px(4), px(4), px(4)), sticky="nswe", padding=(0, 0, 0, 0))
    layout("TNotebook.Tab", [("Modern.tab", {"sticky": "nswe", "children": [
        ("Notebook.padding", {"side": "top", "sticky": "nswe", "children": [
            ("Notebook.focus", {"side": "top", "sticky": "nswe", "children": [
                ("Notebook.label", {"side": "top", "sticky": ""})]})]})]})], ["Modern.tab"])

    # прокрутка: тонкий бегунок без стрелок
    sw, sh, sm = px(14), px(28), px(4)
    for orient, vert in (("Vertical", True), ("Horizontal", False)):
        rows_n = thumb_rows(sw, sh, sm, mix(br, pal["muted"], 0.35), bg)
        rows_h = thumb_rows(sw, sh, sm, pal["muted"], bg)
        if not vert:
            rows_n = [list(c) for c in zip(*rows_n)]
            rows_h = [list(c) for c in zip(*rows_h)]
        name = "Modern.%s.thumb" % orient
        edge = px(8)
        element(name, img(rows_n), ("active", img(rows_h)),
                border=(0, edge, 0, edge) if vert else (edge, 0, edge, 0), sticky="nswe")
        layout(orient + ".TScrollbar", [("%s.Scrollbar.trough" % orient, {"sticky": "ns" if vert else "we",
                                                                         "children": [(name, {"expand": "1", "sticky": "nswe"})]})],
               [name])
    try:
        st.configure("TScrollbar", troughcolor=bg, background=bg, borderwidth=0)
        st.configure("TNotebook", borderwidth=0, tabmargins=(0, 0, 0, 0))
        st.map("TNotebook.Tab", foreground=[("selected", accent), ("active", pal["text"])])
    except Exception:
        pass
    st._modern_images = keep                       # картинки должны жить, пока жива тема
    return done
