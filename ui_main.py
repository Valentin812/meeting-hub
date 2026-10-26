# -*- coding: utf-8 -*-
"""Главное окно Meeting Hub v7: список встреч слева, выбранная встреча справа, одна главная кнопка.

Всё техническое — в окне «Настройки». Логика (обнаружение, запись, расшифровка, итоги) живёт в app.py,
здесь только то, что видит пользователь.
"""
import csv
import json
import os
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

import daily
import recorder as rec_mod
import report as report_mod
import theme
import transcriber as tr_mod
from meeting_detector import ASKING, COUNTDOWN, ENDING, IN_MEETING

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]
SEARCH_HINT = "Поиск по встречам"


def day_label(stem, today=None):
    try:
        d = datetime.strptime(stem[:8], "%Y%m%d").date()
    except ValueError:
        return "Без даты"
    today = today or datetime.now().date()
    if d == today:
        return "Сегодня"
    if d == today - timedelta(days=1):
        return "Вчера"
    return f"{d.day} {MONTHS[d.month - 1]}" + ("" if d.year == today.year else f" {d.year}")


def clock(stem):
    return f"{stem[9:11]}:{stem[11:13]}" if len(stem) >= 13 else ""


def dur_text(sec):
    sec = int(sec or 0)
    if sec >= 3600:
        return f"{sec // 3600} ч {sec % 3600 // 60} мин"
    if sec >= 60:
        return f"{sec // 60} мин"
    return f"{sec} с"


def meeting_title(item):
    title = (item.get("window") or item.get("meeting") or "").strip()
    return title or "Запись без названия"


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class MainWindow:
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

        header = ttk.Frame(root, padding=(18, 14, 18, 4))
        header.pack(fill="x")
        ttk.Label(header, text="Meeting Hub", style="Title.TLabel").pack(side="left")
        self.status = tk.Label(header, text="Запуск…", font=(theme.FONT, 10, "bold"), padx=12, pady=3,
                               bd=0, highlightthickness=0)
        self.status.pack(side="left", padx=(14, 0))
        ttk.Button(header, text="⚙", width=3, command=self.open_settings_dialog).pack(side="right")
        self.rec_btn = ttk.Button(header, text="●  Записать", style="Accent.TButton",
                                  command=self.toggle_recording_button)
        self.rec_btn.pack(side="right", padx=(0, 8))
        self.substatus = ttk.Label(root, text="", style="Small.TLabel", padding=(18, 0, 18, 10))
        self.substatus.pack(fill="x")
        ttk.Separator(root).pack(fill="x")

        body = ttk.PanedWindow(root, orient="horizontal")
        body.pack(fill="both", expand=True)

        # Слева — встречи
        left = ttk.Frame(body, padding=(12, 12, 6, 12))
        body.add(left, weight=0)
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(left, textvariable=self.search_var)
        self.search_entry.pack(fill="x", pady=(0, 8))
        self._search_placeholder(True)
        self.search_entry.bind("<FocusIn>", lambda _e: self._search_placeholder(False))
        self.search_entry.bind("<FocusOut>", lambda _e: self._search_placeholder(True))
        self._search_job = None
        self.search_var.trace_add("write", self._on_search)
        listbox = ttk.Frame(left)
        listbox.pack(fill="both", expand=True)
        self.records_tree = ttk.Treeview(listbox, columns=("info",), show="tree", selectmode="browse",
                                         style="Meetings.Treeview")
        self.records_tree.column("#0", width=230, minwidth=160, stretch=True)
        self.records_tree.column("info", width=150, minwidth=110, anchor="e", stretch=False)
        self.records_tree.tag_configure("day", foreground=pal["muted"], font=(theme.FONT, 9, "bold"))
        sb = ttk.Scrollbar(listbox, orient="vertical", command=self.records_tree.yview)
        self.records_tree.configure(yscrollcommand=sb.set)
        self.records_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.records_tree.bind("<<TreeviewSelect>>", lambda _e: self.on_meeting_selected())
        self.records_tree.bind("<Double-1>", lambda _e: self.open_protocol())
        self.list_hint = ttk.Label(left, text="", style="Small.TLabel", wraplength=330, justify="left")
        self.list_hint.pack(fill="x", pady=(6, 0))

        # Справа — встреча
        right = ttk.Frame(body, padding=(16, 12, 18, 12))
        body.add(right, weight=1)
        self.detail = ttk.Frame(right)
        head = ttk.Frame(self.detail)
        head.pack(fill="x")
        names = ttk.Frame(head)
        names.pack(side="left", fill="x", expand=True)
        self.d_title = ttk.Label(names, text="", style="H2.TLabel", wraplength=520, justify="left")
        self.d_title.pack(anchor="w")
        self.d_meta = ttk.Label(names, text="", style="Muted.TLabel")
        self.d_meta.pack(anchor="w", pady=(2, 0))
        self.more_btn = ttk.Menubutton(head, text="⋯", width=3)
        self.more_btn.pack(side="right")
        self.more_menu = tk.Menu(self.more_btn, tearoff=False)
        self.more_btn["menu"] = self.more_menu
        self.proto_btn = ttk.Button(head, text="Протокол", command=self.open_protocol)
        self.proto_btn.pack(side="right", padx=(0, 6))
        self.play_btn = ttk.Button(head, text="▶  Слушать", command=self.play_selected_record)
        self.play_btn.pack(side="right", padx=(0, 6))

        self.action_bar = ttk.Frame(self.detail, padding=(0, 10, 0, 0))
        self.action_text = ttk.Label(self.action_bar, text="", style="Muted.TLabel")
        self.action_text.pack(side="left")
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

        self.empty = ttk.Frame(right, padding=(10, 60))
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
                ttk.Style(self.root).configure(style, foreground=self.pal["faint"])
            self.search_entry.configure(style=style)
        except tk.TclError:
            pass

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
    def meeting_state(self, item):
        """(короткий текст для списка, ключ состояния)."""
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
        hits = daily.search(self.rec_folder, query) if query else None
        items = [i for i in rec_mod.list_recordings(self.rec_folder)
                 if hits is None or i["path"].stem in hits]
        rows = []                                             # (день, iid, текст, инфо)
        for it in items:
            stem = it["path"].stem
            state, _ = self.meeting_state(it)
            info = " · ".join(x for x in (clock(stem), dur_text(it.get("duration_sec")), state) if x)
            rows.append((day_label(stem), str(it["path"]), meeting_title(it), info))
        tree = self.records_tree
        structure = [(d, iid) for d, iid, _t, _i in rows]
        if structure == getattr(self, "_list_structure", None):
            for _d, iid, title, info in rows:                # то же самое — только обновить подписи
                tree.item(iid, text="  " + title, values=(info,))
        else:
            sel = self.selected
            tree.delete(*tree.get_children())
            days = {}
            for day, iid, title, info in rows:
                if day not in days:
                    days[day] = tree.insert("", "end", iid="day:" + day, text=day, open=True, tags=("day",))
                tree.insert(days[day], "end", iid=iid, text="  " + title, values=(info,))
            self._list_structure = structure
            if sel is not None and tree.exists(str(sel)):
                tree.selection_set(str(sel))
            elif rows:
                tree.selection_set(rows[0][1])
            else:
                self.selected = None
        if query:
            self.list_hint.config(text=f"Найдено встреч: {len(rows)}" if rows else "Ничего не найдено")
        else:
            self.list_hint.config(text="" if rows else "Встреч пока нет")
        self.show_meeting(self.selected)

    def meeting_rows(self):
        return [iid for d in self.records_tree.get_children() for iid in self.records_tree.get_children(d)]

    def on_meeting_selected(self):
        sel = self.records_tree.selection()
        if not sel:
            return
        iid = sel[0]
        if iid.startswith("day:"):
            kids = self.records_tree.get_children(iid)
            if kids:
                self.records_tree.selection_set(kids[0])
            return
        self.selected = Path(iid)
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
        self.play_btn.state(["!disabled"] if wav.exists() else ["disabled"])
        self.proto_btn.state(["!disabled"] if tr_mod.has_transcript(wav) or self.summary_path(wav).exists()
                             else ["disabled"])
        self._fill_more_menu(wav)
        self._fill_action(wav, state, key)
        result = read_json(self.summary_path(wav))
        self._render_summary(result, wav, key)
        self._render_tasks(result)
        self._render_transcript(wav)

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
                total = tr_mod.MODEL_MB.get(self.settings.get("whisper_model", tr_mod.DEFAULT_MODEL), 0)
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
                self.action_bar.pack(fill="x", before=self.detail_nb)
        else:
            self.action_bar.pack_forget()

    def _fill_more_menu(self, wav):
        m = self.more_menu
        m.delete(0, "end")
        has_text = tr_mod.has_transcript(wav)
        if has_text:
            m.add_command(label="Подготовить итоги заново", command=lambda: self.request_summary(wav, rerun=True))
        if wav.exists():
            m.add_command(label="Расшифровать заново", command=lambda: self.transcribe_files([wav], force=True))
        if has_text:
            m.add_command(label="Открыть расшифровку (TXT)", command=lambda: self.open_path(tr_mod.output_paths(wav)[0]))
        if self.summary_path(wav).exists():
            m.add_command(label="Сохранить задачи в CSV…", command=lambda: self.export_tasks(wav))
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
        if not segs:
            self._write(self.tr_text, [("Расшифровки пока нет.", ("muted",))])
            return
        out = []
        for s in segs:
            me = s.get("speaker") == tr_mod.SPEAKERS[0]
            out.append((tr_mod.stamp(s.get("start", 0)) + "   ", ("time",)))
            out.append((s.get("speaker", "") + "\n", ("me" if me else "they",)))
            out.append((s.get("text", "") + "\n\n", ()))
        self._write(self.tr_text, out)

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
        path = report_mod.protocol_paths(wav)[0]
        if not path.exists() or self._mtime(path) < max(self._mtime(self.summary_path(wav)),
                                                         self._mtime(tr_mod.output_paths(wav)[1])):
            path = self.make_protocol(wav)
        if path is None:
            messagebox.showinfo("Протокол", "Протокол появится после расшифровки встречи.", parent=self.root)
            return
        self.open_path(path)

    def play_selected_record(self):
        wav = self.current()
        if wav is None:
            return
        if not wav.exists():
            messagebox.showinfo("Встреча", "Аудио этой встречи удалено по сроку хранения; текст сохранён.",
                                parent=self.root)
            return
        self.open_path(wav)

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
        if self._saving_threads:
            return "Сохраняю запись…", "busy"
        tq = getattr(self, "tq", None)
        if tq is not None and tq.busy():
            if any(x["state"] == "downloading" for x in tq.status.values()):
                return "Скачиваю модель распознавания…", "busy"
            return "Расшифровываю встречу…", "busy"
        if self.analysis_running or self.summary_jobs:
            return "Готовлю итоги…", "busy"
        if not self.auto_detect.get():
            return "Автообнаружение выключено", "off"
        return "Жду встречу", "ok"

    def refresh_status(self):
        text, kind = self.status_view()
        bg, fg = self.pal["pill"][kind]
        try:
            self.status.config(text=text, bg=bg, fg=fg)
            self.rec_btn.config(text="■  Остановить" if self.recorder is not None else "●  Записать")
            sub = self.last_event_text or ""
            levels = self.recording_levels_text()
            if levels:
                sub = levels + ("   ·   " + sub if sub else "")
            self.substatus.config(text=sub)
            if self.tray is not None:
                self.tray.update("Meeting Hub · " + text.replace("●", "").strip())
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
    THEMES = [("как в Windows", "system"), ("светлая", "light"), ("тёмная", "dark")]

    def open_settings_dialog(self):
        if getattr(self, "settings_win", None) is not None and self.settings_win.winfo_exists():
            self.settings_win.lift()
            return
        win = self.settings_win = tk.Toplevel(self.root)
        win.title("Настройки")
        win.transient(self.root)
        win.geometry("640x560")
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
        ttk.Combobox(row, textvariable=theme_var, values=[k for k, _ in self.THEMES], state="readonly",
                     width=16).pack(side="left", padx=10)

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
        grid = ttk.Frame(p); grid.pack(fill="x")
        ttk.Label(grid, text="Качество распознавания").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Combobox(grid, textvariable=model, values=list(tr_mod.MODELS), state="readonly",
                     width=12).grid(row=0, column=1, sticky="w", padx=10)
        mhint = ttk.Label(grid, style="Small.TLabel")
        mhint.grid(row=1, column=0, columnspan=2, sticky="w")
        model.trace_add("write", lambda *_: mhint.config(text=tr_mod.MODELS.get(model.get(), "")))
        mhint.config(text=tr_mod.MODELS.get(model.get(), ""))
        ttk.Label(grid, text="Язык встреч").grid(row=2, column=0, sticky="w", pady=(12, 4))
        ttk.Combobox(grid, textvariable=lang, values=["ru", "en", "auto"], state="readonly",
                     width=12).grid(row=2, column=1, sticky="w", padx=10, pady=(12, 4))
        ttk.Label(grid, text="Модель для итогов (Ollama)").grid(row=3, column=0, sticky="w", pady=(12, 4))
        ttk.Entry(grid, textvariable=llm, width=18).grid(row=3, column=1, sticky="w", padx=10, pady=(12, 4))
        ttk.Button(grid, text="Проверить", command=lambda: self.check_ollama(llm.get())).grid(
            row=3, column=2, sticky="w", pady=(12, 4))
        pause = tk.BooleanVar(value=self.auto_pause.get())
        ttk.Checkbutton(p, text="Не нагружать компьютер во время встречи", variable=pause).pack(anchor="w",
                                                                                              pady=(16, 0))
        hint(p, "Расшифровка и итоги ждут конца встречи, чтобы звонок не тормозил.")
        ttk.Label(p, style="Small.TLabel", wraplength=560, justify="left", text=(
            "Итоги готовит локальная модель Ollama — один раз запустите setup_ollama.bat из папки программы. "
            "Без неё в протоколе будет только расшифровка.")).pack(anchor="w")

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
            old = (s.get("whisper_model", tr_mod.DEFAULT_MODEL), s.get("language", "ru"))
            new_theme = dict(self.THEMES)[theme_var.get()]
            s.update(close_to_tray=bool(tray_var.get()), check_updates=bool(upd.get()),
                     keep_audio_days=dict(self.KEEP_CHOICES)[keep.get()], whisper_model=model.get(),
                     language=lang.get(), ollama_model=llm.get().strip() or self.default_llm,
                     record_meetings=self.record_meetings.get(), auto_transcribe=self.auto_transcribe.get(),
                     auto_summary=self.auto_summary.get(), auto_detect=self.auto_detect.get(),
                     auto_pause=self.auto_pause.get())
            theme_changed = new_theme != s.get("theme", "system")
            s["theme"] = new_theme
            self.save_settings()
            tq = getattr(self, "tq", None)
            if tq is not None:
                tq.language = None if lang.get() == "auto" else lang.get()
                if old != (model.get(), lang.get()) and not tq.busy():
                    tq.engine = None
            self.last_event_text = "Настройки сохранены." + (
                " Оформление поменяется после перезапуска программы." if theme_changed else "")
            self.refresh_status()
            win.destroy()

        bar = ttk.Frame(win, padding=14)
        bar.pack(fill="x")
        ttk.Button(bar, text="Отмена", command=win.destroy).pack(side="right")
        ttk.Button(bar, text="Сохранить", style="Accent.TButton", command=save).pack(side="right", padx=(0, 8))

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
