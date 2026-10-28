# -*- coding: utf-8 -*-
"""Главное окно Meeting Hub v7: список встреч слева, выбранная встреча справа, одна главная кнопка.

Всё техническое — в окне «Настройки». Логика (обнаружение, запись, расшифровка, итоги) живёт в app.py,
здесь только то, что видит пользователь.
"""
import csv
import json
import os
import re
import threading
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

import daily
import gigachat
import recorder as rec_mod
import importer
import report as report_mod
import theme
import transcriber as tr_mod
import widgets
from ui_voices import VoicesUI
from ui_chat import ChatUI
from ui_ai import AiSetupUI
import player as player_mod
from meeting_detector import ASKING, COUNTDOWN, ENDING, IN_MEETING

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]
SEARCH_HINT = "Поиск: название, слова из разговора…"


STAMP_RE = re.compile(r"(20\d\d)[-_.]?(\d\d)[-_.]?(\d\d)(?:[ _T.-]?(\d\d)[-_:.]?(\d\d))?")


def parse_stamp(stem):
    """(дата, «чч:мм» или "") из имени записи: 20261026_152923, 2026-10-08_06-16 и похожих."""
    m = STAMP_RE.search(stem or "")
    if not m:
        return None, ""
    try:
        d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
    except ValueError:
        return None, ""
    hm = ""
    if m.group(4) and int(m.group(4)) < 24 and int(m.group(5)) < 60:
        hm = f"{m.group(4)}:{m.group(5)}"
    return d, hm


def day_label(stem, today=None):
    d, _hm = parse_stamp(stem)
    if d is None:
        return "Без даты"
    today = today or datetime.now().date()
    if d == today:
        return "Сегодня"
    if d == today - timedelta(days=1):
        return "Вчера"
    return f"{d.day} {MONTHS[d.month - 1]}" + ("" if d.year == today.year else f" {d.year}")


def clock(stem):
    return parse_stamp(stem)[1]


def dur_text(sec):
    sec = int(sec or 0)
    if sec >= 3600:
        return f"{sec // 3600} ч {sec % 3600 // 60} мин"
    if sec >= 60:
        return f"{sec // 60} мин"
    return f"{sec} с"


def meeting_title(item):
    title = (item.get("title") or item.get("window") or item.get("meeting") or "").strip()
    return title or "Запись без названия"


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class MainWindow(VoicesUI, ChatUI, AiSetupUI):
    """Примесь к App: главное окно, карточка встречи и настройки."""

    # ------------------------------------------------------------------
    # Построение
    # ------------------------------------------------------------------
    def build_main_ui(self):
        root, pal = self.root, self.pal
        root.minsize(900, 560)
        w = min(1180, root.winfo_screenwidth() - 60)
        h = min(760, root.winfo_screenheight() - 90)
        root.geometry(f"{w}x{h}+{max(0, (root.winfo_screenwidth() - w) // 2)}+20")

        self._icons = {"logo": widgets.app_icon(30), "window": widgets.app_icon(64)}
        if self._icons["window"] is not None:
            try:
                root.iconphoto(True, self._icons["window"])
            except tk.TclError:
                pass
        header = ttk.Frame(root, padding=(18, 14, 18, 4))
        header.pack(fill="x")
        if self._icons["logo"] is not None:
            ttk.Label(header, image=self._icons["logo"]).pack(side="left", padx=(0, 10))
        ttk.Label(header, text="Meeting Hub", style="Title.TLabel").pack(side="left")
        self.status = widgets.Pill(header, pal)
        self.status.pack(side="left", padx=(16, 0))
        self.work = widgets.Pill(header, pal, size=9)          # фоновая обработка — отдельно от встречи
        self.work.bind("<Button-1>", lambda _e: self.select_working())
        self.work.configure(cursor="hand2")
        ttk.Button(header, text="⚙", width=3, command=self.open_settings_dialog).pack(side="right")
        self.rec_btn = ttk.Button(header, text="●  Записать", style="Accent.TButton",
                                  command=self.toggle_recording_button)
        self.rec_btn.pack(side="right", padx=(0, 8))
        self.mute_btn = ttk.Button(header, text="", command=self.toggle_mic_mute)
        self.sysmute_btn = ttk.Button(header, text="", command=self.toggle_system_mute)
        self.update_btn = ttk.Button(header, text="", command=lambda: self.install_found_update())
        self._header = header
        self.substatus = ttk.Label(root, text="", style="Small.TLabel", padding=(18, 0, 18, 10))
        self.substatus.pack(fill="x")
        ttk.Separator(root).pack(fill="x")

        body = ttk.PanedWindow(root, orient="horizontal")
        body.pack(fill="both", expand=True)

        # Слева — встречи
        left = ttk.Frame(body, padding=(12, 12, 6, 12), width=400, style="Side.TFrame")
        body.add(left, weight=0)
        top = ttk.Frame(left, style="Side.TFrame")
        top.pack(fill="x", pady=(0, 8))
        self.add_btn = ttk.Button(top, text="＋ Файл", command=self.add_files)
        self.add_btn.pack(side="right", padx=(6, 0))
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(top, textvariable=self.search_var)
        self.search_entry.pack(side="left", fill="x", expand=True)
        self._search_placeholder(True)
        self.search_entry.bind("<FocusIn>", lambda _e: self._search_placeholder(False))
        self.search_entry.bind("<FocusOut>", lambda _e: self._search_placeholder(True))
        self._search_job = None
        self.search_var.trace_add("write", self._on_search)
        self.records_tree = widgets.MeetingList(left, pal, on_select=self.on_meeting_selected,
                                                on_open=lambda _i: self.open_protocol())
        self.records_tree.pack(fill="both", expand=True)
        self.list_hint = ttk.Label(left, text="", style="Side.Small.TLabel", wraplength=330, justify="left")
        self.list_hint.pack(fill="x", pady=(6, 0))

        # Справа — встреча
        right = ttk.Frame(body, padding=(16, 12, 18, 12))
        body.add(right, weight=1)
        self.detail = ttk.Frame(right)
        head = ttk.Frame(self.detail)
        head.pack(fill="x")
        self.d_title = ttk.Label(head, text="", style="H2.TLabel", justify="left")
        self.d_title.pack(anchor="w", fill="x")
        self.d_meta = ttk.Label(head, text="", style="Muted.TLabel", justify="left")
        self.d_meta.pack(anchor="w", fill="x", pady=(2, 0))
        self.d_desc = ttk.Label(head, text="", justify="left")
        self.d_desc.pack(anchor="w", fill="x", pady=(4, 0))
        btns = ttk.Frame(head)
        btns.pack(anchor="w", pady=(10, 0))
        self.proto_btn = ttk.Button(btns, text="Протокол (Word)", command=self.open_protocol)
        self.proto_btn.pack(side="left")
        self.edit_btn = ttk.Button(btns, text="✎ Название", command=lambda: self.edit_details(self.selected))
        self.edit_btn.pack(side="left", padx=(6, 0))
        self.more_btn = ttk.Menubutton(btns, text="Ещё", width=6)
        self.more_btn.pack(side="left", padx=(6, 0))
        self.more_menu = tk.Menu(self.more_btn, tearoff=False)
        self.more_btn["menu"] = self.more_menu

        # Проигрыватель
        self.pbar = ttk.Frame(head)
        self.pbar.pack(fill="x", pady=(10, 0))
        self.play_btn = ttk.Button(self.pbar, text="▶", width=3, style="Accent.TButton",
                                   command=self.play_selected_record)
        self.play_btn.pack(side="left")
        self.p_time = ttk.Label(self.pbar, text="0:00 / 0:00", style="Muted.TLabel", width=15, anchor="e")
        self.p_time.pack(side="right")
        self.p_scale = ttk.Scale(self.pbar, from_=0, to=1, orient="horizontal")
        self.p_scale.pack(side="left", fill="x", expand=True, padx=10)
        self.p_scale.bind("<ButtonPress-1>", lambda _e: setattr(self, "_p_drag", True))
        self.p_scale.bind("<ButtonRelease-1>", self._player_seek_release)
        self.p_scale.configure(command=self._player_scale_moved)
        self.p_note = ttk.Label(self.pbar, text="", style="Small.TLabel")
        self.player = None
        self._p_drag = False
        self._p_job = None
        self._segments = []

        def rewrap(event):
            for lbl in (self.d_title, self.d_meta, self.d_desc):
                lbl.configure(wraplength=max(200, event.width - 8))
        head.bind("<Configure>", rewrap)

        self.action_bar = tk.Frame(self.detail, bg=pal["select"], padx=14, pady=10)
        self.action_text = tk.Label(self.action_bar, text="", bg=pal["select"], fg=pal["select_fg"],
                                    font=(theme.FONT, 10), anchor="w", justify="left")
        self.action_text.pack(side="left", fill="x", expand=True)
        self.action_btn = ttk.Button(self.action_bar, text="", style="Accent.TButton")
        self.action_btn.pack(side="right")

        self.detail_nb = ttk.Notebook(self.detail)
        self.detail_nb.pack(fill="both", expand=True, pady=(12, 0))
        self.sum_text = self._text_tab(self.detail_nb, "Итоги")
        tasks = ttk.Frame(self.detail_nb, padding=(0, 8, 0, 0))
        self.detail_nb.add(tasks, text="Задачи")
        self.task_tree = ttk.Treeview(tasks, columns=("task", "owner", "deadline"), show="headings",
                                      style="Tasks.Treeview")
        for col, label, width in (("task", "Задача", 460), ("owner", "Кто", 150), ("deadline", "Срок", 110)):
            self.task_tree.heading(col, text=label)
            self.task_tree.column(col, width=width, anchor="w", stretch=col == "task")
        self.task_tree.pack(fill="both", expand=True)
        self.tasks_tab = tasks
        self.tr_text = self._text_tab(self.detail_nb, "Расшифровка")
        self.build_voices_tab(self.detail_nb)
        self.build_chat_tab(self.detail_nb)

        self.empty = ttk.Frame(right, padding=(10, 60))
        big = widgets.app_icon(72)
        if big is not None:
            self._icons["big"] = big
            ttk.Label(self.empty, image=big).pack(anchor="w", pady=(0, 16))
        ttk.Label(self.empty, text="Здесь появятся ваши встречи", style="H2.TLabel").pack(anchor="w")
        ttk.Label(self.empty, style="Muted.TLabel", wraplength=560, justify="left", text=(
            "Meeting Hub сам замечает онлайн-встречу в Chrome, Zoom, Teams, Телемосте и других, записывает её, "
            "расшифровывает на этом компьютере и готовит итоги: резюме, решения, задачи и протокол.\n\n"
            "Ничего нажимать не нужно. Если встреча не началась сама — кнопка «Записать» вверху.")
                  ).pack(anchor="w", pady=(8, 0))
        self.selected = None
        self.refresh_records()

    def _text_tab(self, nb, title):
        frame = ttk.Frame(nb, padding=(0, 8, 0, 0))
        nb.add(frame, text=title)
        text = tk.Text(frame)
        theme.text_style(text, self.pal)
        sb = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=sb.set, state="disabled")
        text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return text

    # ------------------------------------------------------------------
    # Поиск
    # ------------------------------------------------------------------
    def _search_placeholder(self, show):
        if show and not self.search_var.get():
            self._placeholder = True
            self._entry_style("Placeholder.TEntry")
            self.search_var.set(SEARCH_HINT)
        elif not show and getattr(self, "_placeholder", False):
            self._placeholder = False
            self.search_var.set("")
            self._entry_style("TEntry")

    def _entry_style(self, style):
        try:
            if style != "TEntry":
                st = ttk.Style(self.root)
                st.configure(style, foreground=self.pal["faint"])
                st.map(style, foreground=[("!disabled", self.pal["faint"])])
            self.search_entry.configure(style=style)
        except tk.TclError:
            pass

    def show_update_button(self, version):
        self.update_btn.config(text=f"⬆  Обновить до {version}")
        self.update_btn.pack(side="right", padx=(0, 8), before=self.rec_btn)

    def hide_update_button(self):
        self.update_btn.pack_forget()

    def search_query(self):
        return "" if getattr(self, "_placeholder", False) else self.search_var.get().strip()

    def _on_search(self, *_):
        if getattr(self, "_placeholder", False):
            return
        if self._search_job is not None:
            self.root.after_cancel(self._search_job)
        self._search_job = self.root.after(300, self.refresh_records)

    # ------------------------------------------------------------------
    # Список встреч
    # ------------------------------------------------------------------
    SHORT = {"done": "итоги", "text": "текст", "audio": "аудио", "error": "ошибка"}

    @staticmethod
    def busy_chip(state):
        if state.endswith("%"):
            return state.split()[-1]
        return {"готовлю итоги": "итоги…", "итоги в очереди": "очередь", "в очереди": "очередь",
                "ждёт конца встречи": "пауза"}.get(state, "модель" if state.startswith("модель") else "в работе")

    def meeting_state(self, item):
        """(текст состояния, ключ состояния)."""
        wav = item["path"]
        st = getattr(self, "tq", None) and self.tq.status.get(str(wav))
        s = st["state"] if st else ""
        if s == "running":
            return f"расшифровка {int(st['progress'] * 100)}%", "busy"
        if s == "downloading":
            return f"модель {int(st.get('downloaded_mb') or 0)} МБ", "busy"
        if s in ("queued", "loading"):
            return "в очереди", "busy"
        if s == "paused":
            return "ждёт конца встречи", "busy"
        if self.analysis_source is not None and str(self.analysis_source) == str(wav):
            return "готовлю итоги", "busy"
        if any(str(w) == str(wav) for w, _a in self.summary_jobs):
            return "итоги в очереди", "busy"
        if s == "error":
            return "ошибка", "error"
        if self.summary_path(wav).exists():
            return "итоги готовы", "done"
        if tr_mod.has_transcript(wav):
            return "есть расшифровка", "text"
        return "только запись", "audio"

    def refresh_records(self):
        if not hasattr(self, "records_tree"):
            return
        self._search_job = None
        query = self.search_query()
        hits = daily.search_hits(self.rec_folder, query) if query else None
        items = [i for i in rec_mod.list_recordings(self.rec_folder)
                 if hits is None or i["path"].stem in hits]
        rows = []
        for it in items:
            stem = it["path"].stem
            state, key = self.meeting_state(it)
            chip = self.SHORT.get(key) or self.busy_chip(state)
            info = " · ".join(x for x in (clock(stem), dur_text(it.get("duration_sec"))) if x)
            if hits is not None and hits.get(stem, {}).get("snippet"):
                info = hits[stem]["snippet"]                 # где именно нашлось
            rows.append({"day": day_label(stem), "iid": str(it["path"]), "title": meeting_title(it),
                         "info": info, "chip": chip, "key": key})
        lst = self.records_tree
        lst.set_rows(rows)
        if hasattr(self, "tr_text"):
            self._highlight_search()
        sel = str(self.selected) if self.selected is not None else None
        if sel is None or not lst.exists(sel):
            last = str(self.rec_folder / (str(self.settings.get("last_selected", "")) + ".wav"))
            sel = last if lst.exists(last) else (rows[0]["iid"] if rows else None)
            self.selected = Path(sel) if sel else None
        if sel:
            lst.select(sel)
        if query:
            self.list_hint.config(text=f"Найдено встреч: {len(rows)}" if rows else "Ничего не найдено")
        else:
            self.list_hint.config(text="" if rows else "Встреч пока нет")
        self.show_meeting(self.selected)

    def meeting_rows(self):
        return self.records_tree.rows()

    def on_meeting_selected(self, iid=None):
        iid = iid or self.records_tree.selected
        if not iid:
            return
        self.selected = Path(iid)
        if self.settings.get("last_selected") != self.selected.stem:
            self.settings["last_selected"] = self.selected.stem
            self.save_settings()
        self.show_meeting(self.selected, force=True)

    # ------------------------------------------------------------------
    # Карточка встречи
    # ------------------------------------------------------------------
    def show_meeting(self, wav, force=False):
        if wav is None:
            self.detail.pack_forget()
            self.empty.pack(fill="both", expand=True)
            self._shown = None
            return
        self.empty.pack_forget()
        if not self.detail.winfo_ismapped():
            self.detail.pack(fill="both", expand=True)
        wav = Path(wav)
        meta = read_json(wav.with_suffix(".json")) or {}
        item = {"path": wav, **meta}
        state, key = self.meeting_state(item)
        summary_mtime = self._mtime(self.summary_path(wav))
        tr_mtime = self._mtime(tr_mod.output_paths(wav)[1])
        signature = (str(wav), state, summary_mtime, tr_mtime)
        if not force and signature == getattr(self, "_shown", None):
            return
        self._shown = signature
        stem = wav.stem
        self.d_title.config(text=meeting_title(item))
        when = f"{day_label(stem)}, {clock(stem)}" if clock(stem) else stem
        parts = [when, dur_text(meta.get("duration_sec"))]
        if meta.get("meeting") and meta.get("window") and meta["meeting"] not in meta["window"]:
            parts.append(meta["meeting"])
        if meta.get("audio_deleted") or not wav.exists():
            parts.append("аудио удалено")
        self.d_meta.config(text=" · ".join(parts))
        desc = (meta.get("description") or "").strip()
        self.d_desc.config(text=desc)
        if desc:
            self.d_desc.pack(anchor="w", fill="x", pady=(4, 0))
        else:
            self.d_desc.pack_forget()
        self._player_show(wav, meta)
        self.proto_btn.state(["!disabled"] if tr_mod.has_transcript(wav) or self.summary_path(wav).exists()
                             else ["disabled"])
        self._fill_more_menu(wav)
        self._fill_action(wav, state, key)
        result = read_json(self.summary_path(wav))
        self._render_summary(result, wav, key)
        self._render_tasks(result)
        self._render_transcript(wav)
        self._highlight_search()
        self.render_voices(wav)
        self.render_chat()

    def _highlight_search(self):
        """Подсветить слова запроса в итогах и расшифровке открытой встречи; расшифровка прокручивается к первому."""
        query = self.search_query()
        for text in (self.sum_text, self.tr_text):
            text.tag_remove("found", "1.0", "end")
        if not query:
            return
        first = None
        for text in (self.sum_text, self.tr_text):
            text.configure(state="normal")
            for pat in daily._patterns(query):
                idx = "1.0"
                while True:
                    idx = text.search(pat, idx, stopindex="end", nocase=True)
                    if not idx:
                        break
                    end = text.index(f"{idx}+{len(pat)}c wordend")
                    text.tag_add("found", idx, end)
                    if text is self.tr_text and (first is None or text.compare(idx, "<", first)):
                        first = idx
                    idx = end
            text.tag_raise("found")
            text.configure(state="disabled")
        if first:
            self.tr_text.see(first)

    @staticmethod
    def _mtime(path):
        try:
            return Path(path).stat().st_mtime
        except OSError:
            return 0

    def _fill_action(self, wav, state, key):
        cmd, label, text = None, "", ""
        tq = getattr(self, "tq", None)
        st = tq.status.get(str(wav)) if tq else None
        if key == "busy" and st and st["state"] in ("queued", "loading", "downloading", "running", "paused"):
            text = {"queued": "Расшифровка в очереди", "loading": "Загружаю модель распознавания…",
                    "paused": "Расшифровка продолжится после встречи"}.get(st["state"], "")
            if st["state"] == "running":
                text = f"Расшифровываю: {int(st['progress'] * 100)}%"
            if st["state"] == "downloading":
                total = st.get("total_mb") or tr_mod.MODEL_MB.get(self.settings.get("whisper_model", tr_mod.DEFAULT_MODEL), 0)
                text = f"Скачиваю модель распознавания: {int(st.get('downloaded_mb') or 0)} из {total} МБ (один раз)"
            cmd, label = (lambda: self.cancel_transcription(wav)), "Отменить"
        elif key == "busy":
            text = "Готовлю итоги встречи…" if state == "готовлю итоги" else "Итоги подготовятся после текущих"
        elif key == "error":
            text = "Расшифровка не удалась: " + ((st or {}).get("error") or "")[:120]
            cmd, label = (lambda: self.transcribe_files([wav])), "Повторить"
        elif key == "audio" and wav.exists():
            text = "Встреча ещё не расшифрована."
            cmd, label = (lambda: self.transcribe_files([wav])), "Расшифровать"
        elif key == "text":
            text = "Итогов ещё нет."
            cmd, label = (lambda: self.request_summary(wav, automatic=False)), "Подготовить итоги"
        if text:
            self.action_text.config(text=text)
            if cmd:
                self.action_btn.config(text=label, command=cmd)
                self.action_btn.pack(side="right")
            else:
                self.action_btn.pack_forget()
            if not self.action_bar.winfo_ismapped():
                self.action_bar.pack(fill="x", before=self.detail_nb, pady=(12, 0))
        else:
            self.action_bar.pack_forget()

    def _fill_more_menu(self, wav):
        m = self.more_menu
        m.delete(0, "end")
        has_text = tr_mod.has_transcript(wav)
        m.add_command(label="Изменить название и описание…", command=lambda: self.edit_details(wav))
        if has_text:
            m.add_command(label="Подготовить итоги заново", command=lambda: self.request_summary(wav, rerun=True))
        if wav.exists():
            m.add_command(label="Расшифровать заново", command=lambda: self.transcribe_files([wav], force=True))
        if has_text:
            m.add_command(label="Протокол в браузере", command=lambda: self.open_html_protocol(wav))
            m.add_command(label="Открыть расшифровку (TXT)", command=lambda: self.open_path(tr_mod.output_paths(wav)[0]))
        if self.summary_path(wav).exists():
            m.add_command(label="Сохранить задачи в CSV…", command=lambda: self.export_tasks(wav))
        if has_text and self.settings.get("kanban_token"):
            m.add_command(label="Отправить в Канбан", command=lambda: self.kanban_push(wav, manual=True))
        if wav.exists():
            m.add_command(label="Открыть аудио в другой программе", command=lambda: self.open_path(wav))
        m.add_command(label="Показать в папке", command=lambda: self.show_in_folder(wav))
        m.add_separator()
        m.add_command(label="Удалить встречу…", command=lambda: self.delete_meeting(wav))

    # ------------------------------------------------------------------
    # Содержимое вкладок
    # ------------------------------------------------------------------
    @staticmethod
    def _write(text, chunks):
        text.configure(state="normal")
        text.delete("1.0", "end")
        for chunk, tags in chunks:
            text.insert("end", chunk, tags)
        text.configure(state="disabled")
        text.yview_moveto(0)

    def _render_summary(self, result, wav, key):
        if not result:
            hint = {"busy": "Итоги появятся здесь, когда встреча будет обработана.",
                    "text": "Нажмите «Подготовить итоги» — резюме, решения и задачи появятся здесь.",
                    "audio": "Сначала встречу нужно расшифровать."}.get(key, "Итогов нет.")
            self._write(self.sum_text, [(hint, ("muted",))])
            self.detail_nb.tab(self.tasks_tab, text="Задачи")
            return
        out = []
        decisions = [d for d in result.get("decisions") or [] if isinstance(d, dict)]
        tasks = [t for t in result.get("tasks") or [] if isinstance(t, dict)]
        questions = [q for q in result.get("open_questions") or [] if isinstance(q, dict)]

        def plural(n, one, few, many):
            return one if n % 10 == 1 and n % 100 != 11 else few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many
        chips = [(f" {len(decisions)} {plural(len(decisions), 'решение', 'решения', 'решений')} ", "chip_ok"),
                 (f" {len(tasks)} {plural(len(tasks), 'задача', 'задачи', 'задач')} ", "chip_meet"),
                 (f" {len(questions)} {plural(len(questions), 'вопрос', 'вопроса', 'вопросов')} ", "chip_busy")]
        for i, (txt, tag) in enumerate(chips):
            out.append((txt, (tag, "chips")))
            out.append(("   " if i < len(chips) - 1 else "\n", ("chips",)))

        def section(title, lines):
            if not lines:
                return
            out.append((title + "\n", ("h",)))
            for line in lines:
                out.append(("•  " + line + "\n", ("bullet",)))
        section("Резюме", [str(x) for x in result.get("summary") or []] or ["Резюме не сформировано."])
        section("Решения", [str(d.get("text", "")) for d in result.get("decisions") or [] if isinstance(d, dict)])
        section("Открытые вопросы", [str(q.get("question", "")) for q in result.get("open_questions") or []
                                     if isinstance(q, dict)])
        section("Проверьте", [f"{u.get('text', '')} — {u.get('reason', '')}".strip(" —")
                              for u in result.get("uncertain_fragments") or [] if isinstance(u, dict)])
        out.append(("Итоги подготовлены ИИ по расшифровке речи — возможны ошибки, проверьте важное.", ("note",)))
        self._write(self.sum_text, out)

    def _render_tasks(self, result):
        tree = self.task_tree
        tree.delete(*tree.get_children())
        tasks = [t for t in (result or {}).get("tasks") or [] if isinstance(t, dict)]
        for i, t in enumerate(tasks):
            tree.insert("", "end", iid=str(i), values=(t.get("task", ""), t.get("owner") or "—",
                                                        t.get("deadline") or "—"))
        self.detail_nb.tab(self.tasks_tab, text=f"Задачи  {len(tasks)}" if tasks else "Задачи")

    def _render_transcript(self, wav):
        data = read_json(tr_mod.output_paths(wav)[1]) or {}
        segs = data.get("segments") or []
        text = self.tr_text
        for tag in text.tag_names():
            if tag.startswith("seg"):
                text.tag_delete(tag)
        self._segments = []
        if not segs:
            self._write(text, [("Расшифровки пока нет.", ("muted",))])
            return
        out = []
        for i, s in enumerate(segs):
            tag = f"seg{i}"
            me = s.get("speaker") == tr_mod.SPEAKERS[0]
            out.append((tr_mod.stamp(s.get("start", 0)) + "   ", ("time", tag)))
            out.append((s.get("speaker", "") + "\n", ("me" if me else "they", tag)))
            out.append((s.get("text", "") + "\n\n", (tag,)))
            self._segments.append((float(s.get("start", 0)), float(s.get("end", 0)), tag))
        self._write(text, out)
        if wav.exists():
            for start, _end, tag in self._segments:
                text.tag_bind(tag, "<Button-1>", lambda _e, t=start: self.play_from(t))
                text.tag_bind(tag, "<Enter>", lambda _e: text.configure(cursor="hand2"))
                text.tag_bind(tag, "<Leave>", lambda _e: text.configure(cursor=""))
            self._now_tag = None

    # ------------------------------------------------------------------
    # Проигрыватель
    # ------------------------------------------------------------------
    @staticmethod
    def _clock(sec):
        sec = int(sec)
        return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"

    def _player_show(self, wav, meta):
        p = self.player
        if p is not None and p.path is not None and p.path != wav:
            p.stop()
            p.path = None
        if not wav.exists():
            for w in (self.play_btn, self.p_scale, self.p_time):
                w.pack_forget()
            self.p_note.config(text="Аудио удалено по сроку хранения — текст и итоги сохранены.")
            self.p_note.pack(side="left")
            return
        self.p_note.pack_forget()
        if not self.play_btn.winfo_ismapped():
            self.play_btn.pack(side="left")
            self.p_time.pack(side="right")
            self.p_scale.pack(side="left", fill="x", expand=True, padx=10)
        if p is None or p.path != wav:
            dur = float(meta.get("duration_sec") or 0)
            self.p_scale.configure(to=max(dur, 1))
            self.p_scale.set(0)
            self.p_time.config(text=f"0:00 / {self._clock(dur)}")
            self.play_btn.config(text="▶")

    def _ensure_player(self, wav):
        if self.player is None:
            self.player = player_mod.Player()
        if self.player.path != wav:
            self.player.load(wav)
            self.p_scale.configure(to=max(self.player.duration, 1))

    def play_selected_record(self):
        wav = self.current()
        if wav is None:
            return
        if not wav.exists():
            messagebox.showinfo("Встреча", "Аудио этой встречи удалено по сроку хранения; текст сохранён.",
                                parent=self.root)
            return
        try:
            self._ensure_player(wav)
            self.player.toggle()
        except Exception as e:
            self._player_failed(wav, e)
            return
        self._player_update()

    def play_from(self, sec):
        wav = self.selected
        if wav is None or not wav.exists():
            return
        try:
            self._ensure_player(wav)
            self.player.seek(max(0.0, sec - 0.3))
            if not self.player.playing:
                self.player.play()
        except Exception as e:
            self._player_failed(wav, e)
            return
        self._player_update()

    def _player_failed(self, wav, err):
        """Без перехода в другие программы: причину показываем прямо под проигрывателем."""
        self.log.warning("player failed for %s: %s", wav.name, err)
        self._journal_note("Прослушивание не удалось", str(err)[:300], "user")
        self.play_btn.config(text="▶")
        self.p_note.config(text="Звук не воспроизводится: " + str(err)[:160]
                           + ". Проверьте динамики; если стоит Kaspersky — добавьте Meeting Hub в доверенные.")
        self.last_event_text = "Звук не воспроизводится — подробности в журнале"
        self.refresh_status()
        messagebox.showwarning("Прослушивание", "Не удалось воспроизвести запись.\n\n" + str(err)
                               + "\n\nПроверьте, что динамики или наушники подключены и выбраны в Windows. "
                               "Если на компьютере Kaspersky — добавьте Meeting Hub в доверенные программы.",
                               parent=self.root)

    def _player_scale_moved(self, value):
        if self._p_drag and self.player is not None:
            self.p_time.config(text=f"{self._clock(float(value))} / {self._clock(self.player.duration)}")

    def _player_seek_release(self, _e=None):
        self._p_drag = False
        wav = self.selected
        if wav is None or not wav.exists():
            return
        try:
            self._ensure_player(wav)
            self.player.seek(float(self.p_scale.get()))
        except Exception as e:
            self._player_failed(wav, e)
        self._player_update()

    def _player_update(self):
        if self._p_job is not None:
            self.root.after_cancel(self._p_job)
            self._p_job = None
        p = self.player
        if p is None:
            return
        playing = p.playing
        self.play_btn.config(text="❚❚" if playing else "▶")
        if not self._p_drag:
            self.p_scale.set(p.position)
            self.p_time.config(text=f"{self._clock(p.position)} / {self._clock(p.duration)}")
        self._highlight_segment(p.position if playing or p.position > 0 else None)
        if playing:
            self._p_job = self.root.after(200, self._player_update)

    def _highlight_segment(self, pos):
        text = self.tr_text
        now = None
        if pos is not None:
            for start, end, tag in self._segments:
                if start - 0.2 <= pos <= max(end, start + 0.5):
                    now = tag
                    break
        if now == getattr(self, "_now_tag", None):
            return
        text.tag_remove("now", "1.0", "end")
        if now is not None:
            ranges = text.tag_ranges(now)
            if ranges:
                text.tag_add("now", ranges[0], ranges[-1])
                text.see(ranges[0])
        self._now_tag = now

    # ------------------------------------------------------------------
    # Действия со встречей
    # ------------------------------------------------------------------
    def current(self):
        if self.selected is None:
            messagebox.showinfo("Встречи", "Сначала выберите встречу в списке.", parent=self.root)
        return self.selected

    def open_protocol(self):
        wav = self.current()
        if wav is None:
            return
        path = report_mod.docx_path(wav) if report_mod.docx_available() else report_mod.protocol_paths(wav)[0]
        if not path.exists() or self._mtime(path) < max(self._mtime(self.summary_path(wav)),
                                                         self._mtime(tr_mod.output_paths(wav)[1])):
            path = self.make_protocol(wav)
        if path is None:
            messagebox.showinfo("Протокол", "Протокол появится после расшифровки встречи.", parent=self.root)
            return
        self.open_path(path)

    def open_html_protocol(self, wav):
        path = report_mod.protocol_paths(wav)[0]
        if not path.exists():
            self.make_protocol(wav)
        if path.exists():
            self.open_path(path)

    def show_in_folder(self, wav):
        target = wav if wav.exists() else tr_mod.output_paths(wav)[0]
        try:
            import subprocess
            subprocess.Popen(["explorer", "/select,", str(target)])
        except OSError:
            self.open_path(self.rec_folder)

    def export_tasks(self, wav):
        result = read_json(self.summary_path(wav)) or {}
        p = filedialog.asksaveasfilename(parent=self.root, defaultextension=".csv",
                                         initialfile=f"Задачи {wav.stem}.csv", filetypes=[("CSV", "*.csv")])
        if not p:
            return
        with open(p, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Задача", "Ответственный", "Срок", "Основание"])
            for t in result.get("tasks") or []:
                if isinstance(t, dict):
                    w.writerow([t.get("task", ""), t.get("owner", ""), t.get("deadline", ""), t.get("evidence", "")])
        self.last_event_text = "Задачи сохранены: " + Path(p).name
        self.refresh_status()

    def delete_meeting(self, wav):
        files = sorted(p for p in wav.parent.glob(wav.stem + ".*") if p.name.split(".")[0] == wav.stem)
        if not files:
            return
        if not messagebox.askyesno(
                "Удалить встречу",
                f"Удалить встречу «{self.d_title.cget('text')}» вместе с записью, расшифровкой и итогами?\n\n"
                f"Файлов: {len(files)}. Восстановить будет нельзя.", icon="warning", parent=self.root):
            return
        tq = getattr(self, "tq", None)
        if tq is not None and str(wav) == tq.current:
            tq.cancel(wav)
        failed = []
        for p in files:
            try:
                p.unlink()
            except OSError:
                failed.append(p.name)
        self._journal_note("Встреча удалена", wav.stem + (f"; не удалось: {', '.join(failed)}" if failed else ""),
                           "user")
        self.selected = None
        self.refresh_records()

    # ------------------------------------------------------------------
    # Свои файлы, название и описание
    # ------------------------------------------------------------------
    def add_files(self, paths=None):
        paths = list(paths or filedialog.askopenfilenames(
            parent=self.root, title="Добавить аудио или видео встречи", filetypes=importer.filetypes()))
        if not paths:
            return
        win = tk.Toplevel(self.root)
        win.title("Добавляю файл")
        win.transient(self.root)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=18)
        frm.pack(fill="both", expand=True)
        label = ttk.Label(frm, text="", width=56)
        label.pack(anchor="w")
        bar = ttk.Progressbar(frm, maximum=100, length=420)
        bar.pack(fill="x", pady=(10, 4))
        ttk.Label(frm, text="Из видео берётся только звук. Исходный файл не меняется.",
                  style="Small.TLabel").pack(anchor="w")
        stop = threading.Event()
        ttk.Button(frm, text="Отмена", command=stop.set).pack(anchor="e", pady=(10, 0))
        win.protocol("WM_DELETE_WINDOW", stop.set)
        results, errors = [], []

        def should_stop():
            if stop.is_set():
                raise importer.Cancelled()

        def ui(fn, *a):
            self._ui_calls.put((fn, a))

        def show(i, name, frac):
            try:
                label.config(text=f"Файл {i} из {len(paths)}: {name}")
                bar["value"] = int(frac * 100)
            except tk.TclError:
                pass

        def finish():
            try:
                win.destroy()
            except tk.TclError:
                pass
            for wav in results:
                self._journal_note("Файл добавлен", wav.name, "user", file=wav.name)
            if errors:
                messagebox.showwarning("Добавить файл", "\n".join(errors), parent=self.root)
            self.refresh_records()
            if results:
                self.records_tree.select(str(results[-1]), notify=True)
                if self.auto_transcribe.get():
                    self.transcribe_files(results)
                if len(results) == 1:
                    self.root.after(300, lambda: self.edit_details(results[0]))

        def worker():
            for i, src in enumerate(paths, 1):
                name = Path(src).name
                ui(show, i, name, 0.0)
                try:
                    wav = importer.import_file(src, self.rec_folder,
                                               progress=lambda f, i=i, n=name: ui(show, i, n, f),
                                               should_stop=should_stop)
                    results.append(wav)
                except importer.Cancelled:
                    break
                except importer.ImportFailed as e:
                    errors.append(f"{name}: {e}")
                except Exception as e:
                    self.log.exception("Файл не добавлен")
                    errors.append(f"{name}: {e}")
            ui(finish)

        threading.Thread(target=worker, daemon=True).start()

    def edit_details(self, wav):
        if not wav:
            return
        wav = Path(wav)
        meta = read_json(wav.with_suffix(".json")) or {}
        win = tk.Toplevel(self.root)
        win.title("Название и описание")
        win.transient(self.root)
        win.grab_set()
        frm = ttk.Frame(win, padding=18)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Название").pack(anchor="w")
        title = tk.StringVar(value=meeting_title({"path": wav, **meta}))
        entry = ttk.Entry(frm, textvariable=title, width=60)
        entry.pack(fill="x", pady=(2, 10))
        ttk.Label(frm, text="Описание (необязательно): о чём встреча, участники, проект").pack(anchor="w")
        desc = tk.Text(frm, width=60, height=5, wrap="word", font=(theme.FONT, 10))
        desc.pack(fill="x", pady=(2, 0))
        desc.insert("1.0", meta.get("description") or "")
        ttk.Label(frm, text="Описание попадёт в протокол и подскажет программе, о чём встреча.",
                  style="Small.TLabel").pack(anchor="w", pady=(4, 0))

        def save():
            fields = {"title": title.get().strip(), "description": desc.get("1.0", "end").strip()}
            if fields["title"] != meeting_title({"path": wav, **meta}):
                fields["title_by_user"], fields["title_auto"] = True, False        # своё название ИИ не трогает
            if fields["description"] != (meta.get("description") or "").strip():
                fields["description_by_user"], fields["description_auto"] = True, False
            win.destroy()
            importer.write_meta(wav, fields)
            for ext in (".protocol.html", ".protocol.docx"):
                if wav.with_name(wav.stem + ext).exists():
                    self.make_protocol(wav)
                    break
            self.refresh_records()
            self.show_meeting(wav, force=True)

        row = ttk.Frame(frm)
        row.pack(fill="x", pady=(14, 0))
        ttk.Button(row, text="Сохранить", style="Accent.TButton", command=save).pack(side="right")
        ttk.Button(row, text="Отмена", command=win.destroy).pack(side="right", padx=(0, 8))
        win.bind("<Escape>", lambda _e: win.destroy())
        entry.focus_set()
        entry.select_range(0, "end")

    # ------------------------------------------------------------------
    # Статус вверху
    # ------------------------------------------------------------------
    def status_view(self):
        """(текст, вид пилюли) — что сейчас происходит, одной фразой."""
        st = self.detector.state
        if self.recorder is not None:
            sec = int(self.recorder.elapsed())
            return f"●  Запись {sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}", "rec"
        if st == COUNTDOWN:
            return "Начинается встреча…", "meet"
        if st == ASKING:
            return "Это встреча?", "busy"
        if st == ENDING:
            return "Встреча закончилась?", "busy"
        if st == IN_MEETING:
            return "Идёт встреча", "meet"
        if not self.auto_detect.get():
            return "Автообнаружение выключено", "off"
        return "Жду встречу", "ok"

    def work_view(self):
        """Что обрабатывается в фоне: расшифровка и итоги — каждое своё, с очередью. "" — ничего."""
        parts = []
        if self._saving_threads:
            parts.append("сохраняю запись")
        tq = getattr(self, "tq", None)
        if tq is not None:
            states = [st for st in tq.status.values()]
            queued = sum(1 for st in states if st["state"] in ("queued", "loading"))
            cur = next((st for st in states if st["state"] in ("running", "downloading", "paused")), None)
            if cur is not None:
                if cur["state"] == "downloading":
                    text = f"скачиваю модель {int(cur.get('downloaded_mb') or 0)} МБ"
                elif cur["state"] == "paused":
                    text = "расшифровка на паузе"
                else:
                    text = f"расшифровка {int(cur['progress'] * 100)}%"
                parts.append(text + (f" (ещё {queued})" if queued else ""))
            elif queued:
                parts.append(f"расшифровка: в очереди {queued}")
        if self.voice_busy():
            parts.append("определяю голоса")
        jobs = len(self.summary_jobs)
        if self.analysis_running:
            parts.append("итоги" + (f" (ещё {jobs})" if jobs else ""))
        elif jobs:
            parts.append(f"итоги: в очереди {jobs}" + (" — после встречи" if self.detector.blocks_analysis else ""))
        if not parts:
            return ""
        text = " · ".join(parts)
        return "⟳  " + text[0].upper() + text[1:]

    def select_working(self):
        """Щелчок по метке обработки — показать встречу, которая сейчас обрабатывается."""
        tq = getattr(self, "tq", None)
        target = self.analysis_source
        if tq is not None and tq.current:
            target = tq.current
        if target is not None and self.records_tree.exists(str(target)):
            self.selected = Path(target)
            self.records_tree.select(str(target))
            self.show_meeting(self.selected, force=True)

    def refresh_status(self):
        text, kind = self.status_view()
        bg, fg = self.pal["pill"][kind]
        work = self.work_view()
        try:
            self.status.set(text, bg, fg, outer=self.pal["bg"])
            if work:
                wb, wf = self.pal["pill"]["busy"]
                self.work.set(work, wb, wf, outer=self.pal["bg"])
                if not self.work.winfo_ismapped():
                    self.work.pack(side="left", padx=(8, 0), after=self.status)
            elif self.work.winfo_ismapped():
                self.work.pack_forget()
            self.rec_btn.config(text="■  Остановить" if self.recorder is not None else "●  Записать")
            if self.recorder is not None:
                self.mute_btn.config(text="🎙 Включить микрофон" if self.recorder.mic_muted else "🔇 Выключить микрофон")
                self.sysmute_btn.config(text="🔊 Писать звук ПК" if self.recorder.system_muted else "🔇 Не писать звук ПК")
                if not self.mute_btn.winfo_ismapped():
                    self.mute_btn.pack(side="right", padx=(0, 8), before=self.rec_btn)
                    self.sysmute_btn.pack(side="right", padx=(0, 8), before=self.mute_btn)
            elif self.mute_btn.winfo_ismapped():
                self.mute_btn.pack_forget()
                self.sysmute_btn.pack_forget()
            sub = self.last_event_text or ""
            levels = self.recording_levels_text()
            if levels:
                sub = levels + ("   ·   " + sub if sub else "")
            self.substatus.config(text=sub)
            if self.tray is not None:
                tip = text.replace("●", "").strip() + ("\n" + work.replace("⟳", "").strip() if work else "")
                self.tray.update("Meeting Hub · " + tip)
        except tk.TclError:
            pass
        self.check_recording_health()

    def recording_levels_text(self):
        if self.recorder is None:
            return ""

        def bar(v):
            n = max(0, min(5, int(round(v * 5))))
            return "▮" * n + "▯" * (5 - n)
        h = self.recorder.health()
        parts = []
        for name, label in (("mic", "Вы"), ("system", "Собеседники")):
            info = h.get(name, {})
            if info.get("error"):
                parts.append(f"{label}: ошибка записи")
            elif not info.get("started"):
                parts.append(f"{label}: подключаю…")
            else:
                parts.append(f"{label} {bar(info.get('level', 0.0))}")
        return "   ".join(parts)

    # ------------------------------------------------------------------
    # Настройки
    # ------------------------------------------------------------------
    KEEP_CHOICES = [("всегда", 0), ("7 дней", 7), ("14 дней", 14), ("30 дней", 30), ("90 дней", 90),
                    ("180 дней", 180), ("365 дней", 365)]
    THEMES = [("как в Windows", "system")] + [(theme.THEMES[k]["label"], k) for k in theme.ORDER]

    def open_settings_dialog(self):
        if getattr(self, "settings_win", None) is not None and self.settings_win.winfo_exists():
            self.settings_win.lift()
            return
        win = self.settings_win = tk.Toplevel(self.root)
        win.title("Настройки")
        win.transient(self.root)
        win.geometry("660x640")
        win.minsize(560, 480)
        s = self.settings
        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=14, pady=(14, 0))

        def page(title):
            f = ttk.Frame(nb, padding=16)
            nb.add(f, text=title)
            return f

        def hint(parent, text):
            ttk.Label(parent, text=text, style="Small.TLabel", wraplength=540, justify="left").pack(
                anchor="w", padx=(28, 0), pady=(0, 10))

        def heading(parent, text, top=0):
            ttk.Label(parent, text=text, font=(theme.FONT, 10, "bold")).pack(anchor="w", pady=(top, 6))

        # 1. Основные
        p = page("Основные")
        full = tk.BooleanVar(value=self.record_meetings.get() and self.auto_transcribe.get()
                             and self.auto_summary.get())
        ttk.Checkbutton(p, text="Делать всё автоматически", variable=full).pack(anchor="w")
        hint(p, "Каждая встреча записывается, расшифровывается на этом компьютере и превращается в итоги "
                "и протокол. Участников нужно предупреждать о записи.")
        detect = tk.BooleanVar(value=self.auto_detect.get())
        ttk.Checkbutton(p, text="Замечать встречи сами", variable=detect).pack(anchor="w")
        hint(p, "Встреча начинается, когда открыто окно встречи и оно использует микрофон. "
                "Без этого запись — только кнопкой «Записать».")
        auto = tk.BooleanVar(value=daily.autostart_enabled())
        cb = ttk.Checkbutton(p, text="Запускать вместе с Windows", variable=auto)
        cb.pack(anchor="w")
        if not daily.autostart_supported():
            cb.state(["disabled"])
        hint(p, "Программа стартует в фоне со значком у часов — иначе встречи не записываются, пока она закрыта.")
        tray_var = tk.BooleanVar(value=bool(s.get("close_to_tray", True)))
        cb2 = ttk.Checkbutton(p, text="Крестик окна сворачивает в значок у часов", variable=tray_var)
        cb2.pack(anchor="w")
        if self.tray is None:
            cb2.state(["disabled"])
        hint(p, "Выйти полностью — правой кнопкой по значку у часов → «Выход».")
        row = ttk.Frame(p); row.pack(anchor="w", pady=(6, 0))
        ttk.Label(row, text="Оформление").pack(side="left")
        theme_var = tk.StringVar(value=dict((v, k) for k, v in self.THEMES).get(s.get("theme", "system"),
                                                                                "как в Windows"))
        theme_box = ttk.Combobox(row, textvariable=theme_var, values=[k for k, _ in self.THEMES],
                                 state="readonly", width=16)
        theme_box.pack(side="left", padx=10)
        saved_theme = s.get("theme", "system")

        def preview_theme(_e=None):          # тема меняется сразу, «Отмена» вернёт прежнюю
            self.apply_theme(dict(self.THEMES)[theme_var.get()])
        theme_box.bind("<<ComboboxSelected>>", preview_theme)

        def cancel_settings():
            if dict(self.THEMES)[theme_var.get()] != saved_theme:
                self.apply_theme(saved_theme)
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", cancel_settings)

        # 2. Звук
        p = page("Звук")
        heading(p, "Проверка записи")
        ttk.Label(p, style="Muted.TLabel", wraplength=560, justify="left", text=(
            "Пять секунд говорите в микрофон и включите любой звук на компьютере (видео, музыку). "
            "Программа покажет, слышны ли вы и собеседники.")).pack(anchor="w")
        self.test_btn = ttk.Button(p, text="Проверить запись", style="Accent.TButton", command=self.test_recording)
        self.test_btn.pack(anchor="w", pady=(8, 16))
        heading(p, "Если запись не работает")
        for text, cmd in (("Мастер настройки", self.open_first_run),
                          ("Подробная проверка звука", self.full_audio_check),
                          ("Отчёт для поддержки…", self.save_support_bundle)):
            ttk.Button(p, text=text, command=cmd).pack(anchor="w", pady=2)
        ttk.Label(p, style="Small.TLabel", wraplength=560, justify="left", text=(
            "Частая причина — антивирус Kaspersky: добавьте Meeting Hub в доверенные программы. "
            "Мастер настройки подскажет, где это сделать.")).pack(anchor="w", pady=(10, 0))

        # 3. Расшифровка и итоги
        p = page("Расшифровка и итоги")
        model = tk.StringVar(value=s.get("whisper_model", tr_mod.DEFAULT_MODEL))
        lang = tk.StringVar(value=s.get("language", "ru"))
        llm = tk.StringVar(value=s.get("ollama_model", self.default_llm))
        engines = {"GigaAM (русский, рекомендуется)": "gigaam", "Whisper": "whisper"}
        engine = tk.StringVar(value=next((k for k, v in engines.items() if v == s.get("asr_engine", "gigaam")),
                                         "GigaAM (русский, рекомендуется)"))
        grid = ttk.Frame(p); grid.pack(fill="x")
        ttk.Label(grid, text="Распознавание речи").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Combobox(grid, textvariable=engine, values=list(engines), state="readonly",
                     width=32).grid(row=0, column=1, sticky="w", padx=10)
        ttk.Label(grid, style="Small.TLabel", wraplength=520, justify="left", text=(
            "GigaAM — модель Сбера для русского языка, работает на вашем компьютере (при первом включении "
            "скачивается около 230 МБ). Если она не сработает или язык не русский, программа сама использует Whisper."
        )).grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Label(grid, text="Качество Whisper").grid(row=3, column=0, sticky="w", pady=(12, 4))
        ttk.Combobox(grid, textvariable=model, values=list(tr_mod.MODELS), state="readonly",
                     width=12).grid(row=3, column=1, sticky="w", padx=10, pady=(12, 4))
        mhint = ttk.Label(grid, style="Small.TLabel")
        mhint.grid(row=4, column=0, columnspan=2, sticky="w")
        model.trace_add("write", lambda *_: mhint.config(text=tr_mod.MODELS.get(model.get(), "")))
        mhint.config(text=tr_mod.MODELS.get(model.get(), ""))
        ttk.Label(grid, text="Язык встреч").grid(row=2, column=0, sticky="w", pady=(12, 4))
        ttk.Combobox(grid, textvariable=lang, values=["ru", "en", "auto"], state="readonly",
                     width=12).grid(row=2, column=1, sticky="w", padx=10, pady=(12, 4))
        ttk.Label(grid, text="Модель для итогов (Ollama)").grid(row=5, column=0, sticky="w", pady=(12, 4))
        ttk.Entry(grid, textvariable=llm, width=18).grid(row=5, column=1, sticky="w", padx=10, pady=(12, 4))
        ttk.Button(grid, text="Проверить", command=lambda: self.check_ollama(llm.get())).grid(
            row=5, column=2, sticky="w", pady=(12, 4))
        ttk.Button(grid, text="Установить ИИ…", command=self.open_ai_setup).grid(
            row=5, column=3, sticky="w", padx=(8, 0), pady=(12, 4))
        voices_on = tk.BooleanVar(value=self.voices_wanted())
        vrow = ttk.Frame(p); vrow.pack(anchor="w", pady=(14, 0))
        ttk.Checkbutton(vrow, text="Определять, кто говорит (по голосу)", variable=voices_on).pack(side="left")
        ttk.Button(vrow, text="Известные голоса…", command=lambda: self.open_known_voices(win)).pack(
            side="left", padx=(12, 0))
        pause = tk.BooleanVar(value=self.auto_pause.get())
        ttk.Checkbutton(p, text="Не нагружать компьютер во время встречи", variable=pause).pack(anchor="w",
                                                                                              pady=(16, 0))
        hint(p, "Расшифровка и итоги ждут конца встречи, чтобы звонок не тормозил.")
        ttk.Label(p, style="Small.TLabel", wraplength=560, justify="left", text=(
            "Итоги и чат готовит локальная модель Ollama — нажмите «Установить ИИ…», программа всё сделает сама. "
            "Без неё в протоколе будет только расшифровка.")).pack(anchor="w")

        ttk.Separator(p).pack(fill="x", pady=(14, 8))
        ttk.Label(p, text="GigaChat (необязательно)", style="H2.TLabel").pack(anchor="w")
        g_code = tk.StringVar(value=s.get("giga_code", ""))
        hint(p, "Код доступа к GigaChat. Его выдаёт администратор; ключи Сбера в программе "
                "не нужны и нигде не хранятся.")
        grow = ttk.Frame(p); grow.pack(fill="x")
        ttk.Label(grow, text="Код доступа").pack(side="left")
        ttk.Entry(grow, textvariable=g_code, show="•", width=34).pack(side="left", padx=8)

        g_on = tk.BooleanVar(value=bool(s.get("giga_enabled")))
        ttk.Checkbutton(p, text="Итоги, задачи и чат через GigaChat", variable=g_on).pack(anchor="w", pady=(10, 0))
        hint(p, "Если облако не отвечает или лимит кончился, программа сама использует локальную модель. "
                "Текст встреч уходит на сервер команды и дальше к Сберу — включайте, только если это допустимо "
                "для ваших встреч.")
        brow = ttk.Frame(p); brow.pack(fill="x", padx=(28, 0))
        ttk.Button(brow, text="Проверить GigaChat", command=lambda: self.check_giga(g_code.get().strip(), win)).pack(
            side="left")

        ttk.Separator(p).pack(fill="x", pady=(14, 8))
        ttk.Label(p, text="Канбан", style="H2.TLabel").pack(anchor="w")
        hint(p, "Готовые встречи (итоги, решения, задачи, расшифровка) попадают во вкладку «Встречи» вашего Канбана. "
                "Почта и пароль нужны только для подключения и нигде не сохраняются.")
        k_state = tk.StringVar(value=self.kanban_status_text())
        ttk.Label(p, textvariable=k_state).pack(anchor="w", padx=(28, 0))
        k_auto = tk.BooleanVar(value=bool(s.get("kanban_auto", True)))
        ttk.Checkbutton(p, text="Отправлять каждую готовую встречу автоматически", variable=k_auto).pack(
            anchor="w", padx=(28, 0), pady=(6, 0))
        krow = ttk.Frame(p); krow.pack(fill="x", padx=(28, 0), pady=(6, 0))
        ttk.Button(krow, text="Подключить…", command=lambda: self.kanban_connect_dialog(
            win, lambda: k_state.set(self.kanban_status_text()))).pack(side="left")
        ttk.Button(krow, text="Проверить", command=lambda: self.kanban_check(win)).pack(side="left", padx=(8, 0))
        ttk.Button(krow, text="Отключить", command=lambda: (self.kanban_disconnect(),
                                                              k_state.set(self.kanban_status_text()))).pack(
            side="left", padx=(8, 0))

        # 4. Хранение
        p = page("Хранение")
        keep_now = int(s.get("keep_audio_days", 0))
        keep = tk.StringVar(value=next((k for k, v in self.KEEP_CHOICES if v == keep_now), "всегда"))
        row = ttk.Frame(p); row.pack(anchor="w")
        ttk.Label(row, text="Хранить аудио").pack(side="left")
        ttk.Combobox(row, textvariable=keep, values=[k for k, _ in self.KEEP_CHOICES], state="readonly",
                     width=12).pack(side="left", padx=10)
        used = daily.folder_size(self.rec_folder, "*.wav") / (1024 ** 3)
        ttk.Label(p, style="Small.TLabel", wraplength=560, justify="left", text=(
            "Удаляется только аудио старше срока и только с готовой расшифровкой; тексты, итоги и протоколы "
            f"хранятся всегда. Час записи — около 220 МБ. Сейчас аудио занимает {used:.1f} ГБ.")
                  ).pack(anchor="w", pady=(6, 12))
        ttk.Button(p, text="Открыть папку с записями", command=lambda: self.open_path(self.rec_folder)).pack(anchor="w")
        upd = tk.BooleanVar(value=bool(s.get("check_updates", True)))
        heading(p, "Обновления", top=18)
        ttk.Checkbutton(p, text="Проверять обновления", variable=upd).pack(anchor="w")
        ttk.Button(p, text="Проверить сейчас", command=lambda: self.check_updates(manual=True)).pack(anchor="w",
                                                                                                   pady=(6, 0))
        ttk.Label(p, style="Small.TLabel", text=f"Версия {self.app_version} · данные: {self.data_dir}",
                  wraplength=560).pack(anchor="w", pady=(16, 0))

        # 5. Для опытных
        p = page("Для опытных")
        ttk.Label(p, style="Muted.TLabel", wraplength=560, justify="left", text=(
            "Как программа решает, что идёт встреча, и как её научить. Обычно сюда заходить не нужно.")
                  ).pack(anchor="w", pady=(0, 10))
        for text, cmd in (("Журнал обнаружения и обучение…", self.open_journal_window),
                          ("Шаблоны встреч…", self.open_patterns),
                          ("Встреча была, но не записалась…", self.missed_meeting),
                          ("Начать режим встречи вручную", lambda: self.user_action("manual_start")),
                          ("Завершить режим встречи", lambda: self.user_action("manual_end")),
                          ("Экспорт данных обучения…", self.export_training),
                          ("Импорт данных обучения…", self.import_training)):
            ttk.Button(p, text=text, command=cmd).pack(anchor="w", pady=2)

        def save():
            if daily.autostart_supported() and auto.get() != daily.autostart_enabled():
                try:
                    daily.set_autostart(auto.get())
                except OSError as e:
                    messagebox.showerror("Автозапуск", f"Не удалось изменить автозапуск: {e}", parent=win)
            want_full = bool(full.get())
            was_full = self.record_meetings.get() and self.auto_transcribe.get() and self.auto_summary.get()
            if want_full and not was_full:
                ok, why = tr_mod.LocalWhisper.available()
                if not ok:
                    messagebox.showerror("Расшифровка", why, parent=win)
                    want_full = False
            if want_full != was_full:
                for var in (self.record_meetings, self.auto_transcribe, self.auto_summary):
                    var.set(want_full)
            if self.auto_detect.get() != bool(detect.get()):
                self.auto_detect.set(bool(detect.get()))
                self.toggle_auto_detect()
            self.auto_pause.set(bool(pause.get()))
            old = (s.get("whisper_model", tr_mod.DEFAULT_MODEL), s.get("language", "ru"), s.get("asr_engine", "gigaam"),
                   s.get("giga_code", ""))
            if g_on.get() and not s.get("giga_enabled") and not messagebox.askokcancel(
                    "GigaChat", "Тексты встреч (расшифровка и вопросы в чате) будут отправляться на сервер команды и дальше в облако Сбера.\n\n"
                    "Включить?", icon="warning", parent=win):
                g_on.set(False)
            new_theme = dict(self.THEMES)[theme_var.get()]
            s.update(close_to_tray=bool(tray_var.get()), check_updates=bool(upd.get()),
                     keep_audio_days=dict(self.KEEP_CHOICES)[keep.get()], whisper_model=model.get(), asr_engine=engines[engine.get()],
                     language=lang.get(), ollama_model=llm.get().strip() or self.default_llm,
                     record_meetings=self.record_meetings.get(), auto_transcribe=self.auto_transcribe.get(),
                     auto_summary=self.auto_summary.get(), auto_detect=self.auto_detect.get(),
                     auto_pause=self.auto_pause.get(), voices_enabled=bool(voices_on.get()),
                     giga_enabled=bool(g_on.get()), giga_code=g_code.get().strip(),
                     kanban_auto=bool(k_auto.get()))
            for old_key in ("salute_enabled", "salute_key", "salute_scope", "salute_insecure"):
                s.pop(old_key, None)                      # SaluteSpeech из программы убран
            theme_changed = new_theme != s.get("theme", "system")
            s["theme"] = new_theme
            if theme_changed:
                self.apply_theme(new_theme)
                theme_changed = False
            self.save_settings()
            tq = getattr(self, "tq", None)
            if tq is not None:
                tq.language = None if lang.get() == "auto" else lang.get()
                if old != (model.get(), lang.get(), engines[engine.get()], g_code.get().strip()) and not tq.busy():
                    tq.engine = None
            self.last_event_text = "Настройки сохранены."
            self.refresh_status()
            win.destroy()

        bar = ttk.Frame(win, padding=14)
        bar.pack(fill="x")
        ttk.Button(bar, text="Отмена", command=cancel_settings).pack(side="right")
        ttk.Button(bar, text="Сохранить", style="Accent.TButton", command=save).pack(side="right", padx=(0, 8))

    def apply_theme(self, mode):
        """Сменить оформление сразу, без перезапуска."""
        self.pal = theme.apply(self.root, mode)
        for text in (self.sum_text, self.tr_text):
            theme.text_style(text, self.pal)
            text.configure(state="disabled")
        self.status.restyle(self.pal)
        self.work.restyle(self.pal)
        self.records_tree.set_pal(self.pal)
        self.chat_restyle()
        self.action_bar.configure(bg=self.pal["select"])
        self.action_text.configure(bg=self.pal["select"], fg=self.pal["select_fg"], font=(theme.FONT, 10))
        if getattr(self, "_placeholder", False):
            self._entry_style("Placeholder.TEntry")
        self.refresh_records()
        self.show_meeting(self.selected, force=True)
        self.refresh_status()

    # ------------------------------------------------------------------
    # Журнал обнаружения (для опытных)
    # ------------------------------------------------------------------
    def open_journal_window(self):
        if getattr(self, "journal_win", None) is not None and self.journal_win.winfo_exists():
            self.journal_win.lift()
            return
        win = self.journal_win = tk.Toplevel(self.root)
        win.title("Журнал обнаружения и обучение")
        win.geometry("1180x640")
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)
        self.build_training_panel(frame)

        def closed(_e=None):
            if _e is None or _e.widget is win:
                for name in ("training_tree", "training_summary", "mic_label"):
                    if hasattr(self, name):
                        delattr(self, name)
        win.bind("<Destroy>", closed)
