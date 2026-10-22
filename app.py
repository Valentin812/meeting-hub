# -*- coding: utf-8 -*-
"""Meeting Hub v4 — локальный анализ расшифровок через Ollama."""
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


MEETING_PROCESS_NAMES = {
    "zoom.exe", "ms-teams.exe", "teams.exe", "msteams.exe", "webex.exe",
    "webexhost.exe", "skype.exe", "slack.exe", "discord.exe", "gotomeeting.exe",
    "ringcentral.exe", "telemost.exe", "trueconf.exe", "viber.exe",
    "zoom workplace.exe", "avaya.exe"
}
BROWSER_PROCESS_NAMES = {"chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "brave.exe", "browser.exe", "vivaldi.exe"}

def inspect_audio_sessions():
    """Return (status, active_sessions, error). status=True/False/None."""
    try:
        import psutil
        from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
        active_sessions = []
        for session in AudioUtilities.GetAllSessions():
            try:
                if session.State != 1 or session._ctl is None:
                    continue
                meter = session._ctl.QueryInterface(IAudioMeterInformation)
                peak = float(meter.GetPeakValue())
                if peak < 0.008:
                    continue
                pid = getattr(session, "ProcessId", 0)
                name = "system / unknown"
                if pid:
                    try:
                        name = psutil.Process(pid).name()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        name = f"PID {pid}"
                active_sessions.append((name, peak, pid))
            except Exception:
                continue
        return True, active_sessions, ""
    except Exception as e:
        return None, [], str(e)

class MicrophoneActivityMonitor:
    """Continuously measures microphone level locally; does not save or transmit audio."""
    def __init__(self):
        self.available = False
        self.level = 0.0
        self.error = ""
        self.stream = None
        self.lock = threading.Lock()

    def start(self):
        try:
            import sounddevice as sd
            def callback(indata, frames, time_info, status):
                try:
                    # RMS only; audio samples are discarded immediately.
                    sq = 0.0
                    count = len(indata) * (len(indata[0]) if len(indata) else 1)
                    for row in indata:
                        for sample in row:
                            sq += float(sample) * float(sample)
                    rms = math.sqrt(sq / max(count, 1))
                    with self.lock:
                        self.level = rms
                        self.available = True
                except Exception:
                    pass
            self.stream = sd.InputStream(channels=1, samplerate=16000, blocksize=1600, callback=callback)
            self.stream.start()
            self.available = True
            return True
        except Exception as e:
            self.error = str(e)
            self.available = False
            return False

    def is_voice_active(self):
        with self.lock:
            return self.available and self.level >= 0.012

    def close(self):
        try:
            if self.stream is not None:
                self.stream.stop()
                self.stream.close()
        except Exception:
            pass


class ChromeMicRegistryMonitor:
    """Watch Windows microphone-consent registry notifications and emit new Chrome starts.
    This is evidence of Chrome microphone access, not proof that a call started.
    """
    def __init__(self):
        self.events = __import__('queue').Queue()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            import ctypes, winreg
            roots = [
                (winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone\NonPackaged', 'HKCU_NonPackaged'),
                (winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone', 'HKCU_Microphone'),
            ]
            KEY_READ = 0x20019
            REG_NOTIFY_CHANGE_NAME = 0x1
            REG_NOTIFY_CHANGE_LAST_SET = 0x4
            advapi = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
            notify = advapi.RegNotifyChangeKeyValue
            notify.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
            notify.restype = ctypes.c_long

            def snapshot():
                out = {}
                for root, path, prefix in roots:
                    try:
                        with winreg.OpenKey(root, path, 0, KEY_READ) as top:
                            def rec(k, subpath='', depth=0):
                                if depth > 12: return
                                vals = {}
                                i = 0
                                while True:
                                    try: n, v, typ = winreg.EnumValue(k, i)
                                    except OSError: break
                                    if n.lower() in ('lastusedtimestart','lastusedtimestop','value'):
                                        vals[n.lower()] = v
                                    i += 1
                                keyname = (prefix + '\\' + subpath).lower()
                                if 'lastusedtimestart' in vals and 'chrome.exe' in keyname:
                                    out[keyname] = (vals.get('lastusedtimestart'), vals.get('lastusedtimestop'))
                                j = 0
                                while True:
                                    try: child = winreg.EnumKey(k, j)
                                    except OSError: break
                                    try:
                                        with winreg.OpenKey(k, child, 0, KEY_READ) as ck:
                                            rec(ck, subpath + ('\\' if subpath else '') + child, depth+1)
                                    except OSError: pass
                                    j += 1
                            rec(top)
                    except OSError:
                        pass
                return out

            baseline = snapshot()
            self.events.put(('ready', 'Монитор доступа Chrome к микрофону запущен.'))
            # Separate watcher per root; Windows notifies on changes below the watched tree.
            def watch(root, path, prefix):
                try: key = winreg.OpenKey(root, path, 0, KEY_READ)
                except OSError as e:
                    self.events.put(('error', f'Не удалось наблюдать реестр {prefix}: {e}')); return
                handle = ctypes.c_void_p(int(key))
                nonlocal baseline
                while not self.stop_event.is_set():
                    event = ctypes.windll.kernel32.CreateEventW(None, True, False, None)
                    if not event:
                        self.events.put(('error', 'Не удалось создать событие Windows для мониторинга реестра.')); break
                    rc = notify(handle, 1, REG_NOTIFY_CHANGE_NAME | REG_NOTIFY_CHANGE_LAST_SET, event, True)
                    if rc != 0:
                        ctypes.windll.kernel32.CloseHandle(event); time.sleep(1); continue
                    wait = ctypes.windll.kernel32.WaitForSingleObject(event, 700)
                    ctypes.windll.kernel32.CloseHandle(event)
                    if wait == 0:
                        current = snapshot()
                        for k, value in current.items():
                            old = baseline.get(k)
                            if not isinstance(value, tuple):
                                continue
                            start, stop = value
                            old_start, old_stop = old if isinstance(old, tuple) else (None, None)
                            # Windows keeps historical LastUsedTime* values. Only treat a
                            # changed start timestamp as a live access if it is genuinely recent;
                            # otherwise app startup / stale registry notifications can look like a call.
                            if start not in (None, 0, b'', '') and start != old_start:
                                is_recent = False
                                try:
                                    from datetime import datetime, timezone, timedelta
                                    raw = str(start).replace('Z', '+00:00')
                                    if raw.isdigit():
                                        # Windows registry LastUsedTime* is commonly FILETIME:
                                        # 100-ns ticks since 1601-01-01 UTC.
                                        stamp = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=int(raw) / 10)
                                    else:
                                        stamp = datetime.fromisoformat(raw)
                                        if stamp.tzinfo is None:
                                            stamp = stamp.replace(tzinfo=timezone.utc)
                                    age = abs((datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds())
                                    is_recent = age <= 20
                                except Exception:
                                    is_recent = False
                                # A newly created Chrome registry key can be the first real mic-access
                                # record for this profile. Accept it only when the start timestamp is
                                # fresh (<=10 sec) and there is no newer stop marker. This avoids stale
                                # startup history without silently discarding first-time Chrome access.
                                if is_recent and (stop in (None, 0, b'', '') or stop == old_stop):
                                    self.events.put(('chrome_mic_start', {'key': k, 'timestamp': str(start)}))
                            if stop not in (None, 0, b'', '') and stop != old_stop and old is not None:
                                self.events.put(('chrome_mic_stop', {'key': k, 'timestamp': str(stop)}))
                        baseline = current
                    elif wait not in (0x102,):
                        time.sleep(.2)
                winreg.CloseKey(key)
            workers = []
            for root, path, prefix in roots:
                t = threading.Thread(target=watch, args=(root,path,prefix), daemon=True)
                t.start(); workers.append(t)
            while not self.stop_event.is_set(): time.sleep(.2)
        except Exception as e:
            self.events.put(('error', f'Монитор доступа к микрофону не запущен: {e}'))

    def poll(self):
        out=[]
        while True:
            try: out.append(self.events.get_nowait())
            except Exception: break
        return out

    def close(self):
        self.stop_event.set()


def detect_meeting(mic_monitor=None):
    """Return direct evidence only; the UI watcher adds persistence/hysteresis."""
    try:
        import psutil
        running = set()
        for proc in psutil.process_iter(["name"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if name:
                    running.add(name)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception as e:
        return None, "Не удалось проверить процессы Windows: " + str(e)

    active, sessions, error = inspect_audio_sessions()
    # Browser audio is not proof of a meeting: music, videos, notifications, or idle tabs
    # can create active Chrome audio sessions. Chrome meeting detection is handled separately
    # by the fresh registry-start signal above.
    session_names = [s[0].lower() for s in sessions]
    matches = sorted(set(name for name in session_names if name in MEETING_PROCESS_NAMES))
    if matches:
        return True, "Обнаружен звук приложения для встреч: " + ", ".join(matches)

    candidates = sorted(running & MEETING_PROCESS_NAMES)
    if candidates and mic_monitor is not None and mic_monitor.is_voice_active():
        return "mic", "Активность микрофона при открытом приложении для встреч: " + ", ".join(candidates)

    if active is None:
        return None, "Не удалось проверить аудиосессии Windows."
    if sessions:
        return False, "Звук есть, но он не связан с распознанным приложением для встреч."
    if candidates:
        return False, "Приложение встречи/браузер открыто; жду подтверждения звука или речи."
    return False, "Активная встреча не обнаружена."

class App:
    def __init__(self, root):
        self.root = root
        root.title("Meeting Hub v4.9 — обновлённое обучение")
        root.geometry("1360x900")
        self.project_path = ""
        self.transcript_path = ""
        self.settings_path = Path(__file__).with_name("meeting_hub_settings.json")
        self.training_path = Path(__file__).with_name("meeting_hub_training.json")
        self.training_data = self.load_training_data()
        self.last_logged_state = None
        self.current_training_event_id = None
        self.registry_seen_events = set()
        self._dedupe_training_feedback()
        self.settings = self.load_settings()
        self.pending_app_decision = None
        self.chrome_stop_since = None
        self.data = {"summary": [], "decisions": [], "tasks": [], "open_questions": [], "uncertain_fragments": []}

        header = ttk.Frame(root, padding=10); header.pack(fill="x")
        ttk.Label(header, text="MEETING HUB", font=("Segoe UI", 18, "bold")).pack(side="left")
        ttk.Button(header, text="Открыть TXT", command=self.open_txt).pack(side="right")
        ttk.Button(header, text="Сохранить проект", command=self.save_project).pack(side="right", padx=5)
        ttk.Button(header, text="Открыть проект", command=self.open_project).pack(side="right", padx=5)

        meta = ttk.LabelFrame(root, text="Встреча", padding=8); meta.pack(fill="x", padx=10, pady=(0,8))
        ttk.Label(meta, text="Название").grid(row=0,column=0,sticky="w")
        self.name=ttk.Entry(meta,width=38); self.name.grid(row=0,column=1,sticky="ew",padx=5)
        ttk.Label(meta,text="Дата").grid(row=0,column=2,sticky="w")
        self.date=ttk.Entry(meta,width=15); self.date.insert(0,datetime.now().strftime("%Y-%m-%d")); self.date.grid(row=0,column=3,padx=5)
        ttk.Label(meta,text="Модель Ollama").grid(row=0,column=4,sticky="w")
        self.model=ttk.Entry(meta,width=24); self.model.insert(0,DEFAULT_MODEL); self.model.grid(row=0,column=5,padx=5)
        ttk.Button(meta,text="Проверить Ollama",command=self.check_ollama).grid(row=0,column=6,padx=5)
        meta.columnconfigure(1,weight=1)

        livebar = ttk.Frame(root, padding=(12, 0, 12, 4))
        livebar.pack(fill="x")
        self.auto_pause = tk.BooleanVar(value=True)
        ttk.Checkbutton(livebar, text="Автоматически останавливать ИИ при обнаружении встречи",
                        variable=self.auto_pause, command=self.toggle_auto_pause).pack(side="left")
        ttk.Button(livebar, text="Встреча завершена", command=self.release_meeting_guard).pack(side="left", padx=8)
        self.meeting_active = False
        self.mic_monitor = MicrophoneActivityMonitor()
        self.mic_monitor.start()
        self.chrome_mic_monitor = ChromeMicRegistryMonitor()
        self.chrome_mic_countdown_until = None
        self.chrome_mic_countdown_reason = ""
        self.mic_active_since = None
        self.mic_quiet_since = None
        self.meeting_latched = False
        self.analysis_cancel = threading.Event()
        self.analysis_running = False
        self.pending_transcript = None
        self.status = ttk.Label(root, text="Проверка активных встреч…", padding=(12,0,12,8))
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.status.pack(fill="x")
        nb=ttk.Notebook(root); nb.pack(fill="both",expand=True,padx=10,pady=4)
        self.tab_trans=ttk.Frame(nb,padding=8); self.tab_summary=ttk.Frame(nb,padding=8)
        self.tab_dec=ttk.Frame(nb,padding=8); self.tab_tasks=ttk.Frame(nb,padding=8); self.tab_questions=ttk.Frame(nb,padding=8); self.tab_training=ttk.Frame(nb,padding=8)
        nb.add(self.tab_trans,text="Расшифровка"); nb.add(self.tab_summary,text="Резюме")
        nb.add(self.tab_dec,text="Решения"); nb.add(self.tab_tasks,text="Задачи"); nb.add(self.tab_questions,text="Открытые вопросы"); nb.add(self.tab_training,text="Обучение")
        self.trans=tk.Text(self.tab_trans,wrap="word",font=("Segoe UI",10)); self.trans.pack(fill="both",expand=True)
        tbar=ttk.Frame(self.tab_trans); tbar.pack(fill="x",pady=5)
        ttk.Button(tbar,text="Открыть TXT",command=self.open_txt).pack(side="left")
        self.analyze_btn=ttk.Button(tbar,text="Сформировать итоги локально",command=self.analyze); self.analyze_btn.pack(side="left",padx=6)
        self.analyze_btn.config(state="disabled")
        self.root.after(1500, self.meeting_watch_tick)
        self.summary=tk.Text(self.tab_summary,wrap="word",font=("Segoe UI",10)); self.summary.pack(fill="both",expand=True)
        self.dec_tree=self.make_tree(self.tab_dec,[("text","Решение",650),("evidence","Фрагмент-основание",500)])
        self.task_tree=self.make_tree(self.tab_tasks,[("task","Задача",450),("owner","Ответственный",130),("deadline","Срок",100),("confidence","Уверенность",90),("status","Статус",130),("evidence","Фрагмент-основание",350)])
        taskbar=ttk.Frame(self.tab_tasks); taskbar.pack(fill="x",pady=5)
        ttk.Button(taskbar,text="Подтвердить выбранную",command=lambda:self.task_status("Подтверждена")).pack(side="left")
        ttk.Button(taskbar,text="Отклонить выбранную",command=lambda:self.task_status("Отклонена")).pack(side="left",padx=5)
        ttk.Button(taskbar,text="Экспорт CSV",command=self.export_tasks).pack(side="right")
        self.q_tree=self.make_tree(self.tab_questions,[("question","Открытый вопрос",650),("evidence","Фрагмент-основание",500)])
        ttk.Label(root,text="ИИ формирует черновик. Проверяйте решения, поручения, имена, суммы и сроки по записи.",padding=10).pack(fill="x")
        self.build_training_panel()

    def load_training_data(self):
        try:
            data = json.loads(self.training_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("events", [])
                data.setdefault("feedback", [])
                data.setdefault("app_rules", {})
                return data
        except Exception:
            pass
        return {"events": [], "feedback": [], "app_rules": {}}

    def _dedupe_training_feedback(self):
        """Remove duplicate feedback caused by the two Windows registry views."""
        seen = set()
        unique = []
        for item in self.training_data.get("feedback", []):
            detail = str(item.get("detail", ""))
            # Registry events in both views share the same Windows timestamp.
            match = re.search(r"timestamp['\"]?\s*:\s*['\"]?(\d+)", detail)
            identity = (item.get("label"), item.get("source"), item.get("event"), match.group(1) if match else item.get("event_id"))
            if identity in seen:
                continue
            seen.add(identity)
            unique.append(item)
        self.training_data["feedback"] = unique

    def _learn_app_rule_from_feedback(self, event, label):
        """Build a cautious Chrome rule only from distinct microphone START events.
        A microphone STOP event is never treated as evidence that a meeting started.
        """
        if str(event.get("source", "")) != "Google Chrome":
            return
        if "Доступ к микрофону начался" not in str(event.get("event", "")):
            return
        positives, negatives = set(), set()
        for sample in self.training_data.get("feedback", []):
            if sample.get("source") != "Google Chrome":
                continue
            if "Доступ к микрофону начался" not in str(sample.get("event", "")):
                continue
            detail = str(sample.get("detail", ""))
            match = re.search(r"timestamp['\"]?\s*:\s*['\"]?(\d+)", detail)
            identity = match.group(1) if match else sample.get("event_id")
            if identity is None:
                continue
            if sample.get("label") == "meeting": positives.add(identity)
            elif sample.get("label") == "not_meeting": negatives.add(identity)
        if len(positives) >= 2 and len(positives) > len(negatives):
            self.training_data.setdefault("app_rules", {})["chrome.exe"] = "always"
            self.status.config(text="Обучение: Chrome — микрофонный сигнал можно автоматически проверять как кандидата на встречу.")
        elif len(negatives) >= 2 and len(negatives) >= len(positives):
            self.training_data.setdefault("app_rules", {})["chrome.exe"] = "ask"
            self.status.config(text="Обучение: для Chrome оставлен осторожный режим подтверждения.")

    def save_training_data(self):
        try:
            self.training_path.write_text(json.dumps(self.training_data, ensure_ascii=False, indent=2), encoding="utf-8")
            self.training_summary.config(text=f"Сохранено примеров: {len(self.training_data['feedback'])} · событий: {len(self.training_data['events'])}")
        except Exception as e:
            messagebox.showerror("Обучение", f"Не удалось сохранить данные: {e}")

    def build_training_panel(self):
        intro = ttk.LabelFrame(self.tab_training, text="Подтверждение событий", padding=8)
        intro.pack(fill="x", pady=(0, 8))
        ttk.Label(intro, text="Отмечай, что происходило на самом деле. Примеры сохраняются локально рядом с программой; сами веса ИИ-модели не меняются.", wraplength=1100).pack(anchor="w")
        bar = ttk.Frame(intro); bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="Встреча началась сейчас", command=self.manual_meeting_start).pack(side="left", padx=(0, 5))
        ttk.Button(bar, text="Встреча завершилась", command=self.release_meeting_guard).pack(side="left", padx=5)
        ttk.Button(bar, text="Экспорт примеров JSON", command=self.export_training).pack(side="right")
        self.training_tree = ttk.Treeview(self.tab_training, columns=("time", "source", "event", "detail", "feedback"), show="headings", height=13)
        for col, label, width in [("time","Время",145),("source","Источник",120),("event","Событие",180),("detail","Детали",540),("feedback","Метка",150)]:
            self.training_tree.heading(col, text=label); self.training_tree.column(col, width=width, anchor="w")
        self.training_tree.pack(fill="both", expand=True)
        actions = ttk.Frame(self.tab_training); actions.pack(fill="x", pady=8)
        ttk.Button(actions, text="Это была встреча", command=lambda: self.label_training_event("meeting")).pack(side="left", padx=(0,5))
        ttk.Button(actions, text="Это НЕ была встреча", command=lambda: self.label_training_event("not_meeting")).pack(side="left", padx=5)
        ttk.Button(actions, text="Встреча пропущена", command=self.label_missed_meeting).pack(side="left", padx=5)
        ttk.Button(actions, text="Правило для приложения…", command=self.configure_app_rule).pack(side="left", padx=5)
        self.training_summary = ttk.Label(self.tab_training, text="События и подтверждения сохраняются на этом компьютере.")
        self.training_summary.pack(anchor="w", pady=(0, 4))
        for event in self.training_data.get("events", [])[-250:]:
            self._insert_training_row(event)
        self.training_summary.config(text=f"Сохранено примеров: {len(self.training_data['feedback'])} · событий: {len(self.training_data['events'])}")

    def _insert_training_row(self, event):
        if not hasattr(self, "training_tree"):
            return
        iid = self.training_tree.insert("", "end", values=(event.get("time", ""), event.get("source", ""), event.get("event", ""), event.get("detail", ""), event.get("feedback", "")))
        self.training_tree.selection_set(iid)
        self.training_tree.see(iid)
        self.current_training_event_id = event.get("id")

    def record_training_event(self, source, event, detail):
        item = {"id": f"{int(time.time()*1000)}-{len(self.training_data['events'])}", "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "source": source, "event": event, "detail": str(detail)[:500], "feedback": "Не размечено"}
        self.training_data.setdefault("events", []).append(item)
        self.training_data["events"] = self.training_data["events"][-1000:]
        self._insert_training_row(item)
        self.save_training_data()

    def _selected_training_event(self):
        selected = self.training_tree.selection() if hasattr(self, "training_tree") else ()
        if not selected:
            messagebox.showinfo("Обучение", "Сначала выбери событие в списке.")
            return None
        vals = self.training_tree.item(selected[0], "values")
        return next((e for e in self.training_data.get("events", []) if e.get("time") == vals[0] and e.get("source") == vals[1] and e.get("event") == vals[2] and e.get("detail") == vals[3]), None)

    def label_training_event(self, label):
        event = self._selected_training_event()
        if not event: return
        event["feedback"] = "Встреча" if label == "meeting" else "Не встреча"
        self.training_data.setdefault("feedback", []).append({"time": datetime.now().isoformat(timespec="seconds"), "event_id": event.get("id"), "source": event.get("source"), "event": event.get("event"), "detail": event.get("detail"), "label": label})
        self._dedupe_training_feedback()
        self._learn_app_rule_from_feedback(event, label)
        self.save_training_data()
        self.refresh_training_tree()

    def label_missed_meeting(self):
        event = {"id": f"{int(time.time()*1000)}-manual", "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "source": "Ручная отметка", "event": "Пропущенная встреча", "detail": "Пользователь сообщил, что встреча шла, но автоматическое обнаружение не сработало.", "feedback": "Встреча пропущена"}
        self.training_data.setdefault("events", []).append(event)
        self.training_data.setdefault("feedback", []).append({"time": datetime.now().isoformat(timespec="seconds"), "event_id": event["id"], "source": event["source"], "event": event["event"], "detail": event["detail"], "label": "missed_meeting"})
        self._insert_training_row(event); self.save_training_data()

    def refresh_training_tree(self):
        for row in self.training_tree.get_children(): self.training_tree.delete(row)
        for event in self.training_data.get("events", [])[-250:]: self.training_tree.insert("", "end", values=(event.get("time", ""), event.get("source", ""), event.get("event", ""), event.get("detail", ""), event.get("feedback", "")))

    def configure_app_rule(self):
        win = tk.Toplevel(self.root); win.title("Правило приложения"); win.transient(self.root); win.grab_set()
        ttk.Label(win, text="Имя процесса (например chrome.exe):", padding=10).pack(anchor="w")
        name = ttk.Entry(win, width=35); name.insert(0, "chrome.exe"); name.pack(padx=10, fill="x")
        current = tk.StringVar(value="ask")
        ttk.Label(win, text="Что делать при обращении к микрофону?", padding=(10,5)).pack(anchor="w")
        for value, label in [("ask", "Каждый раз спрашивать"), ("always", "Считать сигнал кандидатом на встречу"), ("never", "Игнорировать")]:
            ttk.Radiobutton(win, text=label, value=value, variable=current).pack(anchor="w", padx=10)
        def save_rule():
            app = name.get().strip().lower()
            if not app: return
            self.training_data.setdefault("app_rules", {})[app] = current.get()
            self.save_training_data(); win.destroy()
            messagebox.showinfo("Правило сохранено", f"Правило для {app} сохранено локально. Оно фиксирует предпочтение; детектор будет подключать его по мере следующих улучшений.")
        ttk.Button(win, text="Сохранить", command=save_rule).pack(pady=10)

    def manual_meeting_start(self):
        self.meeting_latched = True; self.meeting_active = True
        self.last_meeting_reason = "Встреча подтверждена пользователем вручную"
        # Link the manual confirmation to the nearest recent Chrome mic START.
        # This creates a useful labeled sample rather than training on the manual button itself.
        now = datetime.now()
        linked = None
        for candidate in reversed(self.training_data.get("events", [])):
            if candidate.get("source") != "Google Chrome":
                continue
            if "Доступ к микрофону начался" not in str(candidate.get("event", "")):
                continue
            try:
                age = (now - datetime.strptime(candidate.get("time", ""), "%Y-%m-%d %H:%M:%S")).total_seconds()
            except Exception:
                continue
            if 0 <= age <= 120:
                linked = candidate
                break
        if linked:
            linked["feedback"] = "Встреча"
            detail = str(linked.get("detail", ""))
            match = re.search(r"timestamp['\"]?\s*:\s*['\"]?(\d+)", detail)
            identity = match.group(1) if match else linked.get("id")
            exists = any(
                x.get("event_id") == linked.get("id") or
                (x.get("source") == linked.get("source") and
                 x.get("event") == linked.get("event") and
                 x.get("detail") == linked.get("detail") and
                 x.get("label") == "meeting")
                for x in self.training_data.get("feedback", [])
            )
            if not exists:
                self.training_data.setdefault("feedback", []).append({
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "event_id": linked.get("id"), "source": linked.get("source"),
                    "event": linked.get("event"), "detail": linked.get("detail"), "label": "meeting"
                })
            self._learn_app_rule_from_feedback(linked, "meeting")
        self.record_training_event("Ручное управление", "Встреча подтверждена", self.last_meeting_reason)
        self.save_training_data()
        if self.auto_pause.get():
            self.analyze_btn.config(state="disabled")
        if linked:
            self.status.config(text="Встреча включена вручную. Ближайшее событие начала микрофона Chrome сохранено как положительный пример.")
            self.refresh_training_tree()
        else:
            self.status.config(text="Встреча включена вручную. Рядом не найдено недавнего события начала микрофона Chrome; отметка сохранена без привязки к приложению.")

    def export_training(self):
        p = filedialog.asksaveasfilename(defaultextension=".json", initialfile="MeetingHub_training.json", filetypes=[("JSON", "*.json")])
        if p:
            Path(p).write_text(json.dumps(self.training_data, ensure_ascii=False, indent=2), encoding="utf-8")
            messagebox.showinfo("Обучение", "Примеры экспортированы.")

    def load_settings(self):
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("apps", {})
                return data
        except Exception:
            pass
        return {"apps": {}}

    def save_settings(self):
        try:
            self.settings_path.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            self.status.config(text=f"Не удалось сохранить настройки приложений: {e}")

    def ask_app_policy(self, app_name, on_choice):
        win = tk.Toplevel(self.root)
        win.title("Настройка обнаружения встреч")
        win.transient(self.root)
        win.grab_set()
        win.resizable(False, False)
        ttk.Label(win, text=f"Обнаружен доступ к микрофону: {app_name}", font=("Segoe UI", 11, "bold"), padding=(16, 14, 16, 6)).pack(anchor="w")
        ttk.Label(win, text="Что делать, когда это приложение обращается к микрофону?", padding=(16, 0, 16, 12)).pack(anchor="w")
        bar = ttk.Frame(win, padding=(12, 0, 12, 12)); bar.pack(fill="x")
        def choose(value):
            try: win.grab_release()
            except Exception: pass
            win.destroy()
            on_choice(value)
        ttk.Button(bar, text="Только в этот раз", command=lambda: choose("once")).pack(side="left", padx=3)
        ttk.Button(bar, text="Всегда", command=lambda: choose("always")).pack(side="left", padx=3)
        ttk.Button(bar, text="Никогда", command=lambda: choose("never")).pack(side="left", padx=3)
        win.protocol("WM_DELETE_WINDOW", lambda: choose("never"))
        win.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - win.winfo_width()) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - win.winfo_height()) // 2)
        win.geometry(f"+{x}+{y}")

    def process_chrome_mic_start(self, payload):
        learned_policy = self.training_data.get("app_rules", {}).get("chrome.exe", "ask")
        if learned_policy == "never":
            self.status.config(text="Chrome ignorируется по правилу панели обучения.")
            return
        if learned_policy == "always":
            self.begin_meeting_countdown(payload)
            return
        policy = self.settings.get("apps", {}).get("chrome.exe")
        if policy == "never":
            self.status.config(text="Доступ Chrome к микрофону игнорируется по настройке «Никогда».")
            return
        if policy == "always":
            self.begin_meeting_countdown(payload)
            return
        if self.pending_app_decision:
            return
        self.pending_app_decision = True
        def decided(choice):
            self.pending_app_decision = None
            if choice in ("always", "never"):
                self.settings.setdefault("apps", {})["chrome.exe"] = choice
                self.save_settings()
            if choice == "never":
                self.status.config(text="Chrome исключён из автоматического обнаружения встреч.")
            else:
                self.begin_meeting_countdown(payload)
        self.ask_app_policy("Google Chrome", decided)

    def begin_meeting_countdown(self, payload=None):
        self.chrome_stop_since = None
        self.chrome_mic_countdown_until = time.time() + 5.0
        self.chrome_mic_countdown_reason = "Windows зафиксировала новое обращение Chrome к микрофону"
        self.status.config(text="Chrome обратился к микрофону. Подтверждение встречи через 5 секунд.")

    def make_tree(self, parent, columns):
        tree=ttk.Treeview(parent,columns=[x[0] for x in columns],show="headings")
        for c,label,width in columns:
            tree.heading(c,text=label); tree.column(c,width=width,anchor="w")
        tree.pack(fill="both",expand=True)
        return tree

    def toggle_live_mode(self):
        if self.live_meeting.get():
            self.analyze_btn.config(state="disabled")
            self.status.config(text="Режим встречи включён: анализ отключён, чтобы не мешать звонку.")
        else:
            self.analyze_btn.config(state="normal")
            self.status.config(text="Режим встречи выключен. Анализ доступен, но может загружать CPU.")

    def on_close(self):
        try:
            self.mic_monitor.close()
            if hasattr(self, "chrome_mic_monitor"):
                self.chrome_mic_monitor.close()
        finally:
            self.root.destroy()

    def release_meeting_guard(self):
        self.meeting_latched = False
        self.meeting_active = False
        self.chrome_mic_countdown_until = None
        self.mic_active_since = None
        self.mic_quiet_since = None
        self.chrome_stop_since = None
        self.analysis_cancel.clear()
        self.status.config(text="Блокировка встречи снята вручную. Ожидающие данные сохранены; анализ снова доступен.")
        if not self.analysis_running:
            self.analyze_btn.config(state="normal")

    def toggle_auto_pause(self):
        if self.auto_pause.get():
            self.analyze_btn.config(state="disabled")
            self.status.config(text="Автоматическая защита включена. Проверяю активные встречи…")
            self.root.after(100, self.meeting_watch_tick)
        else:
            self.analyze_btn.config(state="normal")
            self.status.config(text="Автоматическая защита выключена. Во время встречи анализ может тормозить звонок.")

    def meeting_watch_tick(self):
        if not self.root.winfo_exists():
            return
        signal, reason = detect_meeting(self.mic_monitor)
        now = time.time()

        # Registry events are the experimental Chrome-specific signal. Ignore other apps,
        # and require a cancellable 5-second confirmation window before latching.
        for event_name, payload in self.chrome_mic_monitor.poll():
            if event_name in ("chrome_mic_start", "chrome_mic_stop"):
                stamp = str(payload.get("timestamp", "")) if isinstance(payload, dict) else str(payload)
                dedupe_key = (event_name, stamp)
                if stamp and dedupe_key in self.registry_seen_events:
                    continue
                if stamp:
                    self.registry_seen_events.add(dedupe_key)
                    if len(self.registry_seen_events) > 500:
                        self.registry_seen_events = set(list(self.registry_seen_events)[-250:])
            if event_name == "chrome_mic_start":
                self.record_training_event("Google Chrome", "Доступ к микрофону начался", payload)
                self.process_chrome_mic_start(payload)
            elif event_name == "chrome_mic_stop":
                self.record_training_event("Google Chrome", "Доступ к микрофону завершился", payload)
                # Mic access ending is only a hint; don't end the meeting immediately.
                self.chrome_stop_since = now
                self.status.config(text="Chrome прекратил доступ к микрофону. Проверяю завершение встречи (30 секунд)…")
            elif event_name == "error":
                self.status.config(text=str(payload))
        if self.chrome_mic_countdown_until is not None:
            remaining = self.chrome_mic_countdown_until - now
            if remaining <= 0:
                self.meeting_latched = True
                self.meeting_active = True
                self.last_meeting_reason = self.chrome_mic_countdown_reason
                self.chrome_mic_countdown_until = None
            elif self.auto_pause.get() and not self.meeting_latched:
                self.analyze_btn.config(state="disabled")
                self.status.config(text=f"Chrome использует микрофон. Подтверждение встречи через {max(1, int(remaining + 0.99))} сек. Можно отменить кнопкой «Встреча завершена».")
                self.root.after(500, self.meeting_watch_tick)
                return

        # Release after a confirmed registry stop has remained stable for 30 seconds.
        # A new mic-start event cancels this timer in begin_meeting_countdown().
        if self.meeting_latched and self.chrome_stop_since is not None and now - self.chrome_stop_since >= 30:
            self.meeting_latched = False
            self.meeting_active = False
            self.chrome_stop_since = None
            self.mic_quiet_since = None
            self.status.config(text="Доступ Chrome к микрофону завершён. Режим встречи снят; локальный анализ снова доступен.")
            if not self.analysis_running:
                self.analyze_btn.config(state="normal")

        # Audible output from a meeting app is a strong signal and latches immediately.
        if signal is True:
            state_key = ("meeting_signal", reason)
            if self.last_logged_state != state_key:
                self.record_training_event("Детектор Windows", "Сигнал встречи", reason)
                self.last_logged_state = state_key
            self.meeting_latched = True
            self.mic_active_since = None
            self.mic_quiet_since = None
            self.meeting_active = True
            self.last_meeting_reason = reason
        elif signal == "mic":
            if self.mic_active_since is None:
                self.mic_active_since = now
            self.mic_quiet_since = None
            # Require 5 seconds of continuous mic activity before latching.
            if now - self.mic_active_since >= 5:
                self.meeting_latched = True
                self.meeting_active = True
                self.last_meeting_reason = reason
        elif signal is False:
            state_key = ("no_meeting_signal", reason)
            if self.last_logged_state != state_key:
                self.record_training_event("Детектор Windows", "Встреча не подтверждена", reason)
                self.last_logged_state = state_key
            self.mic_active_since = None
            if self.meeting_latched:
                # Keep the meeting latched while there is a running meeting app.
                try:
                    import psutil
                    running = { (p.info.get("name") or "").lower()
                                for p in psutil.process_iter(["name"])
                                if p.info.get("name") }
                    candidates = running & (MEETING_PROCESS_NAMES | BROWSER_PROCESS_NAMES)
                except Exception:
                    candidates = set()
                if not candidates:
                    if self.mic_quiet_since is None:
                        self.mic_quiet_since = now
                    # Only release after 60 seconds without meeting app or signal.
                    if now - self.mic_quiet_since >= 60:
                        self.meeting_latched = False
                        self.meeting_active = False
                else:
                    self.mic_quiet_since = None
                    self.meeting_active = True
            else:
                self.meeting_active = False
        else:
            # Unknown signal: fail safe if already latched; don't clear it on uncertainty.
            state_key = ("unknown_signal", reason)
            if self.last_logged_state != state_key:
                self.record_training_event("Детектор Windows", "Состояние неизвестно", reason)
                self.last_logged_state = state_key
            self.mic_active_since = None

        if self.auto_pause.get():
            if self.meeting_latched:
                self.analyze_btn.config(state="disabled")
                if self.analysis_running:
                    self.analysis_cancel.set()
                    self.status.config(text="Встреча обнаружена и удерживается. Останавливаю анализ; после встречи продолжу.")
                else:
                    self.status.config(text="Встреча обнаружена. Анализ заблокирован. " + self.last_meeting_reason)
            elif signal == "mic":
                elapsed = int(now - self.mic_active_since) if self.mic_active_since else 0
                self.analyze_btn.config(state="disabled")
                self.status.config(text=f"Проверяю активность микрофона: {elapsed}/5 секунд.")
            elif signal is None:
                self.analyze_btn.config(state="disabled")
                if self.analysis_running:
                    self.analysis_cancel.set()
                self.status.config(text="Не удалось проверить состояние встречи. Анализ заблокирован.")
            else:
                if not self.analysis_running and not self.pending_transcript:
                    self.analyze_btn.config(state="normal")
                    if not self.status.cget("text").startswith("Итоги сформированы"):
                        self.status.config(text=reason)
        elif not self.analysis_running:
            self.analyze_btn.config(state="normal")
        self.root.after(3000, self.meeting_watch_tick)

    def resume_after_meeting(self):
        # Kept for compatibility with older callbacks; release is managed by meeting_watch_tick.
        if not self.root.winfo_exists():
            return
        if self.meeting_latched:
            self.status.config(text="Встреча всё ещё активна. Анализ остаётся заблокирован.")
            return
        if self.pending_transcript:
            transcript, model = self.pending_transcript
            self.pending_transcript = None
            self.start_analysis(transcript, model, automatic=True)
        else:
            self.analyze_btn.config(state="normal")
            self.status.config(text="Встреча завершена. Анализ снова доступен.")

    def open_txt(self):
        p=filedialog.askopenfilename(filetypes=[("Текстовые файлы","*.txt"),("Все файлы","*.*")])
        if not p:return
        try:
            text=Path(p).read_text(encoding="utf-8-sig",errors="replace")
            self.transcript_path=p; self.trans.delete("1.0","end"); self.trans.insert("1.0",text)
            if not self.name.get().strip(): self.name.insert(0,Path(p).stem)
            self.status.config(text=f"Загружено {Path(p).name}: {len(text)} символов.")
        except Exception as e: messagebox.showerror("Ошибка",str(e))

    def check_ollama(self):
        def worker():
            try:
                models=list_models()
                def done():
                    self.status.config(text="Ollama доступна. Модели: "+(", ".join(models) if models else "не установлены"))
                    if not models: messagebox.showwarning("Модель не найдена","Установи модель командой: ollama pull "+self.model.get().strip())
                self.root.after(0,done)
            except Exception as e:
                self.root.after(0,lambda:messagebox.showerror("Ollama недоступна","Не удалось подключиться к локальной Ollama.\n\nЗапусти setup_ollama.bat или проверь, что Ollama установлена.\n\nПодробности: "+str(e)))
        threading.Thread(target=worker,daemon=True).start()

    def analyze(self):
        if self.auto_pause.get():
            state, reason = detect_meeting(self.mic_monitor)
            if state is not False:
                messagebox.showwarning("Анализ заблокирован",
                    "Встреча обнаружена или состояние не удалось проверить. Анализ не запущен.\n\n" + reason)
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
        self.analyze_btn.config(state="disabled")
        label = "Автоматически продолжаю анализ после встречи…" if automatic else "Локальная модель анализирует текст…"
        self.status.config(text=label)

        def worker():
            try:
                result = ollama_request(model, transcript, self.analysis_cancel)
                if self.analysis_cancel.is_set():
                    raise InterruptedError("Обнаружена встреча.")
                self.root.after(0, lambda result=result: self.show_result(result))
                self.root.after(0, lambda: self.status.config(text="Анализ завершён. Проверь результат перед использованием."))
            except InterruptedError:
                self.root.after(0, lambda: self.queue_analysis_after_meeting(transcript, model))
            except Exception as e:
                self.root.after(0, lambda err=str(e): messagebox.showerror("Ошибка анализа", "Не удалось получить результат от Ollama.\n\n" + err))
                self.root.after(0, lambda: self.status.config(text="Анализ не завершён."))
            finally:
                def finish():
                    self.analysis_running = False
                    if self.auto_pause.get() and self.meeting_active:
                        self.analyze_btn.config(state="disabled")
                    elif self.pending_transcript:
                        self.analyze_btn.config(state="disabled")
                    else:
                        self.analyze_btn.config(state="normal")
                self.root.after(0, finish)
        threading.Thread(target=worker, daemon=True).start()

    def queue_analysis_after_meeting(self, transcript, model):
        self.pending_transcript = (transcript, model)
        self.analysis_cancel.clear()
        self.analyze_btn.config(state="disabled")
        self.status.config(text="Анализ остановлен для встречи. Текст сохранён; после встречи анализ будет запущен заново.")
    def show_result(self,result):
        self.data=result
        self.summary.delete("1.0","end")
        self.summary.insert("1.0","\n".join("• "+str(x) for x in result.get("summary",[])) or "Резюме не сформировано.")
        self.fill_tree(self.dec_tree,result.get("decisions",[]),[("text","text"),("evidence","evidence")])
        self.fill_tree(self.task_tree,result.get("tasks",[]),[("task","task"),("owner","owner"),("deadline","deadline"),("confidence","confidence"),("status",None),("evidence","evidence")],task=True)
        self.fill_tree(self.q_tree,result.get("open_questions",[]),[("question","question"),("evidence","evidence")])
        uncertain=result.get("uncertain_fragments",[])
        msg=f"Итоги сформированы. Решений: {len(result.get('decisions',[]))}; задач: {len(result.get('tasks',[]))}; открытых вопросов: {len(result.get('open_questions',[]))}."
        if uncertain: msg+=f" Фрагментов на проверку: {len(uncertain)}."
        self.status.config(text=msg)

    def fill_tree(self,tree,items,mapping,task=False):
        tree.delete(*tree.get_children())
        for idx,item in enumerate(items):
            values=[]
            for col,key in mapping:
                if key is None: values.append("На проверке")
                else: values.append(str(item.get(key,"") or ""))
            tree.insert("","end",iid=str(idx),values=values)

    def task_status(self,status):
        for iid in self.task_tree.selection():
            vals=list(self.task_tree.item(iid,"values")); vals[4]=status; self.task_tree.item(iid,values=vals)
            idx=int(iid)
            if idx < len(self.data.get("tasks",[])): self.data["tasks"][idx]["status"]=status

    def export_tasks(self):
        p=filedialog.asksaveasfilename(defaultextension=".csv",initialfile="MeetingHub_tasks.csv",filetypes=[("CSV","*.csv")])
        if not p:return
        with open(p,"w",newline="",encoding="utf-8-sig") as f:
            w=csv.writer(f); w.writerow(["Задача","Ответственный","Срок","Уверенность","Статус","Фрагмент-основание"])
            for iid in self.task_tree.get_children(): w.writerow(self.task_tree.item(iid,"values"))
        messagebox.showinfo("Экспорт","Задачи сохранены.")

    def save_project(self):
        p=filedialog.asksaveasfilename(defaultextension=".json",initialfile="MeetingHub_project.json",filetypes=[("JSON","*.json")])
        if not p:return
        data={"name":self.name.get(),"date":self.date.get(),"model":self.model.get(),"source_file":self.transcript_path,
              "transcript":self.trans.get("1.0","end-1c"),"analysis":self.data,
              "summary":self.summary.get("1.0","end-1c"),
              "decisions":[self.dec_tree.item(i,"values") for i in self.dec_tree.get_children()],
              "tasks":[self.task_tree.item(i,"values") for i in self.task_tree.get_children()],
              "questions":[self.q_tree.item(i,"values") for i in self.q_tree.get_children()]}
        Path(p).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
        messagebox.showinfo("Сохранение","Проект сохранён.")

    def open_project(self):
        p=filedialog.askopenfilename(filetypes=[("JSON","*.json")])
        if not p:return
        try:
            d=json.loads(Path(p).read_text(encoding="utf-8"))
            self.name.delete(0,"end"); self.name.insert(0,d.get("name",""))
            self.date.delete(0,"end"); self.date.insert(0,d.get("date",""))
            self.model.delete(0,"end"); self.model.insert(0,d.get("model",DEFAULT_MODEL))
            self.trans.delete("1.0","end"); self.trans.insert("1.0",d.get("transcript",""))
            self.transcript_path=d.get("source_file",""); self.data=d.get("analysis",{})
            self.summary.delete("1.0","end"); self.summary.insert("1.0",d.get("summary",""))
            for tree,key in [(self.dec_tree,"decisions"),(self.task_tree,"tasks"),(self.q_tree,"questions")]:
                tree.delete(*tree.get_children())
                for i,row in enumerate(d.get(key,[])): tree.insert("","end",iid=str(i),values=row)
            self.status.config(text="Проект открыт: "+Path(p).name)
        except Exception as e: messagebox.showerror("Ошибка открытия",str(e))

if __name__=="__main__":
    root=tk.Tk(); app=App(root)
    for child in root.winfo_children():
        if isinstance(child,ttk.Frame):
            for w in child.winfo_children():
                if isinstance(w,ttk.Button) and w.cget("text")=="Сохранить проект": w.configure(command=app.save_project)
                if isinstance(w,ttk.Button) and w.cget("text")=="Открыть проект": w.configure(command=app.open_project)
    root.mainloop()
