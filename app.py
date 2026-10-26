# -*- coding: utf-8 -*-
"""Meeting Hub v5 — локальный анализ расшифровок через Ollama и обнаружение встреч."""
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from datetime import datetime
import os, sys, queue, json, csv, urllib.request, urllib.error, threading, re, time, subprocess, math

OLLAMA_URL = "http://localhost:11434/api/chat"
DEFAULT_MODEL = "qwen2.5:3b"

from analyzer import ollama_chat, analyze_transcript, SYSTEM_PROMPT  # noqa: E402,F401


def ollama_request(model, transcript, cancel_event=None):
    """Итоги встречи: длинные расшифровки режутся на части и объединяются (analyzer.py)."""
    return analyze_transcript(transcript, lambda system, user: ollama_chat(model, system, user, cancel_event),
                              cancel_event)


def list_models():
    req = urllib.request.Request("http://localhost:11434/api/tags")
    with urllib.request.urlopen(req, timeout=5) as r:
        return [x.get("name", "") for x in json.loads(r.read().decode("utf-8")).get("models", [])]



# ---------------------------------------------------------------------------
# Обнаружение встреч v5: один цикл опроса в главном потоке Tkinter,
# вся логика — в meeting_detector.py, сигналы Windows — в win_signals.py.
# ---------------------------------------------------------------------------
import logging
from logging.handlers import RotatingFileHandler
from tkinter import simpledialog

import win_signals
import recorder as rec_mod
import transcriber as tr_mod
import report as report_mod
import daily
import tray as tray_mod
from meeting_detector import (MeetingDetector, DEFAULT_PATTERNS, STATE_LABELS, BROWSERS, MEETING_APPS,
                              IDLE, ASKING, COUNTDOWN, IN_MEETING, ENDING,
                              suggest_pattern, strip_browser_suffix)
from training_store import TrainingStore

APP_VERSION = "6.1.0"

import support
from support import app_dir, data_dir
TICK_MS = 1000

WHO = {"auto": "Автоматика", "user": "Пользователь", "legacy": "v4.9 (архив)"}
USER_LABELS = {"correct": "Верно", "wrong": "Ошибка", "": ""}


def setup_logging(folder):
    logger = logging.getLogger("meeting_hub")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        try:
            handler = RotatingFileHandler(Path(folder) / "meeting_hub.log", maxBytes=512 * 1024,
                                          backupCount=3, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
        except OSError:
            logger.addHandler(logging.NullHandler())
    return logger


class MeetingPrompt(tk.Toplevel):
    """Небольшое окно поверх всех окон: отсчёт, вопрос «это встреча?», вопрос о завершении."""

    def __init__(self, app, kind):
        super().__init__(app.root)
        self.app = app
        self.kind = kind
        self.title("Meeting Hub")
        self.resizable(False, False)
        try:
            self.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        box = ttk.Frame(self, padding=14)
        box.pack(fill="both", expand=True)
        self.head = ttk.Label(box, font=("Segoe UI", 11, "bold"))
        self.head.pack(anchor="w")
        self.body = ttk.Label(box, wraplength=400, justify="left")
        self.body.pack(anchor="w", pady=(4, 8))
        self.remember = tk.BooleanVar(value=False)
        self.pattern = tk.StringVar()
        if kind == ASKING:
            self._build_learning(box)
        bar = ttk.Frame(box)
        bar.pack(fill="x", pady=(6, 0))
        if kind == COUNTDOWN:
            ttk.Button(bar, text="Отмена", command=lambda: app.user_action("cancel_countdown")).pack(side="left")
            ttk.Button(bar, text="Начать сейчас", command=lambda: app.user_action("start_now")).pack(side="left", padx=6)
        elif kind == ASKING:
            ttk.Button(bar, text="Да, это встреча", command=self.answer_yes).pack(side="left")
            ttk.Button(bar, text="Нет", command=lambda: app.user_action("answer_ask", False)).pack(side="left", padx=6)
        elif kind == ENDING:
            ttk.Button(bar, text="Нет, встреча идёт", command=lambda: app.user_action("keep_meeting")).pack(side="left")
            ttk.Button(bar, text="Завершить сейчас", command=lambda: app.user_action("end_now")).pack(side="left", padx=6)
        self.update_idletasks()
        x = self.winfo_screenwidth() - self.winfo_reqwidth() - 24
        y = self.winfo_screenheight() - self.winfo_reqheight() - 90
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _build_learning(self, box):
        cand = self.app.detector.candidate
        titles = []
        if cand is not None:
            if cand.kind == "window_only" and cand.title:
                titles = [cand.title]
            elif self.app.last_obs is not None:
                titles = [t for t, p in self.app.last_obs.windows if (p or "").lower() == cand.app]
        titles = [strip_browser_suffix(t) for t in titles if t.strip()][:20]
        learn = ttk.LabelFrame(box, text="Запомнить, чтобы в следующий раз включаться самостоятельно", padding=8)
        learn.pack(fill="x")
        ttk.Checkbutton(learn, text="Запомнить окно встречи", variable=self.remember).pack(anchor="w")
        ttk.Label(learn, text="Окно встречи:").pack(anchor="w", pady=(6, 0))
        combo = ttk.Combobox(learn, values=titles, width=52, state="readonly" if titles else "normal")
        combo.pack(fill="x")
        ttk.Label(learn, text="Шаблон заголовка (без кода комнаты):").pack(anchor="w", pady=(6, 0))
        ttk.Entry(learn, textvariable=self.pattern, width=54).pack(fill="x")

        def pick(_event=None):
            self.pattern.set(suggest_pattern(combo.get()))
            self.remember.set(True)
        combo.bind("<<ComboboxSelected>>", pick)
        if cand is not None and cand.kind == "window_only" and titles:
            combo.current(0)
            pick()

    def answer_yes(self):
        if self.remember.get() and self.pattern.get().strip():
            cand = self.app.detector.candidate
            self.app.add_pattern(self.pattern.get(), cand.label if cand and cand.kind != "browser_mic" else "",
                                 "meeting", example=cand.title if cand else "")
        self.app.user_action("answer_ask", True)

    def set_text(self, head, body):
        self.head.config(text=head)
        self.body.config(text=body)

    def on_close(self):
        # Закрытие окна — «не сейчас», а не «никогда».
        action = {COUNTDOWN: ("cancel_countdown",), ASKING: ("answer_ask", False),
                  ENDING: ("keep_meeting",)}.get(self.kind)
        if action:
            self.app.user_action(*action)
        else:
            self.destroy()


class FirstRunWizard(tk.Toplevel):
    """Мастер настройки: перенос данных старой версии, проверка записи с инструкцией, согласие на запись."""

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("Meeting Hub — настройка")
        self.geometry("760x720")
        self.transient(app.root)
        self.protocol("WM_DELETE_WINDOW", self.finish)
        box = ttk.Frame(self, padding=16)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text=f"Добро пожаловать в Meeting Hub {APP_VERSION}", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(box, wraplength=720, justify="left", text=(
            "Программа сама замечает начало онлайн-встречи, записывает её и помогает подвести итоги. "
            f"Все данные хранятся только на этом компьютере: {app.data_dir}")).pack(anchor="w", pady=(4, 12))

        # 1. Перенос данных
        self.old = [p for p in support.find_old_versions() if p.resolve() != app.data_dir.resolve()]
        mig = ttk.LabelFrame(box, text="1. Данные предыдущей версии", padding=10)
        mig.pack(fill="x", pady=(0, 10))
        has_data = bool(app.store.meeting_patterns() or app.store.data.get("journal"))
        hint = ("Найдены папки старых версий. Перенесите шаблоны встреч, журнал и записи — исходная папка не меняется."
                if self.old else "Если раньше пользовались Meeting Hub из ZIP-архива, укажите его папку.")
        if has_data:
            hint = "Данные уже есть. Можно дополнительно перенести из другой папки."
        ttk.Label(mig, text=hint, wraplength=700, justify="left").pack(anchor="w")
        row = ttk.Frame(mig); row.pack(fill="x", pady=(6, 0))
        self.old_var = tk.StringVar(value=str(self.old[0]) if self.old else "")
        ttk.Combobox(row, textvariable=self.old_var, values=[str(p) for p in self.old], width=70).pack(side="left")
        ttk.Button(row, text="Обзор…", command=self.browse).pack(side="left", padx=4)
        ttk.Button(row, text="Перенести", command=self.migrate).pack(side="left")
        self.mig_status = ttk.Label(mig, text="")
        self.mig_status.pack(anchor="w", pady=(4, 0))

        # 2. Проверка записи
        chk = ttk.LabelFrame(box, text="2. Проверка записи", padding=10)
        chk.pack(fill="both", expand=True, pady=(0, 10))
        ttk.Label(chk, wraplength=700, justify="left", text=(
            "Нажмите «Проверить»: 5 секунд говорите в микрофон и включите любое видео со звуком. "
            "Если что-то мешает записи, здесь появится инструкция именно для вашего случая.")).pack(anchor="w")
        row2 = ttk.Frame(chk); row2.pack(fill="x", pady=6)
        self.check_btn = ttk.Button(row2, text="Проверить", command=self.run_check)
        self.check_btn.pack(side="left")
        self.settings_btn = ttk.Button(row2, text="Открыть настройки Windows", command=self.open_settings)
        self.support_btn = ttk.Button(row2, text="Сохранить отчёт для поддержки…", command=app.save_support_bundle)
        self.support_btn.pack(side="right")
        self.result = tk.Text(chk, height=12, wrap="word", font=("Segoe UI", 10))
        self.result.pack(fill="both", expand=True)
        self.result.insert("1.0", "Проверка ещё не запускалась.")
        self.result.config(state="disabled")
        self.settings_uri = ""

        # 3. Согласие на запись
        cons = ttk.LabelFrame(box, text="3. Запись встреч", padding=10)
        cons.pack(fill="x")
        self.consent = tk.BooleanVar(value=bool(app.settings.get("recording_consent")))
        ttk.Checkbutton(cons, variable=self.consent, text=(
            "Я буду предупреждать участников встреч о записи (запись включается вместе со встречей)")).pack(anchor="w")
        ttk.Label(cons, foreground="#666", wraplength=700, justify="left", text=(
            "Без этой отметки встречи обнаруживаются, но не записываются. Изменить можно в любой момент "
            "галочкой «Записывать встречи».")).pack(anchor="w")

        ttk.Button(box, text="Готово", command=self.finish).pack(side="right", pady=(12, 0))

    def browse(self):
        folder = filedialog.askdirectory(parent=self, title="Папка предыдущей версии Meeting Hub")
        if folder:
            self.old_var.set(folder)

    def migrate(self):
        folder = self.old_var.get().strip()
        if not folder or not Path(folder).is_dir():
            self.mig_status.config(text="Укажите папку предыдущей версии.")
            return
        try:
            done = support.import_from(folder, self.app.data_dir)
        except OSError as e:
            self.mig_status.config(text=f"Не удалось перенести: {e}")
            return
        if not done:
            self.mig_status.config(text="Переносить нечего: данные уже есть или папка пуста.")
            return
        self.app.reload_data()
        self.app._journal_note("Данные перенесены", f"Из папки {folder}: " + ", ".join(done), "user")
        self.mig_status.config(text="Перенесено: " + ", ".join(done))

    def set_result(self, text):
        self.result.config(state="normal")
        self.result.delete("1.0", "end")
        self.result.insert("1.0", text)
        self.result.config(state="disabled")

    def run_check(self):
        if self.app.recorder is not None:
            self.set_result("Сейчас идёт запись встречи. Проверку можно запустить после неё.")
            return
        self.check_btn.config(state="disabled", text="Проверяю… говорите и включите звук")
        self.settings_btn.pack_forget()
        self.set_result("Идёт проверка, 5 секунд…")

        def work():
            try:
                res = rec_mod.self_test(5.0, rec_mod.AutoBackend())
            except Exception as e:
                res = {"error": rec_mod.describe_error(e)}
            code, guide = self.app.diagnose(res)
            self.app._ui_calls.put((self.check_done, (res, code, guide)))
        threading.Thread(target=work, name="first-run-check", daemon=True).start()

    def check_done(self, res, code, guide):
        if not self.winfo_exists():
            return
        self.check_btn.config(state="normal", text="Проверить ещё раз")
        lines = []
        for name, label in (("mic", "Микрофон (вы)"), ("system", "Звук компьютера (собеседники)")):
            info = res.get(name, {"error": res.get("error", "")})
            if info.get("error"):
                lines.append(f"✗ {label}: не записывается")
            else:
                loud = "слышно" if info.get("peak", 0) > 0.02 else "тишина — говорите / включите звук громче"
                lines.append(f"✓ {label}: {info.get('device', '')} — {loud}")
        if guide:
            lines += ["", support.guide_text(guide)]
            self.settings_uri = guide.get("settings_uri", "")
            if self.settings_uri:
                self.settings_btn.pack(side="left", padx=6)
        else:
            lines += ["", "Запись работает."]
        self.set_result("\n".join(lines))
        self.app.log.info("first-run check: %s; diagnosis: %s", res, code)
        self.app._journal_note("Проверка записи (мастер)", "; ".join(l for l in lines[:2]), "user")

    def open_settings(self):
        if self.settings_uri:
            try:
                os.startfile(self.settings_uri)  # type: ignore[attr-defined]
            except (OSError, AttributeError):
                pass

    def finish(self):
        consent = bool(self.consent.get())
        self.app.settings["recording_consent"] = consent
        if not consent:
            self.app.settings["record_meetings"] = False
            self.app.record_meetings.set(False)
        self.app.settings["first_run_done"] = True
        self.app.save_settings()
        self.app.wizard = None
        self.destroy()


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"Meeting Hub v{APP_VERSION}")
        root.geometry("1360x900")
        here = data_dir()
        self.data_dir = here
        self.log = setup_logging(here)
        imported = []
        if app_dir() != here and support.has_user_data(app_dir()) and \
                not (here / "meeting_hub_training.json").exists():
            # Пользователь положил данные старой версии рядом с MeetingHub.exe — забираем их в профиль.
            try:
                imported = support.import_from(app_dir(), here)
            except OSError as e:
                self.log.warning("import from app dir failed: %s", e)
        self.project_path = ""
        self.transcript_path = ""
        self.store = TrainingStore(here / "meeting_hub_training.json")
        self.detector = MeetingDetector(self.store.meeting_patterns(), self.store.ignore_patterns())
        self.prompt = None
        self.last_obs = None
        self.last_event_text = ""
        self._tick_job = None
        self.data = {"summary": [], "decisions": [], "tasks": [], "open_questions": [], "uncertain_fragments": []}
        self.analysis_cancel = threading.Event()
        self.analysis_running = False
        self.analysis_source = None
        self.pending_transcript = None
        self.settings_path = here / "meeting_hub_settings.json"
        self.settings = self.load_settings()
        self.rec_folder = here / "recordings"
        self.recorder = None
        self.rec_by_meeting = False
        self.rec_errors_seen = set()
        self._saving_threads = []
        self._ui_calls = queue.Queue()   # результаты фоновых потоков выполняются в потоке интерфейса
        self.wizard = None

        header = ttk.Frame(root, padding=10); header.pack(fill="x")
        ttk.Label(header, text="MEETING HUB", font=("Segoe UI", 18, "bold")).pack(side="left")
        ttk.Button(header, text="Открыть TXT", command=self.open_txt).pack(side="right")
        ttk.Button(header, text="Настройки…", command=self.open_settings_dialog).pack(side="right", padx=(5, 15))
        ttk.Button(header, text="Сохранить проект", command=self.save_project).pack(side="right", padx=5)
        ttk.Button(header, text="Открыть проект", command=self.open_project).pack(side="right", padx=5)

        meta = ttk.LabelFrame(root, text="Встреча", padding=8); meta.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(meta, text="Название").grid(row=0, column=0, sticky="w")
        self.name = ttk.Entry(meta, width=38); self.name.grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Label(meta, text="Дата").grid(row=0, column=2, sticky="w")
        self.date = ttk.Entry(meta, width=15); self.date.insert(0, datetime.now().strftime("%Y-%m-%d")); self.date.grid(row=0, column=3, padx=5)
        ttk.Label(meta, text="Модель Ollama").grid(row=0, column=4, sticky="w")
        self.model = ttk.Entry(meta, width=24); self.model.insert(0, DEFAULT_MODEL); self.model.grid(row=0, column=5, padx=5)
        ttk.Button(meta, text="Проверить Ollama", command=self.check_ollama).grid(row=0, column=6, padx=5)
        meta.columnconfigure(1, weight=1)

        livebar = ttk.Frame(root, padding=(12, 0, 12, 4)); livebar.pack(fill="x")
        self.auto_detect = tk.BooleanVar(value=True)
        self.auto_pause = tk.BooleanVar(value=True)
        ttk.Checkbutton(livebar, text="Обнаруживать встречи автоматически", variable=self.auto_detect,
                        command=self.toggle_auto_detect).pack(side="left")
        ttk.Checkbutton(livebar, text="Останавливать ИИ во время встречи", variable=self.auto_pause,
                        command=self.refresh_analysis_button).pack(side="left", padx=12)
        self.record_meetings = tk.BooleanVar(value=bool(self.settings.get("record_meetings", True)))
        ttk.Checkbutton(livebar, text="Записывать встречи", variable=self.record_meetings,
                        command=self.toggle_record_setting).pack(side="left")
        self.auto_transcribe = tk.BooleanVar(value=bool(self.settings.get("auto_transcribe", False)))
        ttk.Checkbutton(livebar, text="Расшифровывать записи", variable=self.auto_transcribe,
                        command=self.toggle_transcribe_setting).pack(side="left", padx=12)
        self.auto_summary = tk.BooleanVar(value=bool(self.settings.get("auto_summary", False)))
        ttk.Checkbutton(livebar, text="Сразу делать итоги", variable=self.auto_summary,
                        command=self.toggle_summary_setting).pack(side="left")
        ttk.Button(livebar, text="Завершить встречу", command=lambda: self.user_action("manual_end")).pack(side="right")
        ttk.Button(livebar, text="Начать встречу", command=lambda: self.user_action("manual_start")).pack(side="right", padx=6)
        self.rec_btn = ttk.Button(livebar, text="● Записать", command=self.toggle_recording_button)
        self.rec_btn.pack(side="right", padx=(0, 18))

        self.status = ttk.Label(root, text="Запуск…", padding=(12, 0, 12, 2), font=("Segoe UI", 10, "bold"))
        self.status.pack(fill="x")
        self.substatus = ttk.Label(root, text="", padding=(12, 0, 12, 8))
        self.substatus.pack(fill="x")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        nb = self.nb = ttk.Notebook(root); nb.pack(fill="both", expand=True, padx=10, pady=4)
        self.tab_trans = ttk.Frame(nb, padding=8); self.tab_summary = ttk.Frame(nb, padding=8)
        self.tab_dec = ttk.Frame(nb, padding=8); self.tab_tasks = ttk.Frame(nb, padding=8)
        self.tab_questions = ttk.Frame(nb, padding=8); self.tab_training = ttk.Frame(nb, padding=8)
        self.tab_records = ttk.Frame(nb, padding=8)
        nb.add(self.tab_trans, text="Расшифровка"); nb.add(self.tab_summary, text="Резюме")
        nb.add(self.tab_dec, text="Решения"); nb.add(self.tab_tasks, text="Задачи")
        nb.add(self.tab_questions, text="Открытые вопросы"); nb.add(self.tab_training, text="Обучение")
        nb.add(self.tab_records, text="Записи")
        self.trans = tk.Text(self.tab_trans, wrap="word", font=("Segoe UI", 10)); self.trans.pack(fill="both", expand=True)
        tbar = ttk.Frame(self.tab_trans); tbar.pack(fill="x", pady=5)
        ttk.Button(tbar, text="Открыть TXT", command=self.open_txt).pack(side="left")
        self.analyze_btn = ttk.Button(tbar, text="Сформировать итоги локально", command=self.analyze)
        self.analyze_btn.pack(side="left", padx=6)
        self.summary = tk.Text(self.tab_summary, wrap="word", font=("Segoe UI", 10)); self.summary.pack(fill="both", expand=True)
        self.dec_tree = self.make_tree(self.tab_dec, [("text", "Решение", 650), ("evidence", "Фрагмент-основание", 500)])
        self.task_tree = self.make_tree(self.tab_tasks, [("task", "Задача", 450), ("owner", "Ответственный", 130), ("deadline", "Срок", 100), ("confidence", "Уверенность", 90), ("status", "Статус", 130), ("evidence", "Фрагмент-основание", 350)])
        taskbar = ttk.Frame(self.tab_tasks); taskbar.pack(fill="x", pady=5)
        ttk.Button(taskbar, text="Подтвердить выбранную", command=lambda: self.task_status("Подтверждена")).pack(side="left")
        ttk.Button(taskbar, text="Отклонить выбранную", command=lambda: self.task_status("Отклонена")).pack(side="left", padx=5)
        ttk.Button(taskbar, text="Экспорт CSV", command=self.export_tasks).pack(side="right")
        self.q_tree = self.make_tree(self.tab_questions, [("question", "Открытый вопрос", 650), ("evidence", "Фрагмент-основание", 500)])
        ttk.Label(root, text="ИИ формирует черновик. Проверяйте решения, поручения, имена, суммы и сроки по записи.", padding=10).pack(fill="x")
        self.build_training_panel()
        self.build_records_panel()
        self.refresh_status()
        if self.store.notices:
            self.last_event_text = " ".join(self.store.notices)
            self.log.info("store: %s", self.last_event_text)
        if imported:
            self._journal_note("Данные перенесены", "Из папки программы в профиль пользователя: " + ", ".join(imported))
        if not self.settings.get("first_run_done"):
            self.root.after(600, self.open_first_run)
        self.tray = None
        self.start_tray()
        self.root.after(10 * 60 * 1000, self.housekeeping)                # хранение: через 10 мин после старта
        self.root.after(20 * 1000, lambda: self.check_updates(manual=False))
        self._tick_job = self.root.after(800, self.tick)
        threading.Thread(target=self._recover_recordings, daemon=True).start()

    # ------------------------------------------------------------------
    # Цикл обнаружения
    # ------------------------------------------------------------------
    def drain_ui_calls(self):
        while True:
            try:
                fn, args = self._ui_calls.get_nowait()
            except queue.Empty:
                return
            try:
                fn(*args)
            except Exception:
                self.log.exception("Ошибка обработки фонового результата")

    def tick(self):
        self._tick_job = None
        self.drain_ui_calls()
        try:
            obs = win_signals.observe()
            self.last_obs = obs
            if self.auto_detect.get():
                for decision in self.detector.update(obs):
                    self.apply_decision(decision)
        except Exception:
            self.log.exception("Ошибка цикла обнаружения")
            self.last_event_text = "Ошибка цикла обнаружения — подробности в meeting_hub.log"
        finally:
            tq = getattr(self, "tq", None)
            if tq is not None:
                tq.set_paused(self.detector.blocks_analysis)
                if tq.busy() and time.time() - getattr(self, "_tr_refresh", 0) > 2:
                    self._tr_refresh = time.time()
                    self.refresh_records()
            self.refresh_status()
            self.refresh_prompt()
            if self.root.winfo_exists():
                self._tick_job = self.root.after(TICK_MS, self.tick)

    def apply_decision(self, decision):
        entry = self.store.log(decision.as_dict())
        self.log.info("%s -> %s | %s | %s | %s %s", decision.prev, decision.new, decision.initiator,
                      decision.reason, decision.app, decision.title)
        self.last_event_text = f"{decision.time[-8:]} · {decision.event}: {decision.reason}"
        self._insert_journal_row(entry, select=True)
        if decision.new in (COUNTDOWN, IN_MEETING, ENDING) and self.auto_pause.get() and self.analysis_running:
            self.analysis_cancel.set()
        if decision.new == IN_MEETING and decision.prev not in (IN_MEETING, ENDING):
            if self.recorder is not None:
                self.rec_by_meeting = True
            elif self.record_meetings.get():
                self.start_recording("Запись встречи", decision.initiator, by_meeting=True)
        if decision.new == IDLE and decision.prev in (IN_MEETING, ENDING):
            if self.recorder is not None and self.rec_by_meeting:
                cut_at = None
                if decision.prev == ENDING and self.detector.last_active_at:
                    cut_at = self.detector.last_active_at + 2.0   # тишину после ухода со встречи не храним
                self.stop_recording("Встреча завершена", decision.initiator, cut_at=cut_at)
            self.root.after(500, self.resume_after_meeting)
        self.refresh_status()
        self.refresh_prompt()

    def user_action(self, method, *args):
        decision = getattr(self.detector, method)(*args)
        if decision is not None:
            self.apply_decision(decision)
        self.refresh_status()
        self.refresh_prompt()

    def toggle_auto_detect(self):
        if not self.auto_detect.get() and self.detector.state in (COUNTDOWN, ASKING):
            self.user_action("cancel_countdown" if self.detector.state == COUNTDOWN else "answer_ask", *(
                () if self.detector.state == COUNTDOWN else (False,)))
        self.refresh_status()

    def refresh_prompt(self):
        state = self.detector.state
        kind = state if state in (COUNTDOWN, ASKING, ENDING) else None
        if self.prompt is not None and (kind != self.prompt.kind or not self.prompt.winfo_exists()):
            try:
                self.prompt.destroy()
            except tk.TclError:
                pass
            self.prompt = None
        if kind is None:
            return
        if self.prompt is None:
            self.prompt = MeetingPrompt(self, kind)
        left = self.detector.seconds_left(time.time())
        cand = self.detector.candidate
        what = cand.describe() if cand else ""
        if kind == COUNTDOWN:
            self.prompt.set_text(f"Обнаружена встреча: {cand.label if cand else ''}",
                                 f"{what}.\nРежим встречи{' и запись звука' if self.record_meetings.get() else ''} "
                                 f"включится через {left} с. Локальный ИИ будет приостановлен.")
        elif kind == ASKING:
            self.prompt.set_text("Это встреча?", f"{what}.\nВопрос закроется через {left} с.")
        elif kind == ENDING:
            self.prompt.set_text("Встреча закончилась?",
                                 f"Окно встречи закрыто и микрофон свободен.\nЗавершу режим встречи через {left} с.")

    def refresh_status(self):
        st = self.detector.state
        cand = self.detector.candidate
        place = (cand.label or cand.app) if cand and cand.kind != "manual" else ""
        parts = [STATE_LABELS.get(st, st)]
        if place:
            parts.append(place)
        if not self.auto_detect.get():
            parts.append("автообнаружение выключено")
        if self.recorder is not None:
            sec = int(self.recorder.elapsed())
            parts.append(f"● ЗАПИСЬ {sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}")
        elif self._saving_threads:
            parts.append("сохраняю запись…")
        tq = getattr(self, "tq", None)
        if tq is not None and tq.busy():
            downloading = any(x["state"] == "downloading" for x in tq.status.values())
            parts.append("расшифровка: пауза" if tq.paused.is_set() else
                         "скачиваю модель расшифровки" if downloading else "расшифровка идёт")
        if self.analysis_running:
            parts.append("ИИ: анализ идёт")
        elif self.pending_transcript:
            parts.append("ИИ: анализ отложен до конца встречи")
        elif self.detector.blocks_analysis and self.auto_pause.get():
            parts.append("ИИ: приостановлен")
        else:
            parts.append("ИИ: доступен")
        try:
            self.status.config(text=" · ".join(parts))
            if self.tray is not None:
                self.tray.update("Meeting Hub · " + " · ".join(parts[:2]))
            obs = self.last_obs
            mic = "микрофон: нет данных" if obs is None or obs.mic is None else (
                "микрофон занят: " + ", ".join(sorted(obs.mic)) if obs.mic else "микрофон свободен")
            self.substatus.config(text=(self.last_event_text + "   |   " if self.last_event_text else "") + mic
                                  + self.recording_levels_text())
            self.rec_btn.config(text="■ Остановить запись" if self.recorder is not None else "● Записать")
        except tk.TclError:
            pass
        self.check_recording_health()
        self.refresh_analysis_button()

    def refresh_analysis_button(self):
        try:
            blocked = self.analysis_running or bool(self.pending_transcript) or \
                (self.auto_pause.get() and self.detector.blocks_analysis)
            self.analyze_btn.config(state="disabled" if blocked else "normal")
        except (tk.TclError, AttributeError):
            pass

    # ------------------------------------------------------------------
    # Запись встречи
    # ------------------------------------------------------------------
    def load_settings(self):
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_settings(self):
        try:
            self.settings_path.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            self.log.warning("settings not saved: %s", e)

    # ------------------------------------------------------------------
    # Расшифровка записей
    # ------------------------------------------------------------------
    def transcribe_queue(self):
        if getattr(self, "tq", None) is None:
            folder = self.data_dir / "models"

            def factory():
                return tr_mod.LocalWhisper(folder, size=self.settings.get("whisper_model", tr_mod.DEFAULT_MODEL))

            self.tq = tr_mod.TranscriptionQueue(
                factory, language=self.settings.get("language", "ru"), log=self.log,
                on_done=lambda key, st: self._ui_calls.put((self._transcribed, (key, st))))
        return self.tq

    def toggle_transcribe_setting(self):
        on = bool(self.auto_transcribe.get())
        if on:
            ok, why = tr_mod.LocalWhisper.available()
            if not ok:
                messagebox.showerror("Расшифровка", why)
                self.auto_transcribe.set(False)
                on = False
            elif not messagebox.askokcancel(
                    "Расшифровка записей",
                    "Записи будут расшифровываться на этом компьютере, звук никуда не отправляется.\n\n"
                    "При первой расшифровке один раз скачается модель распознавания речи "
                    f"(«{self.settings.get('whisper_model', tr_mod.DEFAULT_MODEL)}», до 0,5 ГБ) — нужен интернет.\n"
                    "Во время встреч расшифровка ставится на паузу.", parent=self.root):
                self.auto_transcribe.set(False)
                on = False
        self.settings["auto_transcribe"] = on
        self.save_settings()
        self.last_event_text = "Записи будут расшифровываться автоматически." if on else \
            "Автоматическая расшифровка выключена. Кнопка «Расшифровать» на вкладке «Записи» работает."
        self.refresh_status()

    def transcribe_files(self, paths):
        ok, why = tr_mod.LocalWhisper.available()
        if not ok:
            messagebox.showerror("Расшифровка", why)
            return
        q = self.transcribe_queue()
        q.set_paused(self.detector.blocks_analysis)
        added = [p for p in paths if q.add(p)]
        if added:
            self._journal_note("Расшифровка поставлена в очередь", f"Файлов: {len(added)}", "user")
        self.refresh_records()
        self.refresh_status()

    def toggle_summary_setting(self):
        self.settings["auto_summary"] = bool(self.auto_summary.get())
        self.save_settings()
        self.last_event_text = ("После расшифровки итоги встречи будут готовиться автоматически (нужна Ollama)."
                                if self.auto_summary.get() else "Автоматические итоги выключены.")
        self.refresh_status()

    @staticmethod
    def summary_path(wav):
        wav = Path(wav)
        return wav.with_name(wav.stem + ".summary.json")

    def summarize_selected(self):
        sel = self.records_tree.selection()
        if not sel:
            messagebox.showinfo("Записи", "Сначала выберите запись.")
            return
        if not tr_mod.has_transcript(sel[0]):
            messagebox.showinfo("Итоги встречи", "Для этой записи ещё нет расшифровки. Нажмите «Расшифровать», "
                                "после неё можно получить итоги.")
            return
        self.open_summary_for(sel[0], rerun=False)

    def open_summary_for(self, wav, rerun=False, automatic=False):
        """Показать итоги записи; если их нет (или rerun) — запустить анализ расшифровки."""
        wav = Path(wav)
        txt = tr_mod.output_paths(wav)[0]
        try:
            text = txt.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as e:
            messagebox.showerror("Итоги встречи", f"Не удалось прочитать расшифровку: {e}")
            return
        meta = {}
        try:
            meta = json.loads(wav.with_suffix(".json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        self.transcript_path = str(txt)
        self.trans.delete("1.0", "end")
        self.trans.insert("1.0", text)
        self.name.delete(0, "end")
        self.name.insert(0, meta.get("window") or meta.get("meeting") or wav.stem)
        stem = wav.stem
        if len(stem) >= 8 and stem[:8].isdigit():
            self.date.delete(0, "end")
            self.date.insert(0, f"{stem[0:4]}-{stem[4:6]}-{stem[6:8]}")
        sp = self.summary_path(wav)
        if sp.exists() and not rerun:
            try:
                self.show_result(json.loads(sp.read_text(encoding="utf-8")))
                self.nb.select(self.tab_summary)
                return
            except (OSError, ValueError):
                pass
        if self.auto_pause.get() and self.detector.blocks_analysis and not automatic:
            messagebox.showwarning("Итоги встречи", "Сейчас идёт встреча — итоги подготовятся после неё.")
        self.analysis_source = wav
        if self.auto_pause.get() and self.detector.blocks_analysis:
            self.pending_transcript = (text, self.model.get().strip() or DEFAULT_MODEL)
            self.last_event_text = "Итоги встречи подготовятся после её окончания."
            self.refresh_status()
        else:
            self.start_analysis(text, self.model.get().strip() or DEFAULT_MODEL, automatic=automatic)
        if not automatic:
            self.nb.select(self.tab_summary)

    def make_protocol(self, wav, notify=""):
        try:
            path = report_mod.write_protocol(wav)
        except Exception:
            self.log.exception("Протокол не создан")
            return None
        if path is not None:
            self._journal_note("Протокол готов", path.name, "auto", file=path.name)
            if notify:
                self.toast(notify, path)
        self.refresh_records()
        return path

    def open_protocol(self):
        sel = self.records_tree.selection()
        if not sel:
            messagebox.showinfo("Записи", "Сначала выберите запись.")
            return
        path = report_mod.protocol_paths(sel[0])[0]
        if not path.exists():
            path = self.make_protocol(sel[0])
        if path is None:
            messagebox.showinfo("Протокол", "Для этой записи ещё нет ни расшифровки, ни итогов. "
                                "Нажмите «Расшифровать».")
            return
        self.open_path(path)

    def open_path(self, path):
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except (OSError, AttributeError) as e:
            messagebox.showerror("Открыть файл", f"Не удалось открыть: {e}")

    def toast(self, text, path=None, seconds=25):
        """Небольшое окно в углу экрана: не мешает, закрывается само."""
        try:
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            frame = ttk.Frame(win, padding=12, relief="solid")
            frame.pack()
            ttk.Label(frame, text=text, font=("Segoe UI", 10, "bold"), wraplength=320).pack(anchor="w")
            row = ttk.Frame(frame); row.pack(anchor="e", pady=(8, 0))
            if path is not None:
                ttk.Button(row, text="Открыть", command=lambda: (self.open_path(path), win.destroy())).pack(side="left")
            ttk.Button(row, text="Закрыть", command=win.destroy).pack(side="left", padx=(6, 0))
            win.update_idletasks()
            x = win.winfo_screenwidth() - win.winfo_width() - 24
            y = win.winfo_screenheight() - win.winfo_height() - 72
            win.geometry(f"+{x}+{y}")
            win.after(seconds * 1000, lambda: win.winfo_exists() and win.destroy())
        except tk.TclError:
            pass

    def toggle_full_mode(self):
        on = not (self.auto_transcribe.get() and self.auto_summary.get() and self.record_meetings.get())
        if on:
            ok, why = tr_mod.LocalWhisper.available()
            if not ok:
                messagebox.showerror("Полный режим", why)
                return
            if not messagebox.askokcancel(
                    "Полный режим",
                    "Каждая встреча будет записана, расшифрована на этом компьютере и превращена в итоги и "
                    "протокол — без ваших действий.\n\nПри первой расшифровке один раз скачается модель "
                    "распознавания речи (до 0,5 ГБ). Для итогов нужна Ollama (setup_ollama.bat).\n"
                    "Участников встречи нужно предупреждать о записи.", parent=self.root):
                return
        self.record_meetings.set(on)
        self.auto_transcribe.set(on)
        self.auto_summary.set(on)
        self.settings.update(record_meetings=on, auto_transcribe=on, auto_summary=on)
        self.save_settings()
        self.last_event_text = ("Полный режим включён: запись → расшифровка → итоги → протокол." if on
                                else "Полный режим выключен.")
        self.refresh_status()

    def transcribe_selected(self):
        sel = self.records_tree.selection()
        if not sel:
            messagebox.showinfo("Записи", "Сначала выберите запись.")
            return
        self.transcribe_files(list(sel))

    def open_transcript(self):
        sel = self.records_tree.selection()
        if not sel:
            messagebox.showinfo("Записи", "Сначала выберите запись.")
            return
        txt = tr_mod.output_paths(sel[0])[0]
        if not txt.exists():
            messagebox.showinfo("Записи", "Для этой записи расшифровки ещё нет. Нажмите «Расшифровать».")
            return
        try:
            os.startfile(str(txt))  # type: ignore[attr-defined]
        except (OSError, AttributeError) as e:
            messagebox.showerror("Записи", f"Не удалось открыть файл: {e}")

    def cancel_transcription(self):
        if getattr(self, "tq", None) is not None:
            self.tq.cancel()
            for k, st in self.tq.status.items():
                if st["state"] == "queued":
                    st["state"] = "cancelled"
        self.refresh_records()

    def _transcribed(self, key, st):
        name = Path(key).name
        if st["state"] == "done":
            self._journal_note("Расшифровка готова", name, "auto", file=name)
            self.last_event_text = f"Расшифровка готова: {Path(st['result']).name}"
            if self.auto_summary.get() and not self.analysis_running and not self.pending_transcript:
                self.open_summary_for(key, rerun=False, automatic=True)
            elif self.settings.get("auto_protocol", True):
                self.make_protocol(key, notify="Расшифровка и протокол готовы")
        elif st["state"] == "error":
            self._journal_note("Ошибка расшифровки", f"{name}: {st['error']}", "auto", file=name)
            self.last_event_text = f"Ошибка расшифровки: {st['error']}"
        self.refresh_records()
        self.refresh_status()

    def transcript_state_text(self, wav):
        st = getattr(self, "tq", None) and self.tq.status.get(str(wav))
        if st:
            s = st["state"]
            if s == "queued":
                return "в очереди"
            if s == "downloading":
                mb = int(st.get("downloaded_mb") or 0)
                total = tr_mod.MODEL_MB.get(self.settings.get("whisper_model", tr_mod.DEFAULT_MODEL), 0)
                return f"скачиваю модель {mb} из {total} МБ (один раз)"
            if s == "loading":
                return "загружаю модель…"
            if s == "running":
                return f"расшифровка {int(st['progress'] * 100)}%"
            if s == "paused":
                return "пауза (идёт встреча)"
            if s == "error":
                return "ошибка: " + st["error"][:60]
            if s == "cancelled" and not tr_mod.has_transcript(wav):
                return "отменена"
        if tr_mod.has_transcript(wav):
            if report_mod.protocol_paths(wav)[0].exists():
                return "готова, есть протокол"
            return "готова, есть итоги" if self.summary_path(wav).exists() else "готова"
        return ""

    # ------------------------------------------------------------------
    # Повседневная работа: настройки, хранение, обновления
    # ------------------------------------------------------------------
    KEEP_CHOICES = [("всегда", 0), ("7 дней", 7), ("14 дней", 14), ("30 дней", 30), ("90 дней", 90), ("180 дней", 180), ("365 дней", 365)]

    def open_settings_dialog(self):
        win = tk.Toplevel(self.root)
        win.title("Meeting Hub — настройки")
        win.transient(self.root)
        win.resizable(False, False)
        box = ttk.Frame(win, padding=16)
        box.pack(fill="both", expand=True)

        auto = tk.BooleanVar(value=daily.autostart_enabled())
        tray_var = tk.BooleanVar(value=bool(self.settings.get("close_to_tray", True)))
        upd = tk.BooleanVar(value=bool(self.settings.get("check_updates", True)))
        keep_labels = [k for k, _ in self.KEEP_CHOICES]
        keep_now = int(self.settings.get("keep_audio_days", 0))
        keep = tk.StringVar(value=next((k for k, v in self.KEEP_CHOICES if v == keep_now), "всегда"))
        model = tk.StringVar(value=self.settings.get("whisper_model", tr_mod.DEFAULT_MODEL))
        lang = tk.StringVar(value=self.settings.get("language", "ru"))

        row = 0
        ttk.Label(box, text="Работа в фоне", font=("Segoe UI", 10, "bold")).grid(row=row, column=0, sticky="w"); row += 1
        cb = ttk.Checkbutton(box, text="Запускать вместе с Windows (свёрнутой)", variable=auto)
        cb.grid(row=row, column=0, columnspan=2, sticky="w"); row += 1
        if not daily.autostart_supported():
            cb.state(["disabled"])
            ttk.Label(box, text="Доступно в установленной программе (MeetingHub.exe).",
                      foreground="gray").grid(row=row, column=0, columnspan=2, sticky="w"); row += 1
        cb2 = ttk.Checkbutton(box, text="Крестик окна сворачивает в значок у часов", variable=tray_var)
        cb2.grid(row=row, column=0, columnspan=2, sticky="w"); row += 1
        if self.tray is None:
            cb2.state(["disabled"])

        ttk.Label(box, text="Записи", font=("Segoe UI", 10, "bold")).grid(row=row, column=0, sticky="w", pady=(12, 0)); row += 1
        ttk.Label(box, text="Хранить аудио").grid(row=row, column=0, sticky="w")
        ttk.Combobox(box, textvariable=keep, values=keep_labels, state="readonly", width=12).grid(row=row, column=1, sticky="w"); row += 1
        ttk.Label(box, wraplength=420, foreground="gray", text=(
            "По умолчанию аудио хранится всегда. Если выбрать срок, удаляется только аудио старше него и только с готовой расшифровкой; тексты, итоги и протоколы хранятся всегда. "
            "Час записи занимает около 220 МБ.")).grid(row=row, column=0, columnspan=2, sticky="w"); row += 1

        ttk.Label(box, text="Расшифровка", font=("Segoe UI", 10, "bold")).grid(row=row, column=0, sticky="w", pady=(12, 0)); row += 1
        ttk.Label(box, text="Модель").grid(row=row, column=0, sticky="w")
        ttk.Combobox(box, textvariable=model, values=list(tr_mod.MODELS), state="readonly", width=12).grid(row=row, column=1, sticky="w"); row += 1
        hint = ttk.Label(box, wraplength=420, foreground="gray")
        hint.grid(row=row, column=0, columnspan=2, sticky="w"); row += 1
        model.trace_add("write", lambda *_: hint.config(text=tr_mod.MODELS.get(model.get(), "")))
        hint.config(text=tr_mod.MODELS.get(model.get(), ""))
        ttk.Label(box, text="Язык встреч").grid(row=row, column=0, sticky="w")
        ttk.Combobox(box, textvariable=lang, values=["ru", "en", "auto"], state="readonly", width=12).grid(row=row, column=1, sticky="w"); row += 1

        ttk.Label(box, text="Обновления", font=("Segoe UI", 10, "bold")).grid(row=row, column=0, sticky="w", pady=(12, 0)); row += 1
        ttk.Checkbutton(box, text="Проверять обновления при запуске", variable=upd).grid(row=row, column=0, sticky="w")
        ttk.Button(box, text="Проверить сейчас", command=lambda: self.check_updates(manual=True)).grid(row=row, column=1, sticky="w"); row += 1
        ttk.Label(box, foreground="gray", text=f"Версия {APP_VERSION} · данные: {self.data_dir}",
                  wraplength=420).grid(row=row, column=0, columnspan=2, sticky="w", pady=(12, 0)); row += 1

        def save():
            if daily.autostart_supported() and auto.get() != daily.autostart_enabled():
                try:
                    daily.set_autostart(auto.get())
                except OSError as e:
                    messagebox.showerror("Автозапуск", f"Не удалось изменить автозапуск: {e}", parent=win)
            old_model, old_lang = self.settings.get("whisper_model", tr_mod.DEFAULT_MODEL), self.settings.get("language", "ru")
            self.settings.update(close_to_tray=bool(tray_var.get()), check_updates=bool(upd.get()),
                                 keep_audio_days=dict(self.KEEP_CHOICES)[keep.get()],
                                 whisper_model=model.get(), language=lang.get())
            self.save_settings()
            tq = getattr(self, "tq", None)
            if tq is not None:
                tq.language = None if lang.get() == "auto" else lang.get()
                if (old_model != model.get() or old_lang != lang.get()) and not tq.busy():
                    tq.engine = None                      # новая модель загрузится при следующей расшифровке
            self.last_event_text = "Настройки сохранены."
            self.refresh_status()
            win.destroy()
        bar = ttk.Frame(box); bar.grid(row=row, column=0, columnspan=2, sticky="e", pady=(16, 0))
        ttk.Button(bar, text="Сохранить", command=save).pack(side="left")
        ttk.Button(bar, text="Отмена", command=win.destroy).pack(side="left", padx=(6, 0))

    def housekeeping(self):
        """Раз в сутки: удалить старое аудио, у которого есть расшифровка."""
        days = int(self.settings.get("keep_audio_days", 0))
        protect = []                                      # идущая запись — .part.wav и без расшифровки, её не трогаем
        tq = getattr(self, "tq", None)
        if tq is not None:
            protect += [k for k, st in tq.status.items() if st["state"] not in ("done", "error", "cancelled")]

        def work():
            try:
                removed, freed = daily.audio_cleanup(self.rec_folder, days, protect=protect)
            except Exception:
                self.log.exception("Очистка записей")
                return
            if removed:
                self._ui_calls.put((self._cleaned, (len(removed), freed, days)))
        threading.Thread(target=work, name="housekeeping", daemon=True).start()
        try:
            self.root.after(24 * 3600 * 1000, self.housekeeping)
        except tk.TclError:
            pass

    def _cleaned(self, count, freed, days):
        self._journal_note("Старое аудио удалено", f"Записей: {count}, освобождено {freed / 1048576:.0f} МБ; "
                           f"срок хранения {days} дн., тексты и протоколы сохранены", "auto")
        self.refresh_records()

    def check_updates(self, manual=False):
        if not manual and not self.settings.get("check_updates", True):
            return

        def work():
            try:
                found, error = daily.check_update(APP_VERSION), ""
            except Exception as e:
                found, error = None, str(e)
            self._ui_calls.put((self._update_checked, (found, error, manual)))
        threading.Thread(target=work, name="update-check", daemon=True).start()
        if not manual:
            try:
                self.root.after(24 * 3600 * 1000, lambda: self.check_updates(manual=False))
            except tk.TclError:
                pass

    def _update_checked(self, found, error, manual):
        if found is None:
            if manual:
                messagebox.showinfo("Обновления", f"Не удалось проверить: {error}" if error else
                                    f"Установлена последняя версия ({APP_VERSION}).")
            return
        version, url, page = found
        if self.settings.get("skip_version") == version and not manual:
            return
        self.last_event_text = f"Доступна новая версия {version}"
        self.refresh_status()
        try:
            hidden = self.root.state() in ("withdrawn", "iconic")
        except tk.TclError:
            return
        if hidden and not manual:
            # Не показываем диалог поверх работы: только уведомление у часов.
            if self.tray is not None:
                self.tray.notify(f"Доступна версия {version}. Настройки → «Проверить сейчас», чтобы обновиться.")
            return
        answer = messagebox.askyesnocancel(
            "Доступно обновление",
            f"Вышла версия Meeting Hub {version} (у вас {APP_VERSION}).\n\n"
            "«Да» — скачать установщик (запустите его, данные сохранятся).\n"
            "«Нет» — напомнить позже. «Отмена» — пропустить эту версию.")
        if answer:
            import webbrowser
            webbrowser.open(url or page)
        elif answer is None:
            self.settings["skip_version"] = version
            self.save_settings()

    def toggle_record_setting(self):
        self.settings["record_meetings"] = bool(self.record_meetings.get())
        self.save_settings()
        self.last_event_text = ("Встречи будут записываться автоматически." if self.record_meetings.get()
                                else "Автоматическая запись встреч выключена. Кнопка «● Записать» работает.")
        self.refresh_status()

    def _journal_note(self, event, reason, initiator="auto", **extra):
        entry = self.store.log({"prev": self.detector.state, "new": self.detector.state, "event": event,
                                "reason": reason, "initiator": initiator, **extra})
        self._insert_journal_row(entry, select=True)
        self.log.info("%s | %s | %s", event, reason, extra)

    def toggle_recording_button(self):
        if self.recorder is not None:
            self.stop_recording("Пользователь остановил запись", "user")
        else:
            self.start_recording("Пользователь начал запись", "user", by_meeting=self.detector.in_meeting)

    def ensure_recording_consent(self):
        if self.settings.get("recording_consent"):
            return True
        self.show_window()
        ok = messagebox.askyesno(
            "Запись встреч",
            "Meeting Hub записывает ваш голос и голоса собеседников в файл на этом компьютере.\n\n"
            "Во многих странах, в том числе в России, участников нужно предупреждать о записи "
            "разговора. Вы будете предупреждать участников встреч?\n\n"
            "«Да» — разрешить запись. «Нет» — запись выключена (её можно включить позже).",
            parent=self.root)
        self.settings["recording_consent"] = bool(ok)
        if not ok:
            self.settings["record_meetings"] = False
            self.record_meetings.set(False)
        self.save_settings()
        return ok

    def start_recording(self, reason, initiator="auto", by_meeting=False):
        if self.recorder is not None:
            return
        if not self.ensure_recording_consent():
            self._journal_note("Запись не начата", "Нет согласия с условием предупреждать участников", initiator)
            return
        cand = self.detector.candidate
        meta = {"meeting": (cand.label or cand.app) if cand and cand.kind != "manual" else "",
                "window": strip_browser_suffix(cand.title) if cand and cand.title else "",
                "started_by": WHO.get(initiator, initiator), "app_version": APP_VERSION}
        rec = rec_mod.AudioRecorder(self.rec_folder)
        try:
            rec.start(meta)
        except Exception as e:
            self.log.exception("Запись не запущена")
            self.last_event_text = f"Запись не запущена: {e}"
            self._journal_note("Ошибка записи", f"Запись не запущена: {e}", initiator)
            self.refresh_status()
            return
        self.recorder = rec
        self.rec_by_meeting = by_meeting
        self.rec_errors_seen = set()
        self._journal_note("Запись начата", reason, initiator, file=rec.stem + ".wav")
        self.last_event_text = "Запись начата. Левый канал — вы, правый — собеседники."
        self.refresh_status()

    def stop_recording(self, reason, initiator="auto", cut_at=None):
        rec = self.recorder
        if rec is None:
            return
        self.recorder = None
        self.rec_by_meeting = False

        def work():
            try:
                result = rec.stop(cut_at=cut_at)
                error = ""
            except Exception as e:
                self.log.exception("Ошибка сохранения записи")
                result, error = None, str(e)
            self._ui_calls.put((self._recording_saved, (result, error, reason, initiator, th)))

        th = threading.Thread(target=work, name="rec-save", daemon=True)
        self._saving_threads.append(th)
        th.start()
        self.refresh_status()

    def _recording_saved(self, result, error, reason, initiator, thread):
        if thread in self._saving_threads:
            self._saving_threads.remove(thread)
        if result is None:
            self.last_event_text = f"Ошибка сохранения записи: {error}"
            self._journal_note("Ошибка записи", self.last_event_text, initiator)
        else:
            dur = int(result["duration"])
            note = f"{reason}. Файл {result['path'].name}, {dur // 60} мин {dur % 60} с"
            if result["trimmed"] >= 1:
                note += f"; убрано {int(result['trimmed'])} с тишины после встречи"
            if result["errors"]:
                note += ". Предупреждения: " + "; ".join(result["errors"])
            self.last_event_text = "Запись сохранена: " + result["path"].name
            self._journal_note("Запись сохранена", note, initiator, file=result["path"].name)
            if self.auto_transcribe.get() and tr_mod.LocalWhisper.available()[0]:
                q = self.transcribe_queue()
                q.set_paused(self.detector.blocks_analysis)
                q.add(result["path"])
        self.refresh_records()
        self.refresh_status()

    def recording_levels_text(self):
        if self.recorder is None:
            return ""
        def bar(v):
            n = max(0, min(5, int(round(v * 5))))
            return "▮" * n + "▯" * (5 - n)
        h = self.recorder.health()
        parts = []
        for name, label in (("mic", "вы"), ("system", "собеседники")):
            info = h.get(name, {})
            if info.get("error"):
                parts.append(f"{label}: ошибка — см. журнал")
            elif not info.get("started"):
                parts.append(f"{label}: подключаю…")
            else:
                parts.append(f"{label} {bar(info.get('level', 0.0))}")
        return "   |   запись: " + " · ".join(parts)

    def check_recording_health(self):
        if self.recorder is None:
            return
        for name, info in self.recorder.health().items():
            err = info.get("error")
            if err and err not in self.rec_errors_seen:
                self.rec_errors_seen.add(err)
                hint = rec_mod.blocker_hint()
                if hint and hint not in self.rec_errors_seen:
                    self.rec_errors_seen.add(hint)
                    self._journal_note("Запись заблокирована", hint)
                self._journal_note("Ошибка записи", err)
                self.last_event_text = "Проблема с записью: " + err

    def _recover_recordings(self):
        try:
            recovered = rec_mod.recover_unfinished(self.rec_folder)
        except Exception:
            self.log.exception("Ошибка восстановления записей")
            return
        if recovered:
            names = ", ".join(p.name for p in recovered)
            def done():
                self._journal_note("Запись восстановлена",
                                   f"После неожиданного закрытия программы сведены записи: {names}")
                self.refresh_records()
            self._ui_calls.put((done, ()))

    # ------------------------------------------------------------------
    # Вкладка «Записи»
    # ------------------------------------------------------------------
    def build_records_panel(self):
        ok, why = rec_mod.available()
        info = ("Встреча записывается автоматически, если стоит галочка «Записывать встречи»; "
                "кнопка «● Записать» включает и выключает запись вручную. Файлы WAV хранятся только на этом "
                f"компьютере: {self.rec_folder}. Левый канал — ваш микрофон, правый — звук компьютера (собеседники). "
                "Предупреждайте участников встречи о записи.")
        if not ok:
            info += f"\n\nЗапись сейчас недоступна: {why}"
        ttk.Label(self.tab_records, text=info, wraplength=1200, justify="left").pack(anchor="w", pady=(0, 8))
        cols = [("when", "Дата и время", 150), ("duration", "Длительность", 100), ("meeting", "Встреча", 300),
                ("size", "Размер", 90), ("tr", "Расшифровка", 170), ("note", "Примечание", 360)]
        sbar = ttk.Frame(self.tab_records); sbar.pack(fill="x", pady=(0, 6))
        ttk.Label(sbar, text="Поиск по встречам:").pack(side="left")
        self.search_var = tk.StringVar()
        ent = ttk.Entry(sbar, textvariable=self.search_var, width=50)
        ent.pack(side="left", padx=6)
        ttk.Button(sbar, text="×", width=3, command=lambda: self.search_var.set("")).pack(side="left")
        self.search_info = ttk.Label(sbar, text="", foreground="gray")
        self.search_info.pack(side="left", padx=10)
        self._search_job = None

        def on_search(*_):
            if self._search_job is not None:
                self.root.after_cancel(self._search_job)
            self._search_job = self.root.after(300, self.refresh_records)
        self.search_var.trace_add("write", on_search)
        self.records_tree = ttk.Treeview(self.tab_records, columns=[c[0] for c in cols], show="headings", height=16)
        for col, label, width in cols:
            self.records_tree.heading(col, text=label)
            self.records_tree.column(col, width=width, anchor="w")
        self.records_tree.pack(fill="both", expand=True)
        self.records_tree.bind("<Double-1>", lambda _e: self.play_selected_record())
        bar = ttk.Frame(self.tab_records); bar.pack(fill="x", pady=8)
        ttk.Button(bar, text="Воспроизвести", command=self.play_selected_record).pack(side="left")
        ttk.Button(bar, text="Расшифровать", command=self.transcribe_selected).pack(side="left", padx=5)
        ttk.Button(bar, text="Открыть расшифровку", command=self.open_transcript).pack(side="left")
        ttk.Button(bar, text="Итоги встречи", command=self.summarize_selected).pack(side="left", padx=5)
        ttk.Button(bar, text="Протокол", command=self.open_protocol).pack(side="left")
        ttk.Button(bar, text="Отменить", command=self.cancel_transcription).pack(side="left", padx=5)
        ttk.Button(bar, text="Открыть папку", command=self.open_records_folder).pack(side="left", padx=5)
        ttk.Button(bar, text="Полный режим вкл/выкл", command=self.toggle_full_mode).pack(side="left", padx=15)
        ttk.Button(bar, text="Обновить", command=self.refresh_records).pack(side="left", padx=5)
        self.test_btn = ttk.Button(bar, text="Проверить запись (5 с)", command=self.test_recording)
        self.test_btn.pack(side="right")
        ttk.Button(bar, text="Подробная проверка звука…", command=self.full_audio_check).pack(side="right", padx=5)
        ttk.Button(bar, text="Отчёт для поддержки…", command=self.save_support_bundle).pack(side="right", padx=5)
        ttk.Button(bar, text="Мастер настройки…", command=self.open_first_run).pack(side="right", padx=5)
        self.refresh_records()

    def refresh_records(self):
        if not hasattr(self, "records_tree"):
            return
        self._search_job = None
        selected = self.records_tree.selection()
        self.records_tree.delete(*self.records_tree.get_children())
        query = self.search_var.get().strip() if hasattr(self, "search_var") else ""
        hits = daily.search(self.rec_folder, query) if query else None
        shown = 0
        for item in rec_mod.list_recordings(self.rec_folder):
            stem = item["path"].stem
            if hits is not None and stem not in hits:
                continue
            shown += 1
            when = f"{stem[6:8]}.{stem[4:6]}.{stem[0:4]} {stem[9:11]}:{stem[11:13]}" if len(stem) >= 13 else stem
            dur = int(item.get("duration_sec") or 0)
            meeting = item.get("meeting") or ""
            if item.get("window"):
                meeting = (meeting + " · " if meeting else "") + item["window"]
            notes = list(item.get("errors") or [])
            if item.get("status") == "recovered":
                notes.insert(0, "восстановлена после сбоя")
            if item.get("trimmed_sec", 0) >= 1:
                notes.append(f"убрано {int(item['trimmed_sec'])} с после встречи")
            if item.get("audio_deleted"):
                notes.insert(0, "аудио удалено по сроку хранения")
            self.records_tree.insert("", "end", iid=str(item["path"]), values=(
                when, f"{dur // 3600}:{dur % 3600 // 60:02d}:{dur % 60:02d}", meeting,
                f"{item['size'] / (1024 * 1024):.1f} МБ" if item["size"] else "—", self.transcript_state_text(item["path"]), "; ".join(notes)))
        if hasattr(self, "search_info"):
            self.search_info.config(text=f"найдено: {shown}" if query else "")
        keep = [i for i in selected if self.records_tree.exists(i)]
        if keep:
            self.records_tree.selection_set(keep)

    def test_recording(self):
        if self.recorder is not None:
            messagebox.showinfo("Проверка записи", "Сейчас идёт запись. Проверку можно запустить после неё.")
            return
        try:
            backend = rec_mod.AutoBackend()
        except rec_mod.RecorderError as e:
            messagebox.showerror("Проверка записи", str(e))
            return
        self.test_btn.config(state="disabled", text="Проверяю… говорите и включите звук")
        self.last_event_text = "Проверка записи: 5 секунд говорите в микрофон и включите любой звук на компьютере."
        self.refresh_status()

        def work():
            try:
                res = rec_mod.self_test(5.0, backend)
            except Exception as e:
                res = {"error": rec_mod.describe_error(e)}
            self._ui_calls.put((self._test_done, (res,)))
        threading.Thread(target=work, name="rec-test", daemon=True).start()

    def _test_done(self, res):
        self.test_btn.config(state="normal", text="Проверить запись (5 с)")
        if "error" in res and "mic" not in res:
            text = "Проверка не удалась: " + res["error"]
        else:
            lines = []
            for name, label in (("mic", "Микрофон (вы)"), ("system", "Звук компьютера (собеседники)")):
                info = res.get(name, {})
                lines.append(f"{label}\n  устройство: {info.get('device') or '—'}"
                             + (f" ({info['config']})" if info.get("config") else ""))
                if info.get("error"):
                    lines.append(f"  ОШИБКА: {info['error']}")
                else:
                    loud = "слышно" if info.get("peak", 0) > 0.02 else "тишина"
                    lines.append(f"  получено {info.get('seconds', 0):.1f} с звука, пик {info.get('peak', 0):.2f} — {loud}")
            text = "\n".join(lines)
        code, guide = self.diagnose(res)
        if guide:
            text += "\n\n" + support.guide_text(guide)
        self.log.info("recording self-test: %s; diagnosis: %s", res, code)
        self._journal_note("Проверка записи", text.replace("\n", " "), "user")
        messagebox.showinfo("Проверка записи", text)

    def diagnose(self, res):
        if "mic" not in res:
            res = {"mic": {"error": res.get("error", "")}, "system": {"error": res.get("error", "")}}
        return support.diagnose(res, kaspersky_loaded=bool(rec_mod.blocker_hint()),
                                privacy=support.microphone_privacy(),
                                security=support.running_security_software())

    def full_audio_check(self, then=None):
        if self.recorder is not None:
            messagebox.showinfo("Проверка звука", "Сейчас идёт запись. Проверку можно запустить после неё.")
            return
        report = self.data_dir / "meeting_hub_audio_report.txt"
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--audio-check", str(report)]
        else:
            cmd = [sys.executable.replace("pythonw.exe", "python.exe"), str(app_dir() / "audio_check.py"), str(report)]
        self.last_event_text = "Подробная проверка звука: около 10 секунд…"
        self.refresh_status()

        def work():
            try:
                subprocess.run(cmd, cwd=str(app_dir()), timeout=180,
                               capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                error = ""
            except Exception as e:
                error = str(e)
            self._ui_calls.put((then or self._full_check_done, (report, error)))
        threading.Thread(target=work, name="audio-check", daemon=True).start()

    def _full_check_done(self, report, error):
        self.last_event_text = (f"Проверка звука не запустилась: {error}" if error
                                else f"Отчёт проверки звука: {report.name} — пришлите его разработчику.")
        self.refresh_status()
        if report.exists():
            try:
                os.startfile(str(report))  # type: ignore[attr-defined]
            except (OSError, AttributeError):
                pass

    def play_selected_record(self):
        sel = self.records_tree.selection()
        if not sel:
            messagebox.showinfo("Записи", "Сначала выберите запись.")
            return
        if not Path(sel[0]).exists():
            if report_mod.protocol_paths(sel[0])[0].exists():
                self.open_path(report_mod.protocol_paths(sel[0])[0])
            else:
                messagebox.showinfo("Записи", "Аудио этой встречи удалено по сроку хранения; текст сохранён.")
            return
        try:
            os.startfile(sel[0])  # type: ignore[attr-defined]
        except (OSError, AttributeError) as e:
            messagebox.showerror("Записи", f"Не удалось открыть файл: {e}")

    def open_records_folder(self):
        self.rec_folder.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(self.rec_folder))  # type: ignore[attr-defined]
        except (OSError, AttributeError) as e:
            messagebox.showerror("Записи", f"Не удалось открыть папку: {e}")

    # ------------------------------------------------------------------
    # Мастер настройки и поддержка
    # ------------------------------------------------------------------
    def open_first_run(self):
        if getattr(self, "wizard", None) is not None and self.wizard.winfo_exists():
            self.wizard.lift()
            return
        self.wizard = FirstRunWizard(self)

    def reload_data(self):
        """Перечитать данные обучения после переноса из старой версии."""
        self.store = TrainingStore(self.data_dir / "meeting_hub_training.json")
        self.detector.set_patterns(self.store.meeting_patterns(), self.store.ignore_patterns())
        for row in self.training_tree.get_children():
            self.training_tree.delete(row)
        for i, ev in enumerate(self.store.legacy_events()[-250:]):
            self.training_tree.insert("", "end", iid=f"legacy-{i}", values=(
                ev.get("time", ""), WHO["legacy"], ev.get("event", ""), ev.get("detail", "")[:200],
                ev.get("source", ""), ev.get("feedback", "")))
        for entry in self.store.data["journal"][-500:]:
            self._insert_journal_row(entry)
        self.refresh_training_summary()
        self.refresh_records()

    def save_support_bundle(self):
        target = filedialog.asksaveasfilename(
            parent=self.root, defaultextension=".zip",
            initialfile=f"MeetingHub-support-{support.now_stamp()}.zip", filetypes=[("ZIP", "*.zip")],
            title="Сохранить отчёт для поддержки (без записей и расшифровок)")
        if not target:
            return
        self.last_event_text = "Собираю отчёт для поддержки: проверка звука, около 20 секунд…"
        self.refresh_status()

        def bundle(report, error):
            try:
                path = support.support_bundle(target, self.data_dir, APP_VERSION, report,
                                              {"audio_check_error": error} if error else None)
                self.last_event_text = f"Отчёт для поддержки сохранён: {path}"
                messagebox.showinfo("Отчёт для поддержки",
                                    f"Сохранено: {path}\n\nОтправьте этот файл разработчику. "
                                    "Записи встреч и расшифровки в него не входят.", parent=self.root)
            except OSError as e:
                messagebox.showerror("Отчёт для поддержки", f"Не удалось сохранить: {e}", parent=self.root)
            self.refresh_status()

        self.full_audio_check(then=bundle)

    # ------------------------------------------------------------------
    # Вкладка «Обучение»
    # ------------------------------------------------------------------
    def build_training_panel(self):
        intro = ttk.LabelFrame(self.tab_training, text="Как работает обнаружение", padding=8)
        intro.pack(fill="x", pady=(0, 8))
        ttk.Label(intro, wraplength=1200, justify="left", text=(
            "Встреча начинается, когда открыто окно встречи (Google Meet, Телемост, Zoom, Teams, Webex или "
            "ваш сохранённый шаблон) и это приложение использует микрофон: 5 секунд на отмену. "
            "Выключенный микрофон и тишина встречу не завершают. Когда окно закрыто и микрофон свободен, "
            "программа спросит «Встреча закончилась?» и завершит режим через 30 секунд. "
            "Если браузер использует микрофон в незнакомом окне, программа спросит и может запомнить окно. "
            "Звук не записывается и не анализируется; всё хранится только на этом компьютере.")).pack(anchor="w")
        bar = ttk.Frame(intro); bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="Начать встречу", command=lambda: self.user_action("manual_start")).pack(side="left")
        ttk.Button(bar, text="Завершить встречу", command=lambda: self.user_action("manual_end")).pack(side="left", padx=5)
        ttk.Button(bar, text="Встреча пропущена…", command=self.missed_meeting).pack(side="left", padx=5)
        ttk.Button(bar, text="Шаблоны встреч…", command=self.open_patterns).pack(side="left", padx=5)
        ttk.Button(bar, text="Импорт JSON", command=self.import_training).pack(side="right")
        ttk.Button(bar, text="Экспорт JSON", command=self.export_training).pack(side="right", padx=5)

        cols = [("time", "Время", 140), ("who", "Кто", 110), ("change", "Переход", 260),
                ("reason", "Причина", 470), ("where", "Приложение / окно", 260), ("label", "Оценка", 80)]
        self.training_tree = ttk.Treeview(self.tab_training, columns=[c[0] for c in cols], show="headings", height=14)
        for col, label, width in cols:
            self.training_tree.heading(col, text=label)
            self.training_tree.column(col, width=width, anchor="w")
        self.training_tree.pack(fill="both", expand=True)
        actions = ttk.Frame(self.tab_training); actions.pack(fill="x", pady=8)
        ttk.Button(actions, text="Решение верное", command=lambda: self.label_selected("correct")).pack(side="left")
        ttk.Button(actions, text="Решение ошибочное…", command=lambda: self.label_selected("wrong")).pack(side="left", padx=5)
        self.training_summary = ttk.Label(self.tab_training, text="")
        self.training_summary.pack(anchor="w", pady=(0, 4))

        for i, ev in enumerate(self.store.legacy_events()[-250:]):
            self.training_tree.insert("", "end", iid=f"legacy-{i}", values=(
                ev.get("time", ""), WHO["legacy"], ev.get("event", ""), ev.get("detail", "")[:200],
                ev.get("source", ""), ev.get("feedback", "")))
        for entry in self.store.data["journal"][-500:]:
            self._insert_journal_row(entry)
        self.refresh_training_summary()

    def _insert_journal_row(self, entry, select=False):
        if not hasattr(self, "training_tree"):
            return
        change = f"{STATE_LABELS.get(entry.get('prev'), entry.get('prev', ''))} → {STATE_LABELS.get(entry.get('new'), entry.get('new', ''))}"
        if entry.get("prev") == entry.get("new"):
            change = entry.get("event", "")
        where = entry.get("label") or entry.get("app") or ""
        if entry.get("title"):
            where += " · " + strip_browser_suffix(entry["title"])[:80]
        iid = entry.get("id")
        if self.training_tree.exists(iid):
            return
        self.training_tree.insert("", "end", iid=iid, values=(
            entry.get("time", ""), WHO.get(entry.get("initiator"), entry.get("initiator", "")), change,
            entry.get("reason", ""), where, USER_LABELS.get(entry.get("user_label", ""), "")))
        if select:
            self.training_tree.see(iid)
        self.refresh_training_summary()

    def refresh_training_summary(self):
        if not hasattr(self, "training_summary"):
            return
        self.training_summary.config(text=(
            f"Шаблонов встреч: {len(DEFAULT_PATTERNS)} встроенных + {len(self.store.meeting_patterns())} ваших · "
            f"исключений: {len(self.store.ignore_patterns())} · записей журнала: {len(self.store.data['journal'])} · "
            f"архив v4.9: {len(self.store.legacy_events())} событий"))

    def label_selected(self, label):
        sel = self.training_tree.selection()
        if not sel:
            messagebox.showinfo("Обучение", "Сначала выберите строку журнала.")
            return
        iid = sel[0]
        if iid.startswith("legacy-"):
            messagebox.showinfo("Обучение", "Записи архива v4.9 хранятся без изменений и не переоцениваются.")
            return
        entry = self.store.set_label(iid, label)
        if entry is None:
            return
        vals = list(self.training_tree.item(iid, "values")); vals[5] = USER_LABELS[label]
        self.training_tree.item(iid, values=vals)
        if label == "wrong" and entry.get("title") and entry.get("initiator") == "auto" \
                and entry.get("new") in (COUNTDOWN, IN_MEETING, ASKING):
            pattern = simpledialog.askstring(
                "Не считать встречей",
                "Чтобы это окно больше не принималось за встречу, сохраните шаблон-исключение.\n"
                "Можно сократить заголовок до узнаваемой части:",
                initialvalue=strip_browser_suffix(entry["title"]), parent=self.root)
            if pattern and pattern.strip():
                self.add_pattern(pattern, entry.get("label", ""), "ignore", example=entry["title"])

    def add_pattern(self, pattern, label, kind, example=""):
        try:
            added = self.store.add_pattern(pattern, label, kind, example=example)
        except ValueError as e:
            messagebox.showwarning("Шаблон", str(e))
            return False
        self.detector.set_patterns(self.store.meeting_patterns(), self.store.ignore_patterns())
        what = "встречи" if kind == "meeting" else "исключения"
        self.last_event_text = f"Шаблон {what} {'сохранён' if added else 'уже был'}: «{pattern.strip()}»"
        self.log.info("pattern %s %s: %s", kind, "added" if added else "exists", pattern)
        self.refresh_training_summary()
        return added

    def missed_meeting(self):
        self.store.log({"prev": self.detector.state, "new": self.detector.state, "event": "Встреча пропущена",
                        "reason": "Пользователь сообщил, что встреча шла, а обнаружение не сработало",
                        "initiator": "user"})
        self._insert_journal_row(self.store.data["journal"][-1], select=True)
        windows = self.last_obs.windows if self.last_obs else []
        titles = []
        for title, proc in windows:
            if proc in BROWSERS or proc in MEETING_APPS:
                clean_title = strip_browser_suffix(title)
                if clean_title and clean_title not in titles:
                    titles.append(clean_title)
        win = tk.Toplevel(self.root); win.title("Встреча пропущена"); win.transient(self.root)
        ttk.Label(win, padding=10, wraplength=520, text=(
            "Выберите окно, в котором шла встреча, и поправьте шаблон: оставьте узнаваемую часть "
            "заголовка без кода комнаты. В следующий раз программа включится сама.")).pack(anchor="w")
        lst = tk.Listbox(win, height=10, width=80)
        for t in titles:
            lst.insert("end", t)
        lst.pack(fill="both", expand=True, padx=10)
        pattern = tk.StringVar()
        ttk.Entry(win, textvariable=pattern, width=80).pack(fill="x", padx=10, pady=6)
        lst.bind("<<ListboxSelect>>", lambda _e: pattern.set(suggest_pattern(lst.get(lst.curselection()[0]))) if lst.curselection() else None)

        def save():
            if pattern.get().strip() and self.add_pattern(pattern.get(), "", "meeting",
                                                          example=lst.get(lst.curselection()[0]) if lst.curselection() else ""):
                win.destroy()
                if not self.detector.in_meeting and messagebox.askyesno("Встреча", "Включить режим встречи сейчас?"):
                    self.user_action("manual_start")
            elif not pattern.get().strip():
                win.destroy()
        bar = ttk.Frame(win, padding=10); bar.pack(fill="x")
        ttk.Button(bar, text="Сохранить шаблон", command=save).pack(side="left")
        ttk.Button(bar, text="Закрыть", command=win.destroy).pack(side="left", padx=6)

    def open_patterns(self):
        win = tk.Toplevel(self.root); win.title("Шаблоны встреч"); win.transient(self.root)
        tree = ttk.Treeview(win, columns=("pattern", "label", "kind", "source"), show="headings", height=16)
        for col, label, width in [("pattern", "Шаблон заголовка", 280), ("label", "Сервис", 160),
                                  ("kind", "Тип", 110), ("source", "Источник", 110)]:
            tree.heading(col, text=label); tree.column(col, width=width, anchor="w")
        tree.pack(fill="both", expand=True, padx=10, pady=10)

        def reload():
            tree.delete(*tree.get_children())
            for p in DEFAULT_PATTERNS:
                tree.insert("", "end", values=(p["pattern"], p["label"], "встреча", "встроенный"))
            for p in self.store.meeting_patterns():
                tree.insert("", "end", values=(p["pattern"], p.get("label", ""), "встреча", "ваш"))
            for p in self.store.ignore_patterns():
                tree.insert("", "end", values=(p["pattern"], p.get("label", ""), "исключение", "ваш"))

        def add(kind):
            value = simpledialog.askstring("Шаблон", "Часть заголовка окна (минимум 3 символа):", parent=win)
            if value and value.strip():
                self.add_pattern(value, "", kind)
                reload()

        def remove():
            sel = tree.selection()
            if not sel:
                return
            pattern, _label, kind, source = tree.item(sel[0], "values")
            if source == "встроенный":
                messagebox.showinfo("Шаблоны", "Встроенный шаблон нельзя удалить. Добавьте исключение, "
                                    "если он срабатывает на ненужное окно.", parent=win)
                return
            self.store.remove_pattern(pattern, "meeting" if kind == "встреча" else "ignore")
            self.detector.set_patterns(self.store.meeting_patterns(), self.store.ignore_patterns())
            self.refresh_training_summary()
            reload()

        bar = ttk.Frame(win, padding=(10, 0, 10, 10)); bar.pack(fill="x")
        ttk.Button(bar, text="Добавить шаблон встречи", command=lambda: add("meeting")).pack(side="left")
        ttk.Button(bar, text="Добавить исключение", command=lambda: add("ignore")).pack(side="left", padx=5)
        ttk.Button(bar, text="Удалить", command=remove).pack(side="left", padx=5)
        ttk.Button(bar, text="Закрыть", command=win.destroy).pack(side="right")
        reload()

    def export_training(self):
        p = filedialog.asksaveasfilename(defaultextension=".json", initialfile="MeetingHub_training.json",
                                         filetypes=[("JSON", "*.json")])
        if p:
            self.store.export(p)
            messagebox.showinfo("Обучение", "Данные обучения экспортированы.")

    def import_training(self):
        p = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not p:
            return
        try:
            added = self.store.import_file(p)
        except (OSError, ValueError) as e:
            messagebox.showerror("Импорт", f"Не удалось импортировать файл: {e}")
            return
        self.detector.set_patterns(self.store.meeting_patterns(), self.store.ignore_patterns())
        for entry in self.store.data["journal"][-500:]:
            self._insert_journal_row(entry)
        self.refresh_training_summary()
        messagebox.showinfo("Импорт", f"Добавлено записей: {added}. Перед импортом сделана резервная копия.")

    # ------------------------------------------------------------------
    # Анализ расшифровки (без изменений по сути, но блокировка — от детектора)
    # ------------------------------------------------------------------
    def make_tree(self, parent, columns):
        tree = ttk.Treeview(parent, columns=[x[0] for x in columns], show="headings")
        for c, label, width in columns:
            tree.heading(c, text=label); tree.column(c, width=width, anchor="w")
        tree.pack(fill="both", expand=True)
        return tree

    def on_close(self):
        """Крестик окна: в фоне продолжаем замечать и записывать встречи (значок в трее)."""
        if self.tray is not None and self.settings.get("close_to_tray", True):
            self.root.withdraw()
            if not self.settings.get("tray_hint_shown"):
                self.settings["tray_hint_shown"] = True
                self.save_settings()
                self.tray.notify("Meeting Hub работает в фоне и продолжает замечать встречи. "
                                 "Открыть или выйти — через значок у часов.")
            return
        self.quit_app()

    def show_window(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass

    def start_tray(self):
        if not tray_mod.available():
            return
        put = self._ui_calls.put
        try:
            self.tray = tray_mod.Tray(on_open=lambda: put((self.show_window, ())),
                                      on_record=lambda: put((self.toggle_recording_button, ())),
                                      on_quit=lambda: put((self.quit_app, ())),
                                      is_recording=lambda: self.recorder is not None).start()
        except Exception:
            self.log.exception("Значок в трее не запущен")
            self.tray = None

    def quit_app(self):
        if self.tray is not None:
            self.tray.stop()
            self.tray = None
        try:
            if self._tick_job is not None:
                self.root.after_cancel(self._tick_job)
            self.analysis_cancel.set()
            if getattr(self, "tq", None) is not None:
                self.tq.close()
            if self.prompt is not None:
                self.prompt.destroy()
            if self.recorder is not None or self._saving_threads:
                try:
                    self.status.config(text="Сохраняю запись, подождите…")
                    self.root.update()
                except tk.TclError:
                    pass
            if self.recorder is not None:
                rec, self.recorder = self.recorder, None
                try:
                    result = rec.stop()
                    self.log.info("recording saved on close: %s", result)
                    self.store.log({"prev": self.detector.state, "new": self.detector.state,
                                    "event": "Запись сохранена", "initiator": "user",
                                    "reason": "Программа закрыта во время записи",
                                    "file": result["path"].name if result else ""})
                except Exception:
                    self.log.exception("Ошибка сохранения записи при закрытии")
            for th in list(self._saving_threads):
                th.join(timeout=120)
        finally:
            self.root.destroy()

    def resume_after_meeting(self):
        if not self.root.winfo_exists() or self.detector.blocks_analysis:
            return
        if self.pending_transcript:
            transcript, model = self.pending_transcript
            self.pending_transcript = None
            self.start_analysis(transcript, model, automatic=True)
        self.refresh_status()

    def open_txt(self):
        p = filedialog.askopenfilename(filetypes=[("Текстовые файлы", "*.txt"), ("Все файлы", "*.*")])
        if not p: return
        try:
            text = Path(p).read_text(encoding="utf-8-sig", errors="replace")
            self.transcript_path = p; self.trans.delete("1.0", "end"); self.trans.insert("1.0", text)
            if not self.name.get().strip(): self.name.insert(0, Path(p).stem)
            self.last_event_text = f"Загружено {Path(p).name}: {len(text)} символов."
            self.refresh_status()
        except Exception as e: messagebox.showerror("Ошибка", str(e))

    def check_ollama(self):
        def worker():
            try:
                models = list_models()
                def done():
                    self.last_event_text = "Ollama доступна. Модели: " + (", ".join(models) if models else "не установлены")
                    self.refresh_status()
                    if not models: messagebox.showwarning("Модель не найдена", "Установи модель командой: ollama pull " + self.model.get().strip())
                self.root.after(0, done)
            except Exception as e:
                self.root.after(0, lambda err=str(e): messagebox.showerror("Ollama недоступна", "Не удалось подключиться к локальной Ollama.\n\nЗапусти setup.bat или проверь, что Ollama установлена.\n\nПодробности: " + err))
        threading.Thread(target=worker, daemon=True).start()

    def analyze(self):
        if self.auto_pause.get() and self.detector.blocks_analysis:
            messagebox.showwarning("Анализ заблокирован",
                                   "Сейчас идёт встреча, анализ не запущен. Завершите встречу или снимите "
                                   "галочку «Останавливать ИИ во время встречи».")
            return
        transcript = self.trans.get("1.0", "end-1c").strip()
        model = self.model.get().strip() or DEFAULT_MODEL
        if not transcript:
            messagebox.showinfo("Нет текста", "Сначала открой TXT-расшифровку.")
            return
        self.pending_transcript = None
        self.analysis_source = None
        self.start_analysis(transcript, model, automatic=False)

    def start_analysis(self, transcript, model, automatic=False):
        if self.analysis_running:
            return
        self.analysis_cancel.clear()
        self.analysis_running = True
        self.last_event_text = "Автоматически продолжаю анализ после встречи…" if automatic else "Локальная модель анализирует текст…"
        self.refresh_status()

        def worker():
            try:
                result = ollama_request(model, transcript, self.analysis_cancel)
                if self.analysis_cancel.is_set():
                    raise InterruptedError("Обнаружена встреча.")
                self.root.after(0, lambda result=result: self.show_result(result))
            except InterruptedError:
                self.root.after(0, lambda: self.queue_analysis_after_meeting(transcript, model))
            except Exception as e:
                self.root.after(0, lambda err=str(e): self.analysis_failed(err, automatic))
            finally:
                def finish():
                    self.analysis_running = False
                    self.refresh_status()
                self.root.after(0, finish)
        threading.Thread(target=worker, daemon=True).start()

    def analysis_failed(self, err, automatic):
        src, self.analysis_source = getattr(self, "analysis_source", None), None
        self.last_event_text = "Итоги не получены: " + err[:200]
        if automatic:
            # Без модальных окон: журнал + протокол из одной расшифровки, чтобы результат был в любом случае.
            self._journal_note("Ошибка итогов", err[:300], "auto")
            if src is not None:
                self.make_protocol(src, notify="Итоги не получены (проверьте Ollama), но расшифровка готова")
        else:
            messagebox.showerror("Ошибка анализа", "Не удалось получить результат от Ollama.\n\n" + err)
        self.refresh_status()

    def queue_analysis_after_meeting(self, transcript, model):
        self.pending_transcript = (transcript, model)
        self.analysis_cancel.clear()
        self.last_event_text = "Анализ остановлен из-за встречи. Текст сохранён; после встречи анализ запустится заново."
        self.refresh_status()
        if not self.detector.blocks_analysis:
            self.root.after(500, self.resume_after_meeting)

    def show_result(self, result):
        src = getattr(self, "analysis_source", None)
        if src is not None:
            self.analysis_source = None
            try:
                self.summary_path(src).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as e:
                self.log.warning("summary not saved: %s", e)
            self.refresh_records()
            self.make_protocol(src, notify="Итоги встречи и протокол готовы")
        self.data = result
        self.summary.delete("1.0", "end")
        self.summary.insert("1.0", "\n".join("• " + str(x) for x in result.get("summary", [])) or "Резюме не сформировано.")
        self.fill_tree(self.dec_tree, result.get("decisions", []), [("text", "text"), ("evidence", "evidence")])
        self.fill_tree(self.task_tree, result.get("tasks", []), [("task", "task"), ("owner", "owner"), ("deadline", "deadline"), ("confidence", "confidence"), ("status", None), ("evidence", "evidence")], task=True)
        self.fill_tree(self.q_tree, result.get("open_questions", []), [("question", "question"), ("evidence", "evidence")])
        uncertain = result.get("uncertain_fragments", [])
        msg = f"Итоги сформированы. Решений: {len(result.get('decisions', []))}; задач: {len(result.get('tasks', []))}; открытых вопросов: {len(result.get('open_questions', []))}."
        if uncertain: msg += f" Фрагментов на проверку: {len(uncertain)}."
        self.last_event_text = msg + " Проверь результат перед использованием."
        self.refresh_status()

    def fill_tree(self, tree, items, mapping, task=False):
        tree.delete(*tree.get_children())
        for idx, item in enumerate(items):
            values = []
            for col, key in mapping:
                if key is None: values.append("На проверке")
                else: values.append(str(item.get(key, "") or ""))
            tree.insert("", "end", iid=str(idx), values=values)

    def task_status(self, status):
        for iid in self.task_tree.selection():
            vals = list(self.task_tree.item(iid, "values")); vals[4] = status; self.task_tree.item(iid, values=vals)
            idx = int(iid)
            if idx < len(self.data.get("tasks", [])): self.data["tasks"][idx]["status"] = status

    def export_tasks(self):
        p = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="MeetingHub_tasks.csv", filetypes=[("CSV", "*.csv")])
        if not p: return
        with open(p, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f); w.writerow(["Задача", "Ответственный", "Срок", "Уверенность", "Статус", "Фрагмент-основание"])
            for iid in self.task_tree.get_children(): w.writerow(self.task_tree.item(iid, "values"))
        messagebox.showinfo("Экспорт", "Задачи сохранены.")

    def save_project(self):
        p = filedialog.asksaveasfilename(defaultextension=".json", initialfile="MeetingHub_project.json", filetypes=[("JSON", "*.json")])
        if not p: return
        data = {"name": self.name.get(), "date": self.date.get(), "model": self.model.get(), "source_file": self.transcript_path,
                "transcript": self.trans.get("1.0", "end-1c"), "analysis": self.data,
                "summary": self.summary.get("1.0", "end-1c"),
                "decisions": [self.dec_tree.item(i, "values") for i in self.dec_tree.get_children()],
                "tasks": [self.task_tree.item(i, "values") for i in self.task_tree.get_children()],
                "questions": [self.q_tree.item(i, "values") for i in self.q_tree.get_children()]}
        Path(p).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        messagebox.showinfo("Сохранение", "Проект сохранён.")

    def open_project(self):
        p = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not p: return
        try:
            d = json.loads(Path(p).read_text(encoding="utf-8"))
            self.name.delete(0, "end"); self.name.insert(0, d.get("name", ""))
            self.date.delete(0, "end"); self.date.insert(0, d.get("date", ""))
            self.model.delete(0, "end"); self.model.insert(0, d.get("model", DEFAULT_MODEL))
            self.trans.delete("1.0", "end"); self.trans.insert("1.0", d.get("transcript", ""))
            self.transcript_path = d.get("source_file", ""); self.data = d.get("analysis", {})
            self.summary.delete("1.0", "end"); self.summary.insert("1.0", d.get("summary", ""))
            for tree, key in [(self.dec_tree, "decisions"), (self.task_tree, "tasks"), (self.q_tree, "questions")]:
                tree.delete(*tree.get_children())
                for i, row in enumerate(d.get(key, [])): tree.insert("", "end", iid=str(i), values=row)
            self.last_event_text = "Проект открыт: " + Path(p).name
            self.refresh_status()
        except Exception as e: messagebox.showerror("Ошибка открытия", str(e))


def cli_record(seconds):
    """MeetingHub.exe --record-test N: записать N секунд без окна (для проверки сборки)."""
    rec = rec_mod.AudioRecorder(data_dir() / "recordings")
    rec.start({"meeting": "проверка сборки", "app_version": APP_VERSION})
    time.sleep(seconds)
    result = rec.stop()
    import wave
    import numpy as np
    with wave.open(str(result["path"]), "rb") as w:
        frames = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").reshape(-1, 2)
    summary = {"file": result["path"].name, "duration": round(result["duration"], 2), "errors": result["errors"],
               "mic_peak": int(np.abs(frames[:, 0]).max()) if len(frames) else 0,
               "system_peak": int(np.abs(frames[:, 1]).max()) if len(frames) else 0}
    (data_dir() / "record_test.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def cli_transcribe(path, size, language):
    """MeetingHub.exe --transcribe FILE [модель] [язык]: расшифровать запись без окна."""
    eng = tr_mod.LocalWhisper(data_dir() / "models", size=size)
    txt = tr_mod.transcribe_file(path, eng, language)
    report_mod.write_protocol(path)
    if sys.stdout is not None:
        print(txt.read_text(encoding="utf-8"))
    return txt


def main():
    args = sys.argv[1:]
    if args[:1] == ["--transcribe"] and len(args) > 1:
        cli_transcribe(args[1], args[2] if len(args) > 2 else tr_mod.DEFAULT_MODEL,
                       args[3] if len(args) > 3 else "ru")
        return
    if args[:1] == ["--audio-check"]:
        import audio_check
        report = Path(args[1]) if len(args) > 1 and not args[1].startswith("--") else \
            data_dir() / "meeting_hub_audio_report.txt"
        audio_check.run(report)
        if "--open" in args:
            try:
                os.startfile(str(report))  # type: ignore[attr-defined]
            except (OSError, AttributeError):
                pass
        return
    if args[:1] == ["--record-test"]:
        cli_record(float(args[1]) if len(args) > 1 else 3.0)
        return
    root = tk.Tk()
    app = App(root)
    if "--minimized" in args and app.settings.get("first_run_done"):
        if app.tray is not None:
            root.withdraw()
        else:
            root.iconify()
    root.mainloop()


if __name__ == "__main__":
    main()
