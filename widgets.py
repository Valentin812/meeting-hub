# -*- coding: utf-8 -*-
"""Нестандартные элементы интерфейса: скруглённая «пилюля» статуса и список встреч карточками."""
import sys
import tkinter as tk
from tkinter import ttk, font as tkfont

import theme

CHIP = {"done": "ok", "text": "meet", "audio": "off", "busy": "busy", "error": "rec"}


def _round_rect(canvas, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
           x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


class Pill(tk.Canvas):
    """Скруглённая метка: текст на цветном фоне."""

    def __init__(self, master, pal, size=10):
        super().__init__(master, height=28, width=120, highlightthickness=0, bd=0, bg=pal["bg"])
        self.font = tkfont.Font(family=theme.FONT, size=size, weight="bold")
        self.pal = pal
        self._last = None

    def restyle(self, pal):
        self.pal = pal
        self.font.configure(family=theme.FONT)
        self._last = None

    def set(self, text, bg, fg, outer=None):
        if outer:
            self.configure(bg=outer)
        key = (text, bg, fg, self["bg"])
        if key == self._last:
            return
        self._last = key
        w = self.font.measure(text) + 28
        self.configure(width=w)
        self.delete("all")
        _round_rect(self, 1, 2, w - 1, 26, 12, fill=bg, outline=bg)
        self.create_text(w // 2, 14, text=text, fill=fg, font=self.font)

    def cget_text(self):
        return self._last[0] if self._last else ""


class MeetingList(ttk.Frame):
    """Список встреч карточками: день → встречи; заголовок, время и цветная метка состояния.

    rows: [{"day", "iid", "title", "info", "chip", "key"}]. Выбор — on_select(iid), двойной щелчок — on_open(iid).
    """

    def __init__(self, master, pal, on_select, on_open):
        super().__init__(master)
        self.pal = pal
        self.on_select, self.on_open = on_select, on_open
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, bg=pal["bg"])
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=pal["bg"])
        self.win = self.canvas.create_window(0, 0, anchor="nw", window=self.inner)
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.win, width=e.width))
        self.canvas.bind("<Enter>", lambda _e: self._wheel(True))
        self.canvas.bind("<Leave>", lambda _e: self._wheel(False))
        self.title_font = tkfont.Font(family=theme.FONT, size=10, weight="bold")
        self.small_font = tkfont.Font(family=theme.FONT, size=9)
        self.day_font = tkfont.Font(family=theme.FONT, size=9, weight="bold")
        self.cards = {}            # iid -> dict(widgets)
        self.order = []
        self.selected = None
        self._structure = None

    # --- прокрутка колесом
    def _wheel(self, on):
        if on:
            self.canvas.bind_all("<MouseWheel>", self._on_wheel)
        else:
            self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, e):
        if self.canvas.yview() != (0.0, 1.0):
            self.canvas.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")

    # --- данные
    def rows(self):
        return list(self.order)

    def exists(self, iid):
        return iid in self.cards

    def set_pal(self, pal):
        self.pal = pal
        self._structure = None
        for f in (self.title_font, self.small_font, self.day_font):
            f.configure(family=theme.FONT)

    def set_rows(self, rows):
        structure = [(r["day"], r["iid"]) for r in rows]
        if structure != self._structure:
            for child in self.inner.winfo_children():
                child.destroy()
            self.cards, self.order = {}, []
            self.canvas.configure(bg=self.pal["bg"])
            self.inner.configure(bg=self.pal["bg"])
            day = None
            for r in rows:
                if r["day"] != day:
                    day = r["day"]
                    tk.Label(self.inner, text=day.upper(), font=self.day_font, fg=self.pal["faint"],
                             bg=self.pal["bg"], anchor="w").pack(fill="x", padx=10, pady=(14 if self.order else 4, 4))
                self._make_card(r)
            self._structure = structure
            if self.selected not in self.cards:
                self.selected = None
        for r in rows:
            self._update_card(r)
        self._paint_all()

    def _make_card(self, r):
        pal = self.pal
        card = tk.Frame(self.inner, bg=pal["bg"], cursor="hand2")
        card.pack(fill="x", padx=4, pady=1)
        bar = tk.Frame(card, width=3, bg=pal["bg"])
        bar.pack(side="left", fill="y")
        body = tk.Frame(card, bg=pal["bg"])
        body.pack(side="left", fill="both", expand=True, padx=(9, 10), pady=7)
        chip = tk.Label(body, font=self.small_font, padx=7, pady=1)
        chip.pack(side="right", anchor="n", pady=(2, 0))
        title = tk.Label(body, font=self.title_font, anchor="w", justify="left", bg=pal["bg"], fg=pal["text"])
        title.pack(fill="x")
        info = tk.Label(body, font=self.small_font, anchor="w", bg=pal["bg"], fg=pal["muted"])
        info.pack(fill="x")
        w = {"card": card, "bar": bar, "body": body, "title": title, "info": info, "chip": chip, "hover": False}
        self.cards[r["iid"]] = w
        self.order.append(r["iid"])
        for widget in (card, body, title, info, chip, bar):
            widget.bind("<Button-1>", lambda _e, i=r["iid"]: self.select(i, notify=True))
            widget.bind("<Double-Button-1>", lambda _e, i=r["iid"]: self.on_open(i))
            widget.bind("<Enter>", lambda _e, i=r["iid"]: self._hover(i, True))
            widget.bind("<Leave>", lambda _e, i=r["iid"]: self._hover(i, False))
        card.bind("<Configure>", lambda e, t=title: t.configure(wraplength=max(120, e.width - 110)))

    def _update_card(self, r):
        w = self.cards[r["iid"]]
        w["title"].configure(text=r["title"])
        w["info"].configure(text=r["info"])
        bg, fg = self.pal["pill"][CHIP.get(r["key"], "off")]
        w["chip"].configure(text=r["chip"], bg=bg, fg=fg)

    def _hover(self, iid, on):
        if iid in self.cards:
            self.cards[iid]["hover"] = on
            self._paint(iid)

    def _paint(self, iid):
        w, pal = self.cards[iid], self.pal
        sel = iid == self.selected
        bg = pal["select"] if sel else (pal["hover"] if w["hover"] else pal["bg"])
        for key in ("card", "body", "title", "info"):
            w[key].configure(bg=bg)
        w["title"].configure(fg=pal["select_fg"] if sel else pal["text"])
        w["info"].configure(fg=pal["select_fg"] if sel else pal["muted"])
        w["bar"].configure(bg=pal["accent"] if sel else bg)

    def _paint_all(self):
        for iid in self.cards:
            self._paint(iid)

    def select(self, iid, notify=False):
        if iid not in self.cards:
            return
        old, self.selected = self.selected, iid
        if old in self.cards:
            self._paint(old)
        self._paint(iid)
        self.see(iid)
        if notify:
            self.on_select(iid)

    def see(self, iid):
        try:
            self.update_idletasks()
            card = self.cards[iid]["card"]
            total = max(1, self.inner.winfo_height())
            top, h = card.winfo_y(), card.winfo_height()
            view0, view1 = self.canvas.yview()
            if top / total < view0 or (top + h) / total > view1:
                self.canvas.yview_moveto(max(0.0, (top - 40) / total))
        except (tk.TclError, KeyError):
            pass


def app_icon(size=64):
    """Значок программы как PhotoImage (или None без Pillow)."""
    try:
        from PIL import ImageTk
        import tray
        return ImageTk.PhotoImage(tray.make_app_icon(size))
    except Exception:
        return None


def is_windows():
    return sys.platform.startswith("win")
