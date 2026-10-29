# -*- coding: utf-8 -*-
"""Meeting Hub — сам замечает онлайн-встречи, записывает, расшифровывает и готовит итоги."""
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
import ai_setup
import updater
import privacy
import winsounds
import appmute
import tray as tray_mod
import theme
from ui_main import MainWindow
from meeting_detector import (MeetingDetector, DEFAULT_PATTERNS, STATE_LABELS, BROWSERS, MEETING_APPS,
                              IDLE, ASKING, COUNTDOWN, IN_MEETING, ENDING,
                              suggest_pattern, strip_browser_suffix)
from training_store import TrainingStore

APP_VERSION = "7.41.0"

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


class App(MainWindow):
    def __init__(self, root):
        self.root = root
        here = data_dir()
        self.data_dir = here
        self.cloud_log = privacy.CloudLog(here / "cloud_log.jsonl")
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
        self.settings_path = here / "meeting_hub_settings.json"
        self.settings = self.load_settings()
        self.rec_folder = here / "recordings"
        self.recorder = None
        self.rec_by_meeting = False
        self.rec_errors_seen = set()
        self._saving_threads = []
        self._ui_calls = queue.Queue()   # результаты фоновых потоков выполняются в потоке интерфейса
        self.tray = None                 # значок в трее создаётся в конце __init__
        self.update_found = None
        self._update_toast = None
        self.wizard = None

        self.summary_jobs = []           # [(запись, автоматически)] — итоги по очереди
        self.app_version = APP_VERSION
        self.default_llm = DEFAULT_MODEL
        st = self.settings
        self.auto_detect = tk.BooleanVar(value=bool(st.get("auto_detect", True)))
        self.auto_pause = tk.BooleanVar(value=bool(st.get("auto_pause", True)))
        self.record_meetings = tk.BooleanVar(value=bool(st.get("record_meetings", True)))
        self.auto_transcribe = tk.BooleanVar(value=bool(st.get("auto_transcribe", True)))
        self.auto_summary = tk.BooleanVar(value=bool(st.get("auto_summary", True)))
        self.pal = theme.apply(root, st.get("theme", "system"))
        root.title("Meeting Hub")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.build_main_ui()
        self.refresh_status()
        if self.store.notices:
            self.last_event_text = " ".join(self.store.notices)
            self.log.info("store: %s", self.last_event_text)
        if imported:
            self._journal_note("Данные перенесены", "Из папки программы в профиль пользователя: " + ", ".join(imported))
        if not self.settings.get("first_run_done"):
            self.root.after(600, self.open_first_run)
        self.start_tray()
        import shutil
        shutil.rmtree(here / "updates", ignore_errors=True)          # установщик прошлого обновления
        self.sounds_backup = here / "win_sounds_backup.json"
        self.apps_backup = here / "muted_apps_backup.json"
        self.app_muter = None
        if winsounds.supported():
            self.restore_win_sounds("После прошлого запуска звуки были отключены — возвращаю")
            threading.Thread(target=lambda: appmute.restore_backup(self.apps_backup, self.log), daemon=True).start()
        self.root.after(5000, self.backfill_titles)
        self.root.after(8000, self.demote_unsure_voices)
        self.root.after(20 * 1000, self.catch_up_transcription)
        self.root.after(10 * 60 * 1000, self.housekeeping)                # хранение: через 10 мин после старта
        self.root.after(20 * 1000, self._update_timer)
        self.root.after(60 * 1000, lambda: updater.mark_code_good(self.data_dir))   # новый код продержался минуту
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
            self.check_muted_speech()
            tq = getattr(self, "tq", None)
            if tq is not None:
                tq.set_paused(self.detector.blocks_analysis)
                if (tq.busy() or self.analysis_running) and time.time() - getattr(self, "_tr_refresh", 0) > 2:
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
                local = tr_mod.LocalWhisper(folder, size=self.settings.get("whisper_model", tr_mod.DEFAULT_MODEL))
                if self.settings.get("asr_engine", "gigaam") == "gigaam":
                    import gigaam
                    local = gigaam.GigaAMWithFallback(gigaam.GigaAM(folder), local, notify=self._gigaam_fallback)
                return local

            self.tq = tr_mod.TranscriptionQueue(
                factory, language=self.settings.get("language", "ru"), log=self.log,
                on_done=lambda key, st: self._ui_calls.put((self._transcribed, (key, st))))
        return self.tq

    def llm_router(self, model=None):
        """Куда идут итоги и чат: GigaChat через посредника, если включён и есть код доступа, иначе Ollama."""
        import gigachat
        s = self.settings
        local = (model or s.get("ollama_model") or DEFAULT_MODEL).strip()
        code = (s.get("giga_code") or "").strip()
        mask = bool(s.get("privacy_mask", True))
        sig = (code, s.get("giga_proxy_url"), s.get("giga_model"), local, bool(s.get("giga_enabled")), mask)
        if getattr(self, "_router_sig", None) != sig or getattr(self, "_router", None) is None:
            client = gigachat.ProxyClient(code, s.get("giga_proxy_url") or gigachat.PROXY_URL) \
                if s.get("giga_enabled") and code else None
            self._router = gigachat.LlmRouter(local, client, s.get("giga_model") or gigachat.DEFAULT_MODEL,
                                              notify=self._giga_fallback, mask=mask, log=self._giga_logged)
            self._router_sig = sig
        return self._router

    def _giga_logged(self, meeting, what, chars, masked, ok, error=""):
        self.cloud_log.add("GigaChat", meeting, what, chars, masked, ok, error)

    def privacy_words(self):
        return privacy.parse_words(self.settings.get("privacy_words", ""))

    def is_confidential(self, wav):
        return privacy.wav_confidential(wav, self.privacy_words())

    def toggle_confidential(self, wav):
        now = not self.is_confidential(wav)
        privacy.set_confidential(wav, now)
        self._journal_note("Встреча помечена как конфиденциальная" if now else "Пометка «Конфиденциально» снята",
                           Path(wav).stem, "user")
        self.refresh_records()
        self.show_meeting(wav, force=True)

    def _giga_fallback(self, reason):
        self._ui_calls.put((lambda: self._journal_note(
            "GigaChat недоступен", f"{reason}. Использую локальную модель.", "auto"), ()))

    def check_giga(self, code, parent=None):
        import gigachat

        def worker():
            try:
                msg = gigachat.ProxyClient(code, self.settings.get("giga_proxy_url") or gigachat.PROXY_URL,
                                           timeout=30).check()
                show = messagebox.showinfo
            except Exception as e:
                msg, show = f"Не получилось: {e}", messagebox.showwarning
            self._ui_calls.put((lambda: show("GigaChat", msg, parent=parent or self.root), ()))
        threading.Thread(target=worker, daemon=True).start()

    def _gigaam_fallback(self, reason):
        """Из потока расшифровки: GigaAM не сработала, дальше — Whisper."""
        self._ui_calls.put((lambda: self._journal_note(
            "GigaAM недоступна", f"{reason}. Расшифровываю моделью Whisper.", "auto"), ()))

    CATCH_UP_DAYS = 3

    def catch_up_transcription(self):
        """Свежие записи без расшифровки (восстановленные после закрытия программы, пропущенные при обновлении)
        ставятся в очередь сами, если включена автоматическая расшифровка. Каждая запись — один раз за запуск."""
        try:
            if getattr(self, "_catchup_job", None) is not None:
                self.root.after_cancel(self._catchup_job)
            self._catchup_job = self.root.after(15 * 60 * 1000, self.catch_up_transcription)
            if not self.auto_transcribe.get() or self.recorder is not None or self.detector.blocks_analysis:
                return
            if not tr_mod.LocalWhisper.available()[0]:
                return
            tried = self.__dict__.setdefault("_catchup_tried", set())
            limit = time.time() - self.CATCH_UP_DAYS * 86400
            todo = []
            for it in rec_mod.list_recordings(self.rec_folder):
                p = it["path"]
                if str(p) in tried:
                    continue
                if tr_mod.has_transcript(p):               # расшифровка есть, итогов нет — тоже догоняем
                    try:
                        recent = p.with_suffix(".transcript.json").stat().st_mtime >= limit
                    except OSError:
                        recent = False
                    if self.auto_summary.get() and recent and not self.summary_path(p).exists():
                        tried.add(str(p))
                        self.request_summary(p, automatic=True)
                    continue
                if it.get("audio_deleted"):
                    continue
                try:
                    if p.stat().st_mtime < limit or (it.get("duration_sec") or 0) < 20:
                        continue
                except OSError:
                    continue
                todo.append(p)
            if not todo:
                return
            tried.update(str(p) for p in todo)
            q = self.transcribe_queue()
            q.set_paused(self.detector.blocks_analysis)
            added = [p for p in todo if q.add(p)]
            if added:
                self._journal_note("Расшифровка поставлена в очередь", f"Записи без расшифровки: {len(added)}", "auto")
                self.refresh_records()
                self.refresh_status()
        except Exception:
            self.log.exception("Догоняющая расшифровка")

    def transcribe_files(self, paths, force=False):
        ok, why = tr_mod.LocalWhisper.available()
        if not ok:
            messagebox.showerror("Расшифровка", why, parent=self.root)
            return
        q = self.transcribe_queue()
        q.set_paused(self.auto_pause.get() and self.detector.blocks_analysis)
        added = [p for p in paths if q.add(p)]
        if added:
            self._journal_note("Расшифровка поставлена в очередь", f"Файлов: {len(added)}", "user")
        self.refresh_records()
        self.refresh_status()

    @staticmethod
    def summary_path(wav):
        wav = Path(wav)
        return wav.with_name(wav.stem + ".summary.json")

    def make_protocol(self, wav, notify=""):
        try:
            path = report_mod.write_protocol(wav)
        except Exception:
            self.log.exception("Протокол не создан")
            return None
        try:
            # Основной протокол — документ Word: открывается в Word, его удобно править и пересылать.
            path = report_mod.write_docx(wav) or path
        except Exception:
            self.log.exception("Протокол Word не создан, остаётся HTML")
        if path is not None:
            self._journal_note("Протокол готов", path.name, "auto", file=path.name)
            if notify:
                self.toast(notify, path)
            if self.settings.get("kanban_token") and self.settings.get("kanban_auto", True):
                self.kanban_push(wav)
            if self.crm_configured() and self.settings.get("crm_auto", True):
                self.crm_push(wav)
        self.refresh_records()
        return path

    # ------------------------------------------------------------------
    # Канбан: отправка встреч во вкладку «Встречи»
    # ------------------------------------------------------------------
    def kanban_status_text(self):
        if not self.settings.get("kanban_token"):
            return "Не подключён."
        return "Подключён: " + (self.settings.get("kanban_email") or "аккаунт Канбана")

    def kanban_push(self, wav, manual=False):
        token = self.settings.get("kanban_token")
        if not token:
            return
        if self.is_confidential(wav):
            if manual:
                messagebox.showinfo("Канбан", "Встреча помечена как конфиденциальная и в Канбан не отправляется. "
                                    "Снимите пометку в меню «Ещё», если это нужно.", parent=self.root)
            return
        import kanban_sync
        with_text = bool(self.settings.get("kanban_transcript", False))

        def work():
            try:
                kanban_sync.push(token, wav, with_transcript=with_text)
                self.cloud_log.add("Канбан", Path(wav).stem,
                                   "итоги, задачи и расшифровка" if with_text else "итоги и задачи")
                note = ("Встреча отправлена в Канбан", Path(wav).stem)
            except kanban_sync.KanbanError as e:
                self.cloud_log.add("Канбан", Path(wav).stem, "итоги и задачи", ok=False, error=str(e))
                note = ("Канбан: встреча не отправлена", str(e))
            except Exception as e:
                self.log.exception("Отправка в Канбан")
                note = ("Канбан: встреча не отправлена", f"{type(e).__name__}: {e}")
            ok = note[0].startswith("Встреча")

            def done():
                self._journal_note(note[0], note[1], "user" if manual else "auto")
                if manual:
                    (messagebox.showinfo if ok else messagebox.showwarning)(
                        "Канбан", "Встреча отправлена во вкладку «Встречи»." if ok else note[1], parent=self.root)
            self._ui_calls.put((done, ()))
        threading.Thread(target=work, name="kanban-push", daemon=True).start()

    # ------------------------------------------------------------------
    # CRM «Этажи»: протоколы встреч (правила те же, что для Канбана)
    # ------------------------------------------------------------------
    def crm_configured(self):
        s = self.settings
        return bool(s.get("crm_login") and s.get("crm_password"))

    def crm_client(self):
        import crm_sync
        s = self.settings
        key = (s.get("crm_url"), s.get("crm_product"), s.get("crm_login"), s.get("crm_password"))
        if getattr(self, "_crm_key", None) != key or getattr(self, "_crm", None) is None:
            self._crm = crm_sync.Client(s.get("crm_url"), s.get("crm_product"), s.get("crm_login"),
                                        crm_sync.unprotect_password(s.get("crm_password")))
            self._crm_key = key
        return self._crm

    def crm_push(self, wav, manual=False):
        import crm_sync
        if not self.crm_configured():
            return
        if self.is_confidential(wav):
            if manual:
                messagebox.showinfo("CRM", "Встреча помечена как конфиденциальная и в CRM не отправляется.",
                                    parent=self.root)
            return
        if not crm_sync.ready():
            if manual:
                messagebox.showinfo("CRM", "Загрузка протоколов в CRM ещё не настроена: нужно описание запроса "
                                    "API протоколов. Вход в CRM можно проверить в настройках.", parent=self.root)
            return
        with_text = bool(self.settings.get("kanban_transcript", False))

        def work():
            try:
                crm_sync.push(self.crm_client(), wav, with_transcript=with_text)
                self.cloud_log.add("CRM", Path(wav).stem, "итоги, задачи и расшифровка" if with_text else "итоги и задачи")
                note = ("Протокол отправлен в CRM", Path(wav).stem)
            except crm_sync.CrmError as e:
                self.cloud_log.add("CRM", Path(wav).stem, "итоги и задачи", ok=False, error=str(e))
                note = ("CRM: протокол не отправлен", str(e))
            except Exception as e:
                self.log.exception("Отправка в CRM")
                note = ("CRM: протокол не отправлен", f"{type(e).__name__}: {e}")
            ok = note[0].startswith("Протокол")

            def done():
                self._journal_note(note[0], note[1], "user" if manual else "auto")
                if manual:
                    (messagebox.showinfo if ok else messagebox.showwarning)(
                        "CRM", "Протокол отправлен во вкладку «Протоколы»." if ok else note[1], parent=self.root)
            self._ui_calls.put((done, ()))
        threading.Thread(target=work, name="crm-push", daemon=True).start()

    def crm_check(self, product_name, login, password, parent=None, on_ok=None):
        """Проверить вход в CRM; при успехе сохранить логин, пароль (зашифрованным) и проект.
        Адрес CRM зашит в программу, проект берётся из CRM (по умолчанию — проект пользователя)."""
        import crm_sync
        url = self.settings.get("crm_url") or crm_sync.DEFAULT_URL

        def work():
            products, product = [], ""
            try:
                pwd = password or crm_sync.unprotect_password(self.settings.get("crm_password"))
                who, default, products = crm_sync.Client(url, "", login, pwd).info()
                chosen = next((pid for pid, name in products if name == product_name), "")
                product = chosen or default
                blob = crm_sync.protect_password(pwd)
                err = ""
            except Exception as e:
                who, blob, err = "", "", str(e)

            def done():
                if err:
                    messagebox.showwarning("CRM", "Не удалось войти: " + err, parent=parent or self.root)
                    return
                pname = next((name for pid, name in products if pid == product), "")
                self.settings.update(crm_product=product, crm_product_name=pname, crm_products=products,
                                     crm_login=login.strip(), crm_password=blob, crm_name=who)
                self._crm = None
                self.save_settings()
                messagebox.showinfo("CRM", f"Вход работает: {who}." + (f" Проект: {pname}." if pname else ""),
                                    parent=parent or self.root)
                if on_ok:
                    on_ok()
            self._ui_calls.put((done, ()))
        threading.Thread(target=work, name="crm-check", daemon=True).start()

    def _crm_product_choice(self, name):
        """Проект, выбранный в списке настроек (если поменяли без повторной проверки входа)."""
        pid = next((p for p, n in self.settings.get("crm_products") or [] if n == name), None)
        return {"crm_product": pid, "crm_product_name": name} if pid else {}

    def crm_disconnect(self):
        for key in ("crm_password", "crm_name"):
            self.settings.pop(key, None)
        self._crm = None
        self.save_settings()

    def kanban_disconnect(self):
        for key in ("kanban_token", "kanban_email"):
            self.settings.pop(key, None)
        self.save_settings()

    def kanban_check(self, parent=None):
        import kanban_sync
        token = self.settings.get("kanban_token")

        def work():
            try:
                msg, show = "Подключение работает: " + kanban_sync.check(token), messagebox.showinfo
            except Exception as e:
                msg, show = f"Не получилось: {e}", messagebox.showwarning
            self._ui_calls.put((lambda: show("Канбан", msg, parent=parent or self.root), ()))
        threading.Thread(target=work, name="kanban-check", daemon=True).start()

    def kanban_connect_dialog(self, parent, on_done=None):
        import kanban_sync
        win = tk.Toplevel(parent)
        win.title("Подключить Канбан")
        win.transient(parent)
        win.grab_set()
        frm = ttk.Frame(win, padding=18)
        frm.pack(fill="both", expand=True)
        email, password, status = tk.StringVar(), tk.StringVar(), tk.StringVar()
        ttk.Label(frm, text="Почта аккаунта Канбана").pack(anchor="w")
        e1 = ttk.Entry(frm, textvariable=email, width=38)
        e1.pack(fill="x", pady=(2, 8))
        ttk.Label(frm, text="Пароль").pack(anchor="w")
        ttk.Entry(frm, textvariable=password, show="•", width=38).pack(fill="x", pady=(2, 8))
        ttk.Label(frm, text="Пароль используется один раз для получения ключа доступа и нигде не сохраняется.",
                  style="Small.TLabel", wraplength=340, justify="left").pack(anchor="w")
        ttk.Label(frm, textvariable=status, foreground="#b00020", wraplength=340, justify="left").pack(
            anchor="w", pady=(8, 0))
        row = ttk.Frame(frm)
        row.pack(fill="x", pady=(12, 0))
        btn = ttk.Button(row, text="Подключить", style="Accent.TButton")
        btn.pack(side="right")
        ttk.Button(row, text="Отмена", command=win.destroy).pack(side="right", padx=(0, 8))

        def finish(result, error):
            if not win.winfo_exists():
                return
            btn.config(state="normal")
            if error:
                status.set(error)
                return
            token, mail = result
            self.settings.update(kanban_token=token, kanban_email=mail)
            self.save_settings()
            win.destroy()
            if on_done:
                on_done()
            messagebox.showinfo("Канбан", f"Подключено: {mail}.\nГотовые встречи будут уходить во вкладку «Встречи».",
                                parent=parent)

        def go():
            mail, pw = email.get(), password.get()
            btn.config(state="disabled")
            status.set("Подключаюсь…")

            def work():
                try:
                    result, error = kanban_sync.connect(mail, pw), ""
                except kanban_sync.KanbanError as e:
                    result, error = None, str(e)
                except Exception as e:
                    result, error = None, f"{type(e).__name__}: {e}"
                self._ui_calls.put((lambda: finish(result, error), ()))
            threading.Thread(target=work, name="kanban-connect", daemon=True).start()
        btn.config(command=go)
        win.bind("<Return>", lambda _e: go())
        win.bind("<Escape>", lambda _e: win.destroy())
        e1.focus_set()

    def open_path(self, path):
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except (OSError, AttributeError) as e:
            messagebox.showerror("Открыть файл", f"Не удалось открыть: {e}")

    def toast(self, text, path=None, seconds=25, action=None):
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
            if action is not None:
                ttk.Button(row, text=action[0], style="Accent.TButton",
                           command=lambda: (action[1](), win.destroy())).pack(side="left")
            ttk.Button(row, text="Закрыть", command=win.destroy).pack(side="left", padx=(6, 0))
            win.update_idletasks()
            x = win.winfo_screenwidth() - win.winfo_width() - 24
            y = win.winfo_screenheight() - win.winfo_height() - 72
            win.geometry(f"+{x}+{y}")
            win.after(seconds * 1000, lambda: win.winfo_exists() and win.destroy())
        except tk.TclError:
            pass

    def cancel_transcription(self, wav=None):
        tq = getattr(self, "tq", None)
        if tq is not None:
            if wav is None or str(wav) == tq.current:
                tq.cancel()
            if wav is None:
                for st in tq.status.values():
                    if st["state"] == "queued":
                        st["state"] = "cancelled"
            else:
                tq.cancel(wav)
        self.refresh_records()
        self.refresh_status()

    def _transcribed(self, key, st):
        name = Path(key).name
        if st["state"] == "done":
            self._journal_note("Расшифровка готова", name, "auto", file=name)
            self.last_event_text = ""
            try:
                import titles
                titles.apply_initial_name(key)           # название из окна встречи или слов разговора + дата
            except Exception:
                self.log.exception("Название встречи не записано")

            def after():
                if self.auto_summary.get():
                    self.request_summary(key, automatic=True)
                else:
                    self.make_protocol(key, notify="Расшифровка встречи готова")
            if self.voices_wanted():
                self.run_voices(key, then=after)       # сначала голоса: имена попадут в итоги и протокол
            else:
                after()
        elif st["state"] == "error":
            self._journal_note("Ошибка расшифровки", f"{name}: {st['error']}", "auto", file=name)
            self.last_event_text = f"Расшифровка не удалась: {st['error'][:150]}"
        self.refresh_records()
        self.refresh_status()

    def demote_unsure_voices(self):
        """Один раз: имена голосов, подставленные прошлыми версиями без уверенности, становятся предположениями."""
        if self.settings.get("voices_unsure_fixed"):
            return

        def work():
            import voices
            n = 0
            for sp in Path(self.rec_folder).glob("*.speakers.json"):
                try:
                    n += voices.demote_unsure(sp.with_name(sp.name[:-len(".speakers.json")] + ".wav"))
                except Exception:
                    self.log.exception("Голоса: %s", sp.name)
            self.settings["voices_unsure_fixed"] = True

            def done():
                self.save_settings()
                if n:
                    self._journal_note("Голоса уточнены", f"Неуверенные подписи сняты во встречах: {n}", "auto")
                    self.refresh_records()
                    if self.selected is not None:
                        self.show_meeting(self.selected, force=True)
            self._ui_calls.put((done, ()))
        threading.Thread(target=work, daemon=True).start()

    def backfill_titles(self):
        """Один раз: дать названия старым записям без названия (ничего не удаляется, ручные названия целы)."""
        if self.settings.get("titles_backfilled"):
            return

        def work():
            try:
                import titles
                n = titles.backfill(self.rec_folder)
            except Exception:
                self.log.exception("Названия старых записей")
                return
            self.settings["titles_backfilled"] = True
            self.save_settings()
            if n:
                self._ui_calls.put((lambda: (self.refresh_records(), self._journal_note(
                    "Названия старых записей", f"Присвоено названий: {n}", "auto")), ()))
        threading.Thread(target=work, name="titles", daemon=True).start()

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

    def _update_timer(self):
        """Проверка раз в час (запрос к GitHub лёгкий); без сети — повтор через 10 минут."""
        self.check_updates(manual=False)
        try:
            self.root.after(3600 * 1000, self._update_timer)
        except tk.TclError:
            pass

    def check_updates(self, manual=False):
        if not manual and not self.settings.get("check_updates", True):
            return
        self._last_update_check = time.time()

        def work():
            try:
                found, error = daily.check_update(APP_VERSION), ""
            except Exception as e:
                found, error = None, str(e)
            self._ui_calls.put((self._update_checked, (found, error, manual)))
        threading.Thread(target=work, name="update-check", daemon=True).start()

    def _update_checked(self, found, error, manual):
        if found is None:
            if manual:
                messagebox.showinfo("Обновления", f"Не удалось проверить: {error}" if error else
                                    f"Установлена последняя версия ({APP_VERSION}).")
            if error and not manual:
                self.root.after(10 * 60 * 1000, lambda: self.check_updates(manual=False))
            return
        version, asset, page = found
        if self.settings.get("skip_version") == version and not manual:
            return
        self.update_found = found
        self.last_event_text = f"Доступна новая версия {version}"
        self.show_update_button(version)
        self.refresh_status()
        if manual:
            self.show_window()
            self._offered_version = None
            if getattr(self, "_update_toast", None) is None:
                self.update_toast(found)
            return
        self._offer_update()

    def _offer_update(self):
        """Само предлагает обновиться: окошко в углу экрана (даже если программа свёрнута в трей)."""
        found = getattr(self, "update_found", None)
        if not found or getattr(self, "_update_toast", None) is not None:
            return
        version = found[0]
        busy = self.recorder is not None or self.detector.in_meeting or self.detector.state != IDLE
        what = daily.update_offer(version, self.settings, time.time(), busy)
        if what == "wait":
            try:
                self.root.after(120 * 1000, self._offer_update)       # после встречи
            except tk.TclError:
                pass
            return
        if what == "hide" or getattr(self, "_offered_version", None) == version:
            return
        self._offered_version = version
        self.update_toast(found)                          # одно окошко с кнопками, без второго уведомления

    def update_toast(self, found):
        version, asset, page = found
        try:
            win = self._update_toast = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            frame = ttk.Frame(win, padding=14, relief="solid")
            frame.pack()
            ttk.Label(frame, text=f"Вышла новая версия {version}", font=(theme.FONT, 11, "bold")).pack(anchor="w")
            ttk.Label(frame, text=f"У вас {APP_VERSION}. Обновление ставится за минуту, записи и настройки "
                      "сохраняются.", style="Muted.TLabel", wraplength=340, justify="left").pack(anchor="w",
                                                                                                pady=(4, 0))
            row = ttk.Frame(frame); row.pack(anchor="e", pady=(10, 0))

            def close():
                self._update_toast = None
                if win.winfo_exists():
                    win.destroy()

            def now():
                close()
                self.show_window()
                self.install_found_update()

            def later():
                self.settings["update_snooze_until"] = time.time() + 12 * 3600
                self.settings["update_snooze_version"] = version
                self.save_settings()
                close()

            def skip():
                self.settings["skip_version"] = version
                self.save_settings()
                self.hide_update_button()
                close()
            ttk.Button(row, text="Пропустить версию", command=skip).pack(side="left")
            ttk.Button(row, text="Позже", command=later).pack(side="left", padx=6)
            ttk.Button(row, text="Обновить сейчас", style="Accent.TButton", command=now).pack(side="left")
            win.update_idletasks()
            win.geometry(f"+{win.winfo_screenwidth() - win.winfo_width() - 24}+"
                         f"{win.winfo_screenheight() - win.winfo_height() - 72}")
        except tk.TclError:
            self._update_toast = None

    def install_found_update(self):
        found = getattr(self, "update_found", None)
        if not found:
            return
        version, asset, page = found
        code = (asset or {}).get("code")
        if updater.can_update_code(code):
            self.start_update(code["version"], code, code_only=True)       # только код: exe не меняется
        elif updater.can_self_update() and asset and asset.get("url"):
            self.start_update(version, asset)
        else:
            import webbrowser
            webbrowser.open((asset or {}).get("url") or page)

    def start_update(self, version, asset, code_only=False):
        if self.recorder is not None or self.detector.in_meeting:
            messagebox.showinfo("Обновление", "Сейчас идёт встреча или запись. Обновите программу после неё: "
                                "⚙ → «Хранение» → «Проверить сейчас».", parent=self.root)
            return
        tq = getattr(self, "tq", None)
        if (tq is not None and tq.busy()) or self.analysis_running:
            if not messagebox.askyesno("Обновление", "Сейчас обрабатывается встреча. Обработка продолжится после "
                                       "обновления заново. Обновить сейчас?", parent=self.root):
                return
        win = tk.Toplevel(self.root)
        win.title("Обновление Meeting Hub")
        win.transient(self.root)
        win.resizable(False, False)
        box = ttk.Frame(win, padding=20)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text=f"Обновление до версии {version}", style="H2.TLabel").pack(anchor="w")
        label = ttk.Label(box, text="Скачиваю…", style="Muted.TLabel", width=52)
        label.pack(anchor="w", pady=(8, 6))
        bar = ttk.Progressbar(box, mode="determinate", maximum=100, length=420)
        bar.pack(fill="x")
        cancel = threading.Event()
        btn = ttk.Button(box, text="Отмена", command=cancel.set)
        btn.pack(anchor="e", pady=(14, 0))
        win.protocol("WM_DELETE_WINDOW", cancel.set)

        def progress(done, total):
            def ui():
                if total:
                    bar["value"] = done * 100 / total
                    label.config(text=f"Скачиваю: {done / 1048576:.0f} из {total / 1048576:.0f} МБ")
            self._ui_calls.put((ui, ()))

        def work():
            try:
                path = updater.download(asset, self.data_dir / "updates", progress=progress, cancel=cancel)
                self._ui_calls.put((finish, (path, "")))
            except Exception as e:
                self._ui_calls.put((finish, (None, str(e))))

        def finish(path, error):
            if path is None:
                try:
                    win.destroy()
                except tk.TclError:
                    pass
                if "отменено" not in error:
                    messagebox.showerror("Обновление", error, parent=self.root)
                return
            label.config(text="Устанавливаю… Программа перезапустится сама.")
            bar["value"] = 100
            btn.state(["disabled"])
            self._journal_note("Обновление", f"Установка версии {version}" + (" (только код)" if code_only else ""),
                               "user")
            try:
                if code_only:
                    updater.install_code(path, self.data_dir, version)
                    updater.restart_self()
                else:
                    updater.launch_installer(path)
            except (OSError, updater.UpdateError) as e:
                messagebox.showerror("Обновление", f"Не удалось запустить установку: {e}", parent=self.root)
                win.destroy()
                return
            self.root.after(1500, self.quit_app)
        threading.Thread(target=work, name="update-download", daemon=True).start()

    def _journal_note(self, event, reason, initiator="auto", **extra):
        entry = self.store.log({"prev": self.detector.state, "new": self.detector.state, "event": event,
                                "reason": reason, "initiator": initiator, **extra})
        self._insert_journal_row(entry, select=True)
        self.log.info("%s | %s | %s", event, reason, extra)

    def toggle_recording_button(self):
        if self.recorder is not None:
            self.stop_recording("Пользователь остановил запись", "user")
            if self.detector.state in (IN_MEETING, ENDING):
                self.user_action("manual_end")             # вручную остановил — «встреча закончилась?» не спрашиваем
        else:
            self.start_recording("Пользователь начал запись", "user", by_meeting=self.detector.in_meeting)

    def toggle_mic_mute(self):
        """Выключить/включить свой голос в записи (участники вас слышат как обычно)."""
        rec = self.recorder
        if rec is None:
            return
        rec.set_mic_muted(not rec.mic_muted)
        self._journal_note("Микрофон в записи " + ("выключен" if rec.mic_muted else "включён"),
                           "Ваш голос не записывается" if rec.mic_muted else "Ваш голос снова записывается",
                           "user")
        self.last_event_text = ("Ваш микрофон в записи выключен." if rec.mic_muted
                                else "Ваш микрофон в записи включён.")
        self.refresh_status()

    def toggle_recording_confidential(self):
        """Во время встречи: пометить её конфиденциальной (или снять пометку)."""
        rec = self.recorder
        if rec is None:
            return
        rec.set_confidential(not rec.confidential)
        self._journal_note("Встреча помечена как конфиденциальная" if rec.confidential
                           else "Пометка «Конфиденциально» снята", "Во время записи", "user")
        self.last_event_text = ("Встреча конфиденциальная: итоги только локально, в облако не уйдёт." if rec.confidential
                                else "Пометка «Конфиденциально» снята.")
        self.refresh_status()

    def check_muted_speech(self):
        """Микрофон в записи выключен, а вы заговорили (скорее всего, включили микрофон во встрече) —
        включаем его и в записи, чтобы ваш голос не потерялся. Можно отключить в настройках."""
        rec = self.recorder
        if rec is None or not rec.mic_muted or not getattr(rec, "muted_speech", False):
            return
        rec.muted_speech = False
        if self.settings.get("auto_unmute_mic", True):
            self.toggle_mic_mute()
            self._journal_note("Микрофон в записи включён сам", "Вы заговорили, а микрофон в записи был выключен",
                               "auto")
            self.toast("Вы заговорили — ваш микрофон в записи снова включён.", seconds=12,
                       action=("Выключить снова", lambda: self.recorder is not None
                               and not self.recorder.mic_muted and self.toggle_mic_mute()))
        elif not getattr(self, "_muted_speech_asked", False):
            self._muted_speech_asked = True
            self.toast("Вы говорите, а ваш микрофон в записи выключен.", seconds=20,
                       action=("Включить", lambda: self.recorder is not None and self.recorder.mic_muted
                               and self.toggle_mic_mute()))

    def toggle_system_mute(self):
        """Не писать звук компьютера (собеседники и системные звуки) / снова писать."""
        rec = self.recorder
        if rec is None:
            return
        rec.set_system_muted(not rec.system_muted)
        self._journal_note("Звук компьютера в записи " + ("выключен" if rec.system_muted else "включён"),
                           "Собеседники и системные звуки не записываются" if rec.system_muted
                           else "Звук компьютера снова записывается", "user")
        self.last_event_text = ("Звук компьютера в записи выключен: собеседников не слышно в записи."
                                if rec.system_muted else "Звук компьютера в записи включён.")
        self.refresh_status()

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
        self.sounds_forced_on = False
        if self.settings.get("mute_win_sounds", True):
            self.mute_win_sounds()
        if self.settings.get("mute_apps", True):
            self.start_app_mute()
        if self.tray is not None:
            self.tray.notify("Идёт запись встречи. Остановить: правая кнопка по значку у часов → «Остановить запись».",
                             "Meeting Hub: запись")
        self.refresh_status()

    def start_app_mute(self):
        """Заглушить программы из списка (мессенджеры, почта) на время записи."""
        if not winsounds.supported() or self.app_muter is not None:
            return
        cand = self.detector.candidate
        keep = (cand.app,) if cand and cand.app else ()
        self.app_muter = appmute.AppMuter(
            self.apps_backup, appmute.parse_names(self.settings.get("mute_apps_list", appmute.DEFAULT_LIST)),
            all_mode=bool(self.settings.get("mute_apps_all", False)), keep=keep, log=self.log).start()
        self._journal_note("Звуки программ заглушены", "Мессенджеры и почта на время записи", "auto")

    def stop_app_mute(self):
        muter, self.app_muter = self.app_muter, None
        if muter is not None:
            threading.Thread(target=muter.stop, daemon=True).start()

    def mute_win_sounds(self):
        if not winsounds.supported() or winsounds.is_muted(self.sounds_backup):
            return
        try:
            n = winsounds.mute(self.sounds_backup)
        except Exception:
            self.log.exception("Звуки Windows не отключены")
            return
        if n:
            self._journal_note("Звуки Windows отключены", "На время записи, чтобы они не попали в запись", "auto")

    def restore_win_sounds(self, why=""):
        if not winsounds.supported() or not winsounds.is_muted(self.sounds_backup):
            return
        try:
            n = winsounds.restore(self.sounds_backup)
        except Exception:
            self.log.exception("Звуки Windows не восстановлены")
            return
        if n:
            self._journal_note("Звуки Windows включены", why or "Запись закончилась", "auto")

    def sounds_are_muted(self):
        return winsounds.is_muted(self.sounds_backup) or self.app_muter is not None

    def toggle_win_sounds(self):
        """Из меню значка: вручную включить звуки на время записи или снова отключить."""
        if self.sounds_are_muted():
            self.sounds_forced_on = True
            self.restore_win_sounds("Включено вручную во время записи")
            self.stop_app_mute()
        else:
            self.sounds_forced_on = False
            self.mute_win_sounds()
            self.start_app_mute()
        self.refresh_status()

    def stop_recording(self, reason, initiator="auto", cut_at=None):
        rec = self.recorder
        if rec is None:
            return
        self.recorder = None
        self.rec_by_meeting = False
        self.restore_win_sounds()
        self.stop_app_mute()

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
                self.catch_up_transcription()
            self._ui_calls.put((done, ()))

    # ------------------------------------------------------------------
    # Вкладка «Записи»
    # ------------------------------------------------------------------
    def test_recording(self):
        if self.recorder is not None:
            messagebox.showinfo("Проверка записи", "Сейчас идёт запись. Проверку можно запустить после неё.")
            return
        try:
            backend = rec_mod.AutoBackend()
        except rec_mod.RecorderError as e:
            messagebox.showerror("Проверка записи", str(e))
            return
        try:
            self.test_btn.config(state="disabled", text="Проверяю… говорите и включите звук")
        except (tk.TclError, AttributeError):
            pass
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
        try:
            self.test_btn.config(state="normal", text="Проверить запись")
        except (tk.TclError, AttributeError):
            pass
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
        if hasattr(self, "training_tree"):
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
    def build_training_panel(self, parent):
        intro = ttk.LabelFrame(parent, text="Как работает обнаружение", padding=8)
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
        self.training_tree = ttk.Treeview(parent, columns=[c[0] for c in cols], show="headings", height=14)
        for col, label, width in cols:
            self.training_tree.heading(col, text=label)
            self.training_tree.column(col, width=width, anchor="w")
        self.training_tree.pack(fill="both", expand=True)
        actions = ttk.Frame(parent); actions.pack(fill="x", pady=8)
        ttk.Button(actions, text="Решение верное", command=lambda: self.label_selected("correct")).pack(side="left")
        ttk.Button(actions, text="Решение ошибочное…", command=lambda: self.label_selected("wrong")).pack(side="left", padx=5)
        self.training_summary = ttk.Label(parent, text="")
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
        if time.time() - getattr(self, "_last_update_check", 0) > 20 * 60:      # открыли окно — свежая проверка
            self.check_updates(manual=False)
        try:
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            # Windows не отдаёт фокус окну, которое просит его из фона, — на миг делаем окно «поверх всех».
            self.root.attributes("-topmost", True)
            self.root.after(250, lambda: self.root.attributes("-topmost", False))
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
                                      on_mute=lambda: put((self.toggle_mic_mute, ())),
                                      is_muted=lambda: bool(self.recorder and self.recorder.mic_muted),
                                      on_mute_system=lambda: put((self.toggle_system_mute, ())),
                                      is_system_muted=lambda: bool(self.recorder and self.recorder.system_muted),
                                      on_confidential=lambda: put((self.toggle_recording_confidential, ())),
                                      is_confidential=lambda: bool(self.recorder and self.recorder.confidential),
                                      on_sounds=(lambda: put((self.toggle_win_sounds, ())))
                                      if winsounds.supported() else None,
                                      sounds_muted=lambda: self.sounds_are_muted(),
                                      is_recording=lambda: self.recorder is not None,
                                      elapsed=lambda: self.recorder.elapsed() if self.recorder else 0.0).start()
        except Exception:
            self.log.exception("Значок в трее не запущен")
            self.tray = None

    def quit_app(self):
        self.restore_win_sounds("Программа закрыта")
        if self.app_muter is not None:
            muter, self.app_muter = self.app_muter, None
            muter.stop()
        if getattr(self, "player", None) is not None:
            self.player.close()
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
                    self.status.set("Сохраняю запись, подождите…", *self.pal["pill"]["busy"])
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
        if not self.root.winfo_exists():
            return
        self._next_summary()
        self.refresh_status()

    def request_summary(self, wav, rerun=False, automatic=False):
        """Поставить подготовку итогов встречи в очередь (по одной, не во время встречи)."""
        wav = Path(wav)
        if not tr_mod.has_transcript(wav):
            if not automatic:
                messagebox.showinfo("Итоги", "Сначала встречу нужно расшифровать.", parent=self.root)
            return
        if self.summary_path(wav).exists() and not rerun:
            self.refresh_records()
            return
        if str(self.analysis_source) == str(wav) or any(str(w) == str(wav) for w, _a in self.summary_jobs):
            return
        self.summary_jobs.append((wav, automatic))
        self._next_summary()
        self.refresh_records()
        self.refresh_status()

    def _next_summary(self):
        if self.analysis_running or not self.summary_jobs:
            return
        if self.auto_pause.get() and self.detector.blocks_analysis:
            return                                   # продолжим после встречи (resume_after_meeting)
        wav, automatic = self.summary_jobs.pop(0)
        try:
            text = tr_mod.output_paths(wav)[0].read_text(encoding="utf-8-sig", errors="replace")
        except OSError as e:
            self._journal_note("Ошибка итогов", f"{wav.name}: {e}", "auto")
            self.root.after(100, self._next_summary)
            return
        try:
            meta = json.loads(Path(wav).with_suffix(".json").read_text(encoding="utf-8"))
            context = ", ".join(x for x in (meta.get("title"), meta.get("description")) if x)
            if context:
                text = f"Тема встречи: {context}\n\n" + text
        except (OSError, ValueError):
            pass
        self.analysis_source = wav
        self.start_analysis(text, self.settings.get("ollama_model") or DEFAULT_MODEL, automatic=automatic)

    def check_ollama(self, model=None):
        model = (model or self.settings.get("ollama_model") or DEFAULT_MODEL).strip()

        def worker():
            st = ai_setup.status(model)
            if st == "ok":
                self._ui_calls.put((lambda: messagebox.showinfo(
                    "Ollama", f"Ollama работает, модель «{model}» установлена."), ()))
                return
            why = {"no_model": f"Ollama работает, но модели «{model}» нет.",
                   "not_running": "Ollama установлена, но не запущена.",
                   "not_installed": "Ollama на этом компьютере не установлена."}[st]

            def ask():
                self._ai_offered = False
                self.offer_ai_setup(why)
            self._ui_calls.put((ask, ()))
        threading.Thread(target=worker, daemon=True).start()

    def start_analysis(self, transcript, model, automatic=False):
        if self.analysis_running:
            return
        self.analysis_cancel.clear()
        self.analysis_running = True
        src = self.analysis_source
        self.refresh_records()
        self.refresh_status()

        def worker():
            try:
                router = self.llm_router(model)
                closed = bool(src) and self.is_confidential(src)
                stem = Path(src).stem if src else None
                result = analyze_transcript(transcript, lambda system, user: router.chat_json(
                    system, user, self.analysis_cancel, meeting=stem, local_only=closed), self.analysis_cancel)
                if self.analysis_cancel.is_set():
                    raise InterruptedError("Обнаружена встреча.")
                self._ui_calls.put((self.show_result, (result,)))
            except InterruptedError:
                self._ui_calls.put((self.queue_analysis_after_meeting, (src, automatic)))
            except Exception as e:
                self._ui_calls.put((self.analysis_failed, (str(e), automatic)))
            finally:
                def finish():
                    self.analysis_running = False
                    self.refresh_records()
                    self.refresh_status()
                    self.root.after(300, self._next_summary)
                self._ui_calls.put((finish, ()))
        threading.Thread(target=worker, name="summary", daemon=True).start()

    def analysis_failed(self, err, automatic):
        src, self.analysis_source = self.analysis_source, None
        if "Connection refused" in err or "urlopen error" in err or "10061" in err:
            err = ("ИИ для итогов не установлен или не запущен. Откройте ⚙ → «Расшифровка и итоги» → "
                   "«Установить ИИ…» — программа всё сделает сама.")
            self.root.after(500, lambda: self.offer_ai_setup("Итоги не подготовлены."))
        self.last_event_text = "Итоги не получены: " + err[:200]
        self._journal_note("Ошибка итогов", err[:300], "auto")
        if src is not None:
            self.make_protocol(src, notify="Итоги не получены, но расшифровка готова" if automatic else "")
        if not automatic:
            messagebox.showerror("Итоги", "Не удалось подготовить итоги.\n\n" + err, parent=self.root)
        self.refresh_status()

    def queue_analysis_after_meeting(self, wav, automatic):
        self.analysis_source = None
        self.analysis_cancel.clear()
        if wav is not None:
            self.summary_jobs.insert(0, (Path(wav), automatic))
        self.last_event_text = "Итоги подготовятся после окончания встречи."
        self.refresh_status()

    def show_result(self, result):
        src, self.analysis_source = self.analysis_source, None
        if src is None:
            return
        try:
            self.summary_path(src).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            self.log.warning("summary not saved: %s", e)
            return
        try:
            import importer
            importer.auto_name(src, result)
        except Exception:
            self.log.exception("Название встречи не записано")
        self.make_protocol(src, notify="Итоги встречи готовы")
        self.last_event_text = ""
        self.refresh_records()
        self.refresh_status()

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
    report_mod.write_docx(path)
    if sys.stdout is not None:
        print(txt.read_text(encoding="utf-8"))
    return txt


def cli_import(path, size, language):
    """MeetingHub.exe --import FILE [модель] [язык]: добавить аудио/видео как запись и расшифровать (для проверки)."""
    import importer
    wav = importer.import_file(path, data_dir() / "recordings", description="проверка импорта")
    eng = tr_mod.LocalWhisper(data_dir() / "models", size=size)
    txt = tr_mod.transcribe_file(wav, eng, language)
    report_mod.write_protocol(wav)
    (data_dir() / "import_test.json").write_text(
        json.dumps({"wav": wav.name, "text": txt.read_text(encoding="utf-8")}, ensure_ascii=False), encoding="utf-8")


def cli_voices_test():
    """MeetingHub.exe --voices-test: модуль голосов в установленной программе — модель, отпечаток тона."""
    import numpy as np
    import voices
    out = {"ok": False}
    try:
        ok, why = voices.available()
        if not ok:
            raise RuntimeError(why)
        voices.download_model(data_dir())
        emb = voices.Embedder(voices.model_path(data_dir()))
        t = np.arange(16000 * 2) / 16000
        v = emb.embed((np.sin(2 * np.pi * 220 * t) * 0.3).astype(np.float32))
        out = {"ok": v is not None and len(v) > 50, "dim": 0 if v is None else int(len(v))}
    except Exception as e:
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    (data_dir() / "voices_test.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def cli_play_test(seconds=1.5):
    """MeetingHub.exe --play-test: проиграть тон внутри программы и записать, каким способом вышел звук."""
    import wave
    import numpy as np
    import player as player_mod
    path = data_dir() / "play_test.wav"
    t = np.arange(int(16000 * 3)) / 16000
    tone = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(np.stack([tone, tone], axis=1).tobytes())
    p = player_mod.Player()
    result = {"ok": False}
    try:
        p.load(path)
        p.play()
        time.sleep(seconds)
        result = {"ok": True, "backend": getattr(p.backend, "name", "?"), "position": round(p.position, 2),
                  "config": getattr(p.backend, "config", None), "errors": p.errors}
    except Exception as e:
        result = {"ok": False, "error": str(e), "errors": p.errors}
    finally:
        p.close()
    (data_dir() / "play_test.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def main():
    args = sys.argv[1:]
    if args[:1] == ["--code-info"]:                      # для проверки обновления кода на сборке
        info = {"version": APP_VERSION, "file": os.path.abspath(__file__),
                "base_runtime": os.environ.get("MEETINGHUB_BASE_RUNTIME", ""),
                "base_version": os.environ.get("MEETINGHUB_BASE_VERSION", "")}
        (data_dir() / "code_info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        return
    if args[:1] == ["--voices-test"]:
        cli_voices_test()
        return
    if args[:1] == ["--play-test"]:
        cli_play_test()
        return
    if args[:1] == ["--import"] and len(args) > 1:
        cli_import(args[1], args[2] if len(args) > 2 else tr_mod.DEFAULT_MODEL, args[3] if len(args) > 3 else "ru")
        return
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
