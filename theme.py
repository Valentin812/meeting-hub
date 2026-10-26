# -*- coding: utf-8 -*-
"""Оформление: тема Sun Valley (как Windows 11), светлая или тёмная — как в системе.

Без sv_ttk программа выглядит по-старому, но работает.
"""
import os

LIGHT = {
    "bg": "#fafafa", "surface": "#ffffff", "text": "#1c1c1c", "muted": "#616161", "faint": "#8a8a8a",
    "border": "#e5e5e5", "accent": "#005fb8", "select": "#e5f0fb", "hover": "#f0f0f0", "card": "#ffffff",
    "me": "#005fb8", "they": "#7a3db8",
    "pill": {"ok": ("#dff6dd", "#0e700e"), "rec": ("#fde7e9", "#c42b1c"), "busy": ("#fff4ce", "#7a5a00"),
             "meet": ("#e8f1fb", "#005fb8"), "off": ("#ededed", "#5c5c5c")},
}
DARK = {
    "bg": "#1c1c1c", "surface": "#232323", "text": "#f0f0f0", "muted": "#ababab", "faint": "#8a8a8a",
    "border": "#3a3a3a", "accent": "#60cdff", "select": "#2b3946", "hover": "#2d2d2d", "card": "#272727",
    "me": "#60cdff", "they": "#d0a8ff",
    "pill": {"ok": ("#1f3a1f", "#9de09a"), "rec": ("#442726", "#ff9a8f"), "busy": ("#433519", "#fcd571"),
             "meet": ("#1f3346", "#9ccfff"), "off": ("#333333", "#c8c8c8")},
}
FONT = "Segoe UI"


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


def apply(root, mode="system"):
    """Включить тему. mode: system | light | dark. Вернуть палитру."""
    dark = windows_dark() if mode == "system" else mode == "dark"
    pal = dict(DARK if dark else LIGHT)
    pal["dark"] = dark
    pal["themed"] = False
    try:
        import sv_ttk
        sv_ttk.set_theme("dark" if dark else "light", root)
        pal["themed"] = True
    except Exception:
        pal = dict(LIGHT, dark=False, themed=False)
    from tkinter import ttk
    st = ttk.Style(root)
    pal["bg"] = st.lookup("TFrame", "background") or pal["bg"]
    st.configure("Title.TLabel", font=(FONT, 15, "bold"))
    st.configure("H2.TLabel", font=(FONT, 13, "bold"))
    st.configure("Muted.TLabel", foreground=pal["muted"])
    st.configure("Small.TLabel", foreground=pal["muted"], font=(FONT, 9))
    st.configure("Meetings.Treeview", rowheight=30, font=(FONT, 10))
    st.configure("Tasks.Treeview", rowheight=28)
    if not pal["themed"]:
        st.configure("Accent.TButton", font=(FONT, 10, "bold"))
    return pal


def text_style(widget, pal, size=10):
    """tk.Text под тему: без рамки, с отступами, цветами палитры и тегами разметки."""
    widget.configure(bg=pal["surface"] if pal["themed"] else "#ffffff", fg=pal["text"], relief="flat",
                     borderwidth=0, highlightthickness=0, padx=14, pady=10, wrap="word",
                     font=(FONT, size), insertbackground=pal["text"], spacing1=2, spacing3=4)
    widget.tag_configure("h", font=(FONT, size + 2, "bold"), spacing1=12, spacing3=4)
    widget.tag_configure("muted", foreground=pal["muted"])
    widget.tag_configure("time", foreground=pal["faint"], font=(FONT, size - 1))
    widget.tag_configure("me", foreground=pal["me"], font=(FONT, size, "bold"))
    widget.tag_configure("they", foreground=pal["they"], font=(FONT, size, "bold"))
    widget.tag_configure("bullet", lmargin1=8, lmargin2=24)
    widget.tag_configure("note", foreground=pal["muted"], font=(FONT, size - 1, "italic"), spacing1=14)
    for name, (bg, fg) in pal["pill"].items():
        widget.tag_configure("chip_" + name, background=bg, foreground=fg, font=(FONT, size - 1, "bold"))
    widget.tag_configure("chips", spacing1=4, spacing3=6)
    widget.tag_configure("now", background=pal["select"])
    widget.tag_configure("lead", font=(FONT, size + 1), spacing3=6, lmargin1=8, lmargin2=8)
