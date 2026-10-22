# -*- coding: utf-8 -*-
"""Meeting Hub v5 — локальный анализ расшифровок через Ollama и обнаружение встреч."""
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from datetime import datetime
import json, csv, urllib.request, urllib.error, threading, re, time, subprocess, math

OLLAMA_URL = "http://localhost:11434/api/chat"
DEFAULT_MODEL = "qwen2.5:3b"

SYSTEM_PROMPT = """Ты — помощник по протоколированию рабочих встреч на русском языке.
Работай ТОЛЬКО с фактами из переданной расшифровки. Не выдумывай решения, задачи, имена, суммы и сроки.
Распознавание речи может содержать ошибки. Если смысл неясен, пометь фрагмент как требующий проверки.
Различай:
- summary: короткое резюме без повторов;
- decisions: только явно принятые решения;
- tasks: только явные поручения/действия. Если ответственный или срок не назван, оставь поле пустым;
- open_questions: вопросы, которые остались без ответа или требуют уточнения.
Верни строго JSON без markdown и без текста вокруг, по схеме:
{
 "summary": ["..."],
 "decisions": [{"text":"...", "evidence":"дословный короткий фрагмент"}],
 "tasks": [{"task":"...", "owner":"", "deadline":"", "evidence":"дословный короткий фрагмент", "confidence":"high|medium|low"}],
 "open_questions": [{"question":"...", "evidence":"дословный короткий фрагмент"}],
 "uncertain_fragments": [{"text":"...", "reason":"..."}]
}
Не превращай предложение или обсуждение в решение, если согласие не выражено явно.
Фразы «просит добавить», «можно добавить», «надо бы», «обсудили» сами по себе НЕ доказывают, что решение принято.
Если нет явного подтверждения согласия, не добавляй фразу в decisions.
Если неясно, было ли поручение принято, добавь фрагмент в uncertain_fragments, а не в tasks.
Не выдумывай формулировки вроде «отменять не нужно», если этого нет в расшифровке.
Не дублируй одну и ту же фразу в decisions и tasks без отдельного явного основания.
В evidence приводи только короткую цитату, реально присутствующую в расшифровке.
"""

def ollama_request(model, transcript, cancel_event=None):
    payload = {
        "model": model, "stream": True, "format": "json",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Проанализируй расшифровку встречи. Верни JSON по схеме из системного сообщения.\n\nРАСШИФРОВКА:\n" + transcript}
        ],
        "options": {"temperature": 0.1, "num_thread": 2, "num_ctx": 4096}
    }
    req = urllib.request.Request(OLLAMA_URL, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                headers={"Content-Type": "application/json"}, method="POST")
    chunks = []
    response = urllib.request.urlopen(req, timeout=900)
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                response.close()
                raise InterruptedError("Анализ остановлен для встречи.")
            line = response.readline()
            if not line:
                break
            part = json.loads(line.decode("utf-8"))
            if part.get("error"):
                raise RuntimeError(part["error"])
            text = part.get("message", {}).get("content", "")
            if text:
                chunks.append(text)
            if part.get("done"):
                break
    finally:
        response.close()
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("Анализ остановлен для встречи.")
    content = "".join(chunks)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            raise ValueError("Модель вернула ответ, который не удалось прочитать как JSON.")
        return json.loads(match.group(0))

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
from meeting_detector import (MeetingDetector, DEFAULT_PATTERNS, STATE_LABELS, BROWSERS, MEETING_APPS,
                              IDLE, ASKING, COUNTDOWN, IN_MEETING, ENDING,
                              suggest_pattern, strip_browser_suffix)
from training_store import TrainingStore

APP_VERSION = "5.0"
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


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"Meeting Hub v{APP_VERSION}")
        root.geometry("1360x900")
        here = Path(__file__).resolve().parent
        self.log = setup_logging(here)
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
        self.pending_transcript = None

        header = ttk.Frame(root, padding=10); header.pack(fill="x")
        ttk.Label(header, text="MEETING HUB", font=("Segoe UI", 18, "bold")).pack(side="left")
        ttk.Button(header, text="Открыть TXT", command=self.open_txt).pack(side="right")
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
        ttk.Button(livebar, text="Завершить встречу", command=lambda: self.user_action("manual_end")).pack(side="right")
        ttk.Button(livebar, text="Начать встречу", command=lambda: self.user_action("manual_start")).pack(side="right", padx=6)

        self.status = ttk.Label(root, text="Запуск…", padding=(12, 0, 12, 2), font=("Segoe UI", 10, "bold"))
        self.status.pack(fill="x")
        self.substatus = ttk.Label(root, text="", padding=(12, 0, 12, 8))
        self.substatus.pack(fill="x")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        nb = ttk.Notebook(root); nb.pack(fill="both", expand=True, padx=10, pady=4)
        self.tab_trans = ttk.Frame(nb, padding=8); self.tab_summary = ttk.Frame(nb, padding=8)
        self.tab_dec = ttk.Frame(nb, padding=8); self.tab_tasks = ttk.Frame(nb, padding=8)
        self.tab_questions = ttk.Frame(nb, padding=8); self.tab_training = ttk.Frame(nb, padding=8)
        nb.add(self.tab_trans, text="Расшифровка"); nb.add(self.tab_summary, text="Резюме")
        nb.add(self.tab_dec, text="Решения"); nb.add(self.tab_tasks, text="Задачи")
        nb.add(self.tab_questions, text="Открытые вопросы"); nb.add(self.tab_training, text="Обучение")
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
        self.refresh_status()
        if self.store.notices:
            self.last_event_text = " ".join(self.store.notices)
            self.log.info("store: %s", self.last_event_text)
        self._tick_job = self.root.after(800, self.tick)

    # ------------------------------------------------------------------
    # Цикл обнаружения
    # ------------------------------------------------------------------
    def tick(self):
        self._tick_job = None
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
        if decision.new == IDLE and decision.prev in (IN_MEETING, ENDING):
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
                                 f"{what}.\nРежим встречи включится через {left} с. "
                                 "Локальный ИИ будет приостановлен.")
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
            obs = self.last_obs
            mic = "микрофон: нет данных" if obs is None or obs.mic is None else (
                "микрофон занят: " + ", ".join(sorted(obs.mic)) if obs.mic else "микрофон свободен")
            self.substatus.config(text=(self.last_event_text + "   |   " if self.last_event_text else "") + mic)
        except tk.TclError:
            pass
        self.refresh_analysis_button()

    def refresh_analysis_button(self):
        try:
            blocked = self.analysis_running or bool(self.pending_transcript) or \
                (self.auto_pause.get() and self.detector.blocks_analysis)
            self.analyze_btn.config(state="disabled" if blocked else "normal")
        except (tk.TclError, AttributeError):
            pass

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
        try:
            if self._tick_job is not None:
                self.root.after_cancel(self._tick_job)
            self.analysis_cancel.set()
            if self.prompt is not None:
                self.prompt.destroy()
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
                self.root.after(0, lambda err=str(e): messagebox.showerror("Ollama недоступна", "Не удалось подключиться к локальной Ollama.\n\nЗапусти setup_ollama.bat или проверь, что Ollama установлена.\n\nПодробности: " + err))
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
                self.root.after(0, lambda err=str(e): messagebox.showerror("Ошибка анализа", "Не удалось получить результат от Ollama.\n\n" + err))
                self.root.after(0, lambda: setattr(self, "last_event_text", "Анализ не завершён."))
            finally:
                def finish():
                    self.analysis_running = False
                    self.refresh_status()
                self.root.after(0, finish)
        threading.Thread(target=worker, daemon=True).start()

    def queue_analysis_after_meeting(self, transcript, model):
        self.pending_transcript = (transcript, model)
        self.analysis_cancel.clear()
        self.last_event_text = "Анализ остановлен из-за встречи. Текст сохранён; после встречи анализ запустится заново."
        self.refresh_status()
        if not self.detector.blocks_analysis:
            self.root.after(500, self.resume_after_meeting)

    def show_result(self, result):
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


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
