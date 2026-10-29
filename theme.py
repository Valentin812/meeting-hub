# -*- coding: utf-8 -*-
"""Оформление: несколько готовых тем. Все строятся на одной основе (ttk «clam») и палитре,
поэтому цвета, шрифты и рамки полностью наши и одинаково работают на любой Windows.

Темы: светлая и тёмная (мягкие, без белого и без чёрного), латте, капучино (кофейная),
пиксель-арт, Windows 95. «Как в Windows» выбирает светлую или тёмную по настройке системы.
"""
import os

FONT = "Segoe UI"            # меняется вместе с темой (apply)
MONO = "Consolas"


def _pills(ok, rec, busy, meet, off):
    return {"ok": ok, "rec": rec, "busy": busy, "meet": meet, "off": off}


THEMES = {
    "light": dict(
        label="Светлая", dark=False, font="Segoe UI", relief="flat",
        bg="#e8ecf2", surface="#f2f5f9", text="#18212f", muted="#566275", faint="#8a94a6",
        border="#cfd7e3", accent="#3b5bdb", accent_fg="#ffffff", accent_hover="#2f49b5",
        select="#d9e2fa", select_fg="#18212f", hover="#dde3ec", card="#f2f5f9", btn="#f2f5f9",
        me="#3b5bdb", they="#9c36b5",
        pill=_pills(("#dcf5e3", "#17653a"), ("#fde2e1", "#b42318"), ("#fdf0c9", "#7a5200"),
                    ("#e1e8fd", "#2b45a8"), ("#e9edf3", "#4a5568"))),
    "dark": dict(
        label="Тёмная", dark=True, font="Segoe UI", relief="flat",
        bg="#282d38", surface="#323846", text="#e8ebf1", muted="#a9b2c1", faint="#7b8596",
        border="#444c5c", accent="#8097ff", accent_fg="#0e1430", accent_hover="#9fb0ff",
        select="#323f6b", select_fg="#ffffff", hover="#3a4150", card="#323846", btn="#3a4150",
        me="#8fb4ff", they="#d8b4fe",
        pill=_pills(("#25472f", "#a6e3b4"), ("#582f33", "#ffb4b0"), ("#51432a", "#f5d98a"),
                    ("#2d3f6a", "#b4c8ff"), ("#3a4150", "#d0d6df"))),
    "latte": dict(
        label="Латте", dark=False, font="Segoe UI", relief="flat",
        bg="#efe3d3", surface="#f8f0e5", text="#3e2c22", muted="#6f5849", faint="#a28f80",
        border="#d6c2a8", accent="#9c5f34", accent_fg="#ffffff", accent_hover="#834d27",
        select="#e7d2b8", select_fg="#3e2c22", hover="#e6d6c2", card="#f8f0e5", btn="#f8f0e5",
        me="#8a4b1f", they="#2f6b6b",
        pill=_pills(("#dbe8cd", "#3d6b2a"), ("#f1cfc6", "#a3321f"), ("#f3e0b0", "#74540c"),
                    ("#e7d2b8", "#7a4a24"), ("#e5d9c9", "#6d5a4a"))),
    "cappuccino": dict(
        label="Капучино", dark=True, font="Segoe UI", relief="flat",
        bg="#45342a", surface="#54402f", text="#f4e8d8", muted="#dccab5", faint="#a89480",
        border="#6e5948", accent="#e0a96d", accent_fg="#33231a", accent_hover="#efc08b",
        select="#6a5241", select_fg="#fff6ea", hover="#5a4637", card="#4f3c2d", btn="#614a39",
        me="#ffd29a", they="#b9e0d0",
        pill=_pills(("#58673f", "#e6f4cc"), ("#86463d", "#ffdcd2"), ("#876a31", "#ffecb8"),
                    ("#80614a", "#ffe8cb"), ("#6a5646", "#e8d8c4"))),
    "mac": dict(
        label="Mac (светлая)", dark=False, font="Segoe UI", relief="flat",
        bg="#f6f6f8", surface="#ffffff", text="#1d1d1f", muted="#5c5c62", faint="#8e8e93",
        sidebar="#dcdde3", border="#c9c9d0", accent="#0a64d0", accent_fg="#ffffff", accent_hover="#0852aa",
        select="#0a64d0", select_fg="#ffffff", hover="#cfd0d8", card="#ffffff", btn="#ffffff",
        me="#0a64d0", they="#8944ab",
        pill=_pills(("#d4ecd6", "#1e6b2c"), ("#f8d9d6", "#b3261e"), ("#f6e6b8", "#6e5200"),
                    ("#d3e3f8", "#0a5cc0"), ("#dcdce2", "#4b4b52"))),
    "mac_dark": dict(
        label="Mac (тёмная)", dark=True, font="Segoe UI", relief="flat",
        bg="#262629", surface="#303034", text="#f2f2f7", muted="#b0b0b8", faint="#85858d",
        sidebar="#1c1c1f", border="#46464c", accent="#0a6fe0", accent_fg="#ffffff", accent_hover="#3b8df0",
        select="#0a6fe0", select_fg="#ffffff", hover="#2c2c30", card="#303034", btn="#3d3d42",
        me="#6cb4ff", they="#d4a8f5",
        pill=_pills(("#2f4d35", "#a6e3ae"), ("#5c3639", "#ffb3ae"), ("#56482c", "#f5da8e"),
                    ("#2f4968", "#aad2ff"), ("#45454c", "#d0d0d8"))),
    "pixel": dict(
        label="Пиксель-арт", dark=True, font="Courier New", relief="pixel", bold=True,
        bg="#1a1c2c", surface="#262b44", text="#f4f4f4", muted="#94b0c2", faint="#566c86",
        border="#566c86", accent="#ffcd75", accent_fg="#1a1c2c", accent_hover="#ffe0a3",
        select="#3b5dc9", select_fg="#f4f4f4", hover="#333c57", card="#262b44", btn="#333c57",
        me="#73eff7", they="#ffcd75",
        pill=_pills(("#257179", "#a7f070"), ("#b13e53", "#f4f4f4"), ("#ef7d57", "#1a1c2c"),
                    ("#3b5dc9", "#f4f4f4"), ("#333c57", "#94b0c2"))),
    "win95": dict(
        label="Windows 95", dark=False, font="Tahoma", relief="win95",
        bg="#c0c0c0", surface="#ffffff", text="#000000", muted="#404040", faint="#808080",
        border="#808080", accent="#000080", accent_fg="#ffffff", accent_hover="#1010a0",
        select="#000080", select_fg="#ffffff", hover="#d4d0c8", card="#ffffff", btn="#c0c0c0",
        me="#000080", they="#800080",
        pill=_pills(("#008000", "#ffffff"), ("#c00000", "#ffffff"), ("#ffff00", "#000000"),
                    ("#000080", "#ffffff"), ("#707070", "#ffffff"))),
}
ORDER = ["light", "dark", "latte", "cappuccino", "mac", "mac_dark", "pixel", "win95"]


def windows_dark():
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def resolve(mode):
    if mode == "system" or mode not in THEMES:
        return "dark" if windows_dark() else "light"
    return mode


def _configure_ttk(st, pal):
    f = pal["font"]
    bold = "bold" if pal.get("bold") else "normal"
    bg, sf, tx, br = pal["bg"], pal["surface"], pal["text"], pal["border"]
    kind = pal["relief"]
    st.theme_use("clam")
    if kind == "win95":
        light, dark3, thick, brd = "#ffffff", "#808080", 2, "#000000"
    elif kind == "pixel":
        light, dark3, thick, brd = br, br, 2, br
    else:
        light, dark3, thick, brd = br, br, 1, br
    st.configure(".", background=bg, foreground=tx, fieldbackground=sf, bordercolor=brd, lightcolor=light,
                 darkcolor=dark3, troughcolor=pal["hover"], focuscolor=bg, selectbackground=pal["select"],
                 selectforeground=pal["select_fg"], insertcolor=tx, font=(f, 10, bold))
    st.configure("TFrame", background=bg)
    side = pal.get("sidebar", bg)
    st.configure("TLabel", background=bg, foreground=tx)
    st.configure("Side.TFrame", background=side)
    st.configure("Side.Small.TLabel", background=side, foreground=pal["muted"], font=(f, 9, bold))
    st.configure("Title.TLabel", font=(f, 17 if kind == "flat" else 15, "bold"))
    st.configure("H2.TLabel", font=(f, 14 if kind == "flat" else 13, "bold"))
    st.configure("Muted.TLabel", foreground=pal["muted"])
    st.configure("Small.TLabel", foreground=pal["muted"], font=(f, 9, bold))
    st.configure("TSeparator", background=br)
    st.configure("TButton", background=pal["btn"], foreground=tx, bordercolor=brd, lightcolor=light,
                 darkcolor=dark3, borderwidth=thick, relief="raised" if kind == "win95" else "flat",
                 padding=(14, 8) if kind == "flat" else (12, 6), anchor="center", focuscolor=pal["btn"])
    st.map("TButton", background=[("pressed", pal["hover"]), ("active", pal["hover"]), ("disabled", bg)],
           foreground=[("disabled", pal["faint"])],
           relief=[("pressed", "sunken")] if kind == "win95" else [])
    st.configure("Accent.TButton", background=pal["accent"], foreground=pal["accent_fg"], bordercolor=pal["accent"]
                 if kind != "win95" else brd, lightcolor=pal["accent"] if kind != "win95" else light,
                 darkcolor=pal["accent"] if kind != "win95" else dark3, font=(f, 10, "bold"))
    st.map("Accent.TButton", background=[("pressed", pal["accent_hover"]), ("active", pal["accent_hover"]),
                                         ("disabled", pal["hover"])],
           foreground=[("disabled", pal["faint"])])
    st.configure("TMenubutton", background=pal["btn"], foreground=tx, bordercolor=brd, lightcolor=light,
                 darkcolor=dark3, borderwidth=thick, relief="raised" if kind == "win95" else "flat",
                 padding=(10, 6), arrowcolor=tx)
    st.map("TMenubutton", background=[("active", pal["hover"])])
    for name in ("TEntry", "TSpinbox", "TCombobox"):
        st.configure(name, fieldbackground=sf, foreground=tx, bordercolor=brd, lightcolor=sf if kind != "win95"
                     else dark3, darkcolor=sf if kind != "win95" else light, padding=6, insertcolor=tx,
                     arrowcolor=tx, borderwidth=thick, relief="sunken" if kind == "win95" else "flat")
        st.map(name, bordercolor=[("focus", pal["accent"])] if kind != "win95" else [],
               fieldbackground=[("readonly", sf), ("disabled", bg)],
               foreground=[("disabled", pal["faint"])])
    st.configure("TCheckbutton", background=bg, foreground=tx, indicatorcolor=sf, indicatorbackground=sf,
                 indicatorforeground=tx)
    st.map("TCheckbutton", background=[("active", bg)], indicatorcolor=[("selected", pal["accent"])],
           foreground=[("disabled", pal["faint"])])
    st.configure("TRadiobutton", background=bg, foreground=tx, indicatorcolor=sf)
    st.map("TRadiobutton", background=[("active", bg)], indicatorcolor=[("selected", pal["accent"])])
    st.configure("TNotebook", background=bg, bordercolor=br, lightcolor=bg, darkcolor=bg, tabmargins=(0, 4, 0, 0),
                 borderwidth=thick)
    st.configure("TNotebook.Tab", background=pal["hover"], foreground=pal["muted"], padding=(18, 9) if kind == "flat" else (16, 7),
                 bordercolor=br, lightcolor=pal["hover"], darkcolor=pal["hover"], borderwidth=thick)
    st.map("TNotebook.Tab", background=[("selected", sf), ("active", pal["select"])],
           foreground=[("selected", tx), ("active", pal["select_fg"])],
           lightcolor=[("selected", sf)], darkcolor=[("selected", sf)])
    st.configure("Treeview", background=sf, fieldbackground=sf, foreground=tx, bordercolor=br, rowheight=34 if kind == "flat" else 30,
                 font=(f, 10, bold), lightcolor=sf, darkcolor=sf, borderwidth=thick)
    st.configure("Meetings.Treeview", rowheight=30, font=(f, 10, bold))
    st.configure("Tasks.Treeview", rowheight=28)
    st.map("Treeview", background=[("selected", pal["select"])], foreground=[("selected", pal["select_fg"])])
    st.configure("Treeview.Heading", background=pal["hover"], foreground=tx, relief="flat", padding=(8, 6),
                 bordercolor=br, lightcolor=pal["hover"], darkcolor=pal["hover"], font=(f, 10, "bold"))
    st.map("Treeview.Heading", background=[("active", pal["select"])])
    st.configure("TScrollbar", background=pal["border"] if kind != "win95" else "#c0c0c0", troughcolor=pal["hover"]
                 if kind != "win95" else "#e0e0e0", bordercolor=bg, arrowcolor=tx, lightcolor=pal["border"]
                 if kind != "win95" else light, darkcolor=pal["border"] if kind != "win95" else dark3,
                 relief="flat" if kind != "win95" else "raised", gripcount=0, arrowsize=14)
    st.map("TScrollbar", background=[("active", pal["muted"])] if kind != "win95" else [])
    st.configure("TProgressbar", background=pal["accent"], troughcolor=pal["hover"], bordercolor=br,
                 lightcolor=pal["accent"], darkcolor=pal["accent"], thickness=12)
    st.configure("TScale", background=bg, troughcolor=pal["hover"], bordercolor=br, lightcolor=pal["accent"],
                 darkcolor=pal["accent"])
    st.configure("TPanedwindow", background=bg)
    st.configure("Sash", background=bg, sashthickness=6)
    st.configure("TLabelframe", background=bg, bordercolor=br)
    st.configure("TLabelframe.Label", background=bg, foreground=tx)
    # Совсем маленькая «растяжка» полосы прокрутки и стрелок — как в стиле темы


def apply(root, mode="system"):
    """Включить тему. mode: system | light | dark | latte | cappuccino | pixel | win95. Вернуть палитру."""
    global FONT
    name = resolve(mode)
    pal = dict(THEMES[name])
    pal["name"], pal["themed"] = name, True
    FONT = pal["font"]
    from tkinter import ttk
    st = ttk.Style(root)
    try:
        _configure_ttk(st, pal)
    except Exception:
        pal["themed"] = False
    else:
        try:
            import modern
            pal["modern"] = modern.install(root, st, pal)       # скруглённые элементы; при сбое — обычный вид
        except Exception:
            pal["modern"] = []
    try:
        root.configure(bg=pal["bg"])
        for opt, val in (("Menu.background", pal["surface"]), ("Menu.foreground", pal["text"]),
                         ("Menu.activeBackground", pal["select"]), ("Menu.activeForeground", pal["select_fg"]),
                         ("Menu.relief", "flat"), ("Menu.borderWidth", 1),
                         ("Toplevel.background", pal["bg"]), ("Listbox.background", pal["surface"]),
                         ("Listbox.foreground", pal["text"]), ("Text.background", pal["surface"]),
                         ("Text.foreground", pal["text"]), ("TCombobox*Listbox.background", pal["surface"]),
                         ("TCombobox*Listbox.foreground", pal["text"]),
                         ("TCombobox*Listbox.selectBackground", pal["select"]),
                         ("TCombobox*Listbox.selectForeground", pal["select_fg"])):
            root.option_add("*" + opt, val)
        recolor(root, pal)
    except Exception:
        pass
    return pal


def recolor(widget, pal):
    """Пройти по уже созданным обычным (не ttk) виджетам: меню и окна."""
    import tkinter as tk
    for w in widget.winfo_children():
        try:
            if isinstance(w, tk.Menu):
                w.configure(background=pal["surface"], foreground=pal["text"], activebackground=pal["select"],
                            activeforeground=pal["select_fg"], font=(pal["font"], 10))
            elif isinstance(w, tk.Toplevel):
                w.configure(bg=pal["bg"])
        except tk.TclError:
            pass
        recolor(w, pal)


def text_style(widget, pal, size=10):
    """tk.Text под тему: без рамки, с отступами, цветами палитры и тегами разметки."""
    f = pal["font"]
    b = "bold"
    widget.configure(bg=pal["surface"], fg=pal["text"], relief="flat", borderwidth=0, highlightthickness=0,
                     padx=14, pady=10, wrap="word", font=(f, size), insertbackground=pal["text"], spacing1=2,
                     spacing3=4, selectbackground=pal["select"], selectforeground=pal["select_fg"])
    widget.tag_configure("h", font=(f, size + 2, b), spacing1=12, spacing3=4)
    widget.tag_configure("muted", foreground=pal["muted"])
    widget.tag_configure("time", foreground=pal["faint"], font=(f, size - 1))
    widget.tag_configure("me", foreground=pal["me"], font=(f, size, b))
    widget.tag_configure("they", foreground=pal["they"], font=(f, size, b))
    widget.tag_configure("bullet", lmargin1=8, lmargin2=24)
    widget.tag_configure("note", foreground=pal["muted"], font=(f, size - 1, "italic"), spacing1=14)
    for name, (bg, fg) in pal["pill"].items():
        widget.tag_configure("chip_" + name, background=bg, foreground=fg, font=(f, size - 1, b))
    widget.tag_configure("chips", spacing1=4, spacing3=6)
    widget.tag_configure("now", background=pal["select"], foreground=pal["select_fg"])
    widget.tag_configure("found", background="#ffe27a", foreground="#1b1b1b")
    widget.tag_configure("lead", font=(f, size + 1), spacing3=6, lmargin1=8, lmargin2=8)
