import os, json, sqlite3, threading, subprocess, wave, tempfile, ctypes, time
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox
import numpy as np
import soundcard as sc
import soundfile as sf
import pystray
from PIL import Image, ImageDraw

APP=Path(__file__).resolve().parent
VERSION="1.1.21"
DATA=Path(os.getenv("LOCALAPPDATA",APP))/ "Meeting Hub"
DATA.mkdir(parents=True,exist_ok=True)
RECORDINGS=DATA/"recordings"; RECORDINGS.mkdir(exist_ok=True)
DB=DATA/"meetinghub.db"
WHISPER=os.getenv("MH_WHISPER_MODEL","base")
OLLAMA=os.getenv("MH_OLLAMA_MODEL","qwen2.5:1.5b-instruct")

def db():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c

def init():
    c=db(); c.executescript("""CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY,name TEXT UNIQUE,summary TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS meetings(id INTEGER PRIMARY KEY,title TEXT,project TEXT,started TEXT,audio TEXT,transcript TEXT,summary TEXT,decisions TEXT,questions TEXT,points TEXT,status TEXT);
    CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY,meeting_id INTEGER,project TEXT,task TEXT,owner TEXT,deadline TEXT,status TEXT,created TEXT);"""); c.commit(); c.close()
init()

def clean(s): return " ".join(str(s or "").split())

def transcribe_stereo(path):
    try:
        with wave.open(str(path),"rb") as w: ch=w.getnchannels(); rate=w.getframerate(); raw=w.readframes(w.getnframes())
        if ch<2: return transcribe_file(path)
        a=np.frombuffer(raw,np.int16).reshape(-1,ch)
        from faster_whisper import WhisperModel
        model=WhisperModel(WHISPER,device="cpu",compute_type="int8"); out=[]
        with tempfile.TemporaryDirectory() as td:
            for i,label in ((0,"Вы"),(1,"Собеседники")):
                p=Path(td)/f"c{i}.wav"
                with wave.open(str(p),"wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(a[:,i].tobytes())
                segs,_=model.transcribe(str(p),beam_size=1,vad_filter=True,language="ru")
                got=False
                for s in segs:
                    t=clean(s.text)
                    if t:
                        got=True; out.append((float(s.start),label,t))
                # Если VAD решил, что дорожка слишком тихая, пробуем без VAD.
                if not got:
                    segs,_=model.transcribe(str(p),beam_size=1,vad_filter=False,language="ru")
                    for s in segs:
                        t=clean(s.text)
                        if t: out.append((float(s.start),label,t))
        out.sort()
        text="\n".join(f"[{int(x//60):02d}:{x%60:04.1f}] {sp}: {t}" for x,sp,t in out)
        if text: return text
        # Крайний fallback: транскрибируем готовую смешанную запись целиком.
        return transcribe_file(path)
    except Exception as e: return "[Ошибка транскрибации: %s]"%e

def transcribe_file(path):
    try:
        from faster_whisper import WhisperModel
        model=WhisperModel(WHISPER,device="cpu",compute_type="int8"); segs,_=model.transcribe(str(path),beam_size=1,vad_filter=True,language="ru")
        return " ".join(clean(x.text) for x in segs if clean(x.text))
    except Exception as e: return "[Ошибка транскрибации: %s]"%e

def find_ollama():
    candidates=[os.getenv("OLLAMA_EXE",""),os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"),os.path.expandvars(r"%LOCALAPPDATA%\Ollama\ollama.exe"),r"C:\Program Files\Ollama\ollama.exe"]
    for p in candidates:
        if p and Path(p).exists(): return p
    try:
        r=subprocess.run(["where","ollama"],capture_output=True,text=True,timeout=5)
        if r.returncode==0 and r.stdout.strip(): return r.stdout.splitlines()[0].strip()
    except Exception: pass
    return None

def ai_analyze(transcript,project=""):
    prompt=f"""Ты помощник Meeting Hub. Сделай очень краткий разбор новой встречи на русском.
Нужны ТОЛЬКО:
1) 3-7 коротких тезисов о том, что обсуждали и к чему пришли.
2) Только реальные задачи, которые были поставлены на встрече.
Не выдумывай задачи, людей или сроки. Если ответственный/срок не названы — оставь поле пустым.
Верни ТОЛЬКО JSON:
{{"title":"","project":"{project or ""}","points":[],"tasks":[{{"task":"","owner":"","deadline":""}}]}}
РАСШИФРОВКА:
{transcript[:30000]}"""
    exe=find_ollama()
    if not exe:
        return {"title":"","project":project,"points":[],"tasks":[],"ai_error":True,"error":"Локальный AI не найден. Запустите установщик Meeting Hub ещё раз или дождитесь установки локального AI."}
    try:
        # Ollama — консольное Windows-приложение. Не показываем его окно пользователю.
        si=None; flags=0
        if os.name=="nt":
            si=subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            flags=subprocess.CREATE_NO_WINDOW
        r=subprocess.run(
            [exe,"run",OLLAMA,prompt,"--format","json"],
            capture_output=True,text=True,encoding="utf-8",errors="replace",
            timeout=300,stdin=subprocess.DEVNULL,startupinfo=si,creationflags=flags
        )
        s=r.stdout.strip(); a=s.find("{"); b=s.rfind("}")
        if a>=0 and b>a:
            raw=s[a:b+1]
            # Модели иногда возвращают реальные переводы строк/табуляции внутри JSON-строк.
            # Нормализуем только управляющие символы внутри кавычек, не ломая структуру JSON.
            fixed=[]; quoted=False; escaped=False
            for ch in raw:
                if escaped:
                    fixed.append(ch); escaped=False; continue
                if ch=="\\": fixed.append(ch); escaped=True; continue
                if ch=='"': fixed.append(ch); quoted=not quoted; continue
                if quoted and ord(ch)<32:
                    fixed.append("\\n" if ch=="\\n" else ("\\t" if ch=="\\t" else " "))
                else:
                    fixed.append(ch)
            d=json.loads("".join(fixed)); d.setdefault("points",[]); d.setdefault("tasks",[]); return d
        if r.returncode!=0: raise RuntimeError(r.stderr.strip() or "Ollama завершил работу с ошибкой")
        return {"title":"","project":project,"points":[s[:3000]],"tasks":[]}
    except Exception as e:
        return {"title":"","project":project,"points":[],"tasks":[],"ai_error":True,"error":"Ошибка локального AI: "+str(e)}

class Recorder:
    def __init__(self,mic=None,speaker=None):
        self.mic=mic; self.speaker=speaker; self.rate=48000; self.run=False; self.m=[]; self.s=[]; self.muted=False
        self.silence_started=None; self.level=0.0
    def is_silent(self):
        if not self.m or not self.s: return False
        try:
            m=np.concatenate(self.m[-4:])[:,0]
            ss=np.concatenate(self.s[-4:])[:,0]
            if len(m)==0 or len(ss)==0: return False
            rms=max(float(np.sqrt(np.mean(m*m))),float(np.sqrt(np.mean(ss*ss))))
            return rms < 0.003
        except Exception: return False
    def set_mute(self,value): self.muted=value
    def start(self):
        self.run=True
        self.silence_started=None
        mic=sc.get_microphone(self.mic,include_loopback=False) if self.mic else sc.default_microphone()
        sp=sc.get_speaker(self.speaker) if self.speaker else sc.default_speaker()
        loop=sc.get_microphone(id=str(sp.id),include_loopback=True)
        def rec(dev,out,is_mic=False):
            try:
                with dev.recorder(samplerate=self.rate,channels=1) as r:
                    while self.run:
                        block=r.record(int(self.rate*.25))
                        if is_mic and self.muted: block=np.zeros_like(block)
                        out.append(block)
                        try:
                            lv=float(np.sqrt(np.mean(block*block)))
                            self.level=max(self.level*0.35,min(1.0,lv*8.0))
                        except Exception: pass
            except Exception: pass
        threading.Thread(target=rec,args=(mic,self.m,True),daemon=True).start()
        threading.Thread(target=rec,args=(loop,self.s,False),daemon=True).start()
    def stop(self,path,source_path=None):
        self.run=False; time.sleep(.6)
        if not self.m or not self.s: return None
        m=np.concatenate(self.m)[:,0]; s=np.concatenate(self.s)[:,0]; n=min(len(m),len(s))
        m=m[:n].astype(np.float32); s=s[:n].astype(np.float32)
        # Исходник сохраняем раздельно: L=микрофон, R=системный звук.
        if source_path:
            sf.write(str(source_path),np.column_stack((m,s)),self.rate,subtype="PCM_16")
            sp=Path(source_path)
            sf.write(str(sp.with_name(sp.stem.replace("_source","_mic")+".wav")),m,self.rate,subtype="PCM_16")
            sf.write(str(sp.with_name(sp.stem.replace("_source","_system")+".wav")),s,self.rate,subtype="PCM_16")
        # Для обычного прослушивания оба источника должны быть слышны в обоих ушах.
        mix=(m+s)/2.0
        peak=float(np.max(np.abs(mix))) if len(mix) else 0.0
        if peak>0.98: mix=mix*(0.98/peak)
        sf.write(str(path),np.column_stack((mix,mix)),self.rate,subtype="PCM_16")
        return path

MEETING_PROCS=("zoom.exe","ms-teams.exe","teams.exe","webex.exe","webexmta.exe","skype.exe")
TITLE_KEYS=("zoom meeting","zoom meeting controls","microsoft teams","teams meeting","google meet","meet.google.com","webex","yandex telemost","яндекс телемост","телемост","webex meeting","конференция","conference call")

def windows_meeting_title():
    found=[]; user32=ctypes.windll.user32; kernel32=ctypes.windll.kernel32
    EnumProc=ctypes.WINFUNCTYPE(ctypes.c_bool,ctypes.c_void_p,ctypes.c_void_p)
    def cb(hwnd,lparam):
        if not user32.IsWindowVisible(hwnd): return True
        buf=ctypes.create_unicode_buffer(512); user32.GetWindowTextW(hwnd,buf,512); title=buf.value.strip()
        if not title: return True
        pid=ctypes.c_ulong(); user32.GetWindowThreadProcessId(hwnd,ctypes.byref(pid)); process=""
        h=kernel32.OpenProcess(0x1000|0x0010,False,pid.value)
        if h:
            pbuf=ctypes.create_unicode_buffer(512); size=ctypes.c_ulong(512)
            if kernel32.QueryFullProcessImageNameW(h,0,pbuf,ctypes.byref(size)): process=Path(pbuf.value).name.lower()
            kernel32.CloseHandle(h)
        low=title.lower()
        if process in ("meetinghub.exe","meetinghub-debug.exe"): return True
        if process in MEETING_PROCS or any(k in low for k in TITLE_KEYS): found.append((title,process))
        return True
    user32.EnumWindows(EnumProc(cb),0); return found[0] if found else None

def tray_image(recording=False,muted=False):
    img=Image.new("RGBA",(64,64),(25,29,38,255)); d=ImageDraw.Draw(img)
    d.rounded_rectangle((22,10,42,39),8,fill=(230,235,245,255))
    d.arc((15,25,49,53),0,180,fill=(230,235,245,255),width=4); d.line((32,48,32,56),fill=(230,235,245,255),width=4); d.line((23,57,41,57),fill=(230,235,245,255),width=4)
    if recording:
        d.ellipse((46,5,60,19),fill=(220,65,65,255))
    if muted:
        d.line((10,10,54,54),fill=(220,65,65,255),width=5)
    return img

class Review(tk.Toplevel):
    def __init__(self,parent,data,save):
        super().__init__(parent); self.title("Проверка встречи"); self.geometry("850x620"); self.grab_set(); self.data=data; self.save=save
        f=ttk.Frame(self,padding=15); f.pack(fill="both",expand=True)
        ttk.Label(f,text="Тезисы встречи").pack(anchor="w")
        self.points=tk.Text(f,height=10); self.points.pack(fill="x",pady=(3,12)); self.points.insert("1.0","\n".join(data.get("points",[]) or []))
        ttk.Label(f,text="Задачи: задача | ответственный | срок").pack(anchor="w")
        self.tasks=tk.Text(f,height=10); self.tasks.pack(fill="both",expand=True,pady=(3,8))
        for x in data.get("tasks",[]) or []: self.tasks.insert("end",f"{x.get('task','')} | {x.get('owner','')} | {x.get('deadline','')}\n")
        if data.get("ai_error"): ttk.Label(f,text="⚠ "+data.get("error","AI недоступен"),foreground="#c65").pack(anchor="w",pady=5)
        ttk.Button(f,text="✓ Сохранить встречу",command=self.apply).pack(anchor="e",pady=8)
    def apply(self):
        d=dict(self.data); d["points"]=[clean(x) for x in self.points.get("1.0","end").splitlines() if clean(x)]; d["tasks"]=[]
        for line in self.tasks.get("1.0","end").splitlines():
            p=[x.strip() for x in line.split("|")]
            if p and p[0]: d["tasks"].append({"task":p[0],"owner":p[1] if len(p)>1 else "","deadline":p[2] if len(p)>2 else ""})
        self.save(d); self.destroy()

class MeetingDetail(tk.Frame):
    def __init__(self,parent,m,on_back=None):
        super().__init__(parent,bg="#101218")
        self.on_back=on_back
        f=ttk.Frame(self,padding=18); f.pack(fill="both",expand=True)
        ttk.Button(f,text="← К встречам",command=self.go_back).pack(anchor="w",pady=(0,8))
        ttk.Label(f,text=m["title"] or "Без названия",font=("Segoe UI",18,"bold")).pack(anchor="w")
        ttk.Label(f,text=f"{m['started'][:16]}   •   {m['project'] or 'Без проекта'}").pack(anchor="w",pady=(2,12))
        ttk.Label(f,text="Тезисы",font=("Segoe UI",10,"bold")).pack(anchor="w")
        t=tk.Text(f,height=8); t.pack(fill="x",pady=(2,10)); val=m["points"] or ""
        try: val="\n".join(json.loads(val))
        except Exception: pass
        t.insert("1.0",val); t.config(state="disabled")
        ttk.Label(f,text="Задачи",font=("Segoe UI",10,"bold")).pack(anchor="w")
        q=tk.Text(f,height=7); q.pack(fill="x",pady=(2,10))
        c=db(); tasks=c.execute("select * from tasks where meeting_id=? order by id",(m["id"],)).fetchall(); c.close()
        q.insert("1.0","\n".join(f"{x['task']} | {x['owner'] or ''} | {x['deadline'] or ''}" for x in tasks)); q.config(state="disabled")
        ttk.Label(f,text="Расшифровка",font=("Segoe UI",10,"bold")).pack(anchor="w")
        tr=tk.Text(f); tr.pack(fill="both",expand=True); tr.insert("1.0",m["transcript"] or ""); tr.config(state="disabled")
        if m["status"] in ("transcribing","transcription_error") or not (m["transcript"] or "").strip():
            ttk.Button(f,text="↻ Повторить транскрибацию",command=lambda:self.retry_transcription(m["id"],m["audio"])).pack(anchor="w",pady=(4,6))
        row=ttk.Frame(f); row.pack(fill="x",pady=(8,0))
        ttk.Button(row,text="▶ Открыть запись",command=lambda:self.play(m["audio"])).pack(side="left")
        ttk.Button(row,text="🎤 Микрофон",command=lambda:self.play(self.track_path(m["audio"],"mic"))).pack(side="left",padx=6)
        ttk.Button(row,text="🔊 Системный звук",command=lambda:self.play(self.track_path(m["audio"],"system"))).pack(side="left",padx=6)
        ttk.Button(row,text="🎚 Исходник L/R",command=lambda:self.play(self.source_path(m["audio"]))).pack(side="left",padx=6)
        ttk.Button(row,text="📁 Папка",command=lambda:open_recordings_folder()).pack(side="left",padx=6)
        ttk.Button(row,text="🗑 Удалить встречу",command=lambda:self.delete_meeting(m["id"])).pack(side="right")
        src=self.source_path(m["audio"]); mic=self.track_path(m["audio"],"mic"); system=self.track_path(m["audio"],"system")
        ttk.Label(f,text="Запись: смешанный звук (оба источника в обоих каналах)\\nМикрофон: "+mic+"\\nСистемный звук: "+system+"\\nИсходник L/R: "+src,wraplength=820).pack(anchor="w",pady=(8,0))
    def go_back(self):
        if self.on_back: self.on_back()
    def source_path(self,path):
        if not path: return ""
        p=Path(path)
        return str(p.with_name(p.stem+"_source.wav"))
    def track_path(self,path,kind):
        if not path: return ""
        p=Path(path)
        return str(p.with_name(p.stem+"_"+kind+".wav"))
    def delete_meeting(self,meeting_id):
        if not messagebox.askyesno("Удалить встречу","Удалить встречу, расшифровку, задачи и все связанные аудиофайлы?\\n\\nЭто действие нельзя отменить."):
            return
        try:
            c=db()
            row=c.execute("select audio from meetings where id=?",(meeting_id,)).fetchone()
            if row:
                audio=row["audio"]
                paths=[audio,self.source_path(audio),self.track_path(audio,"mic"),self.track_path(audio,"system")]
                c.execute("delete from tasks where meeting_id=?",(meeting_id,))
                c.execute("delete from meetings where id=?",(meeting_id,))
                c.commit()
                c.close()
                for path in paths:
                    if path:
                        try: Path(path).unlink(missing_ok=True)
                        except Exception: pass
            else:
                c.close()
            self.go_back()
        except Exception as e:
            try: c.rollback(); c.close()
            except Exception: pass
            messagebox.showerror("Удаление встречи","Не удалось удалить встречу:\\n"+str(e))
    def retry_transcription(self,meeting_id,audio):
        if not audio or not Path(audio).exists():
            messagebox.showerror("Транскрибация","Файл записи не найден.")
            return
        source=self.source_path(audio)
        if not Path(source).exists():
            source=audio
        c=db()
        c.execute("update meetings set status=?,transcript=? where id=?",("transcribing","",meeting_id)); c.commit(); c.close()
        self.status.set("Расшифровка выполняется в фоне…")
        threading.Thread(target=self.process_meeting_background,args=(meeting_id,audio,source),daemon=True).start()
    def play(self,path):
        if not path or not Path(path).exists(): messagebox.showerror("Запись","Файл записи не найден"); return
        try: os.startfile(path)
        except Exception as e: messagebox.showerror("Запись",str(e))

def open_recordings_folder():
    RECORDINGS.mkdir(exist_ok=True)
    try: os.startfile(str(RECORDINGS))
    except Exception: pass

class AutoDetector:
    def __init__(self,app): self.app=app; self.run=True; self.pending=False; threading.Thread(target=self.loop,daemon=True).start()
    def loop(self):
        while self.run:
            try:
                hit=windows_meeting_title()
                if not hit:
                    self.app.force_stop_lock=False
                if self.app.auto_enabled and hit and not self.app.rec and not self.pending and not self.app.force_stop_lock:
                    self.pending=True; self.app.after(0,lambda h=hit:self._start(h))
                elif self.app.rec and self.app.auto_mode and not self.app.processing:
                    if not hit:
                        self.app.after(0,self.app.auto_stop)
                    elif self.app.rec.is_silent():
                        if self.app.rec.silence_started is None:
                            self.app.rec.silence_started=time.time()
                        elif time.time()-self.app.rec.silence_started >= 90:
                            self.app.after(0,self.app.auto_stop)
                    else:
                        self.app.rec.silence_started=None
            except Exception: pass
            time.sleep(2)
    def _start(self,hit):
        self.pending=False
        if not self.app.rec: self.app.auto_start(hit[0])

class App(tk.Tk):
    def __init__(self):
        super().__init__(); self.title("Meeting Hub — v"+VERSION); self.geometry("1050x700"); self.configure(bg="#101218")
        self.rec=None; self.started=None; self.auto_mode=False; self.auto_enabled=True; self.processing=False; self.mic_muted=False; self.last_recording=None; self.force_stop_lock=False; self.pending_meeting_id=None
        self.project=tk.StringVar(); self.titlev=tk.StringVar(value="Новая встреча"); self.status=tk.StringVar(value="Ожидание встречи")
        self.countdown_window=None; self.countdown_after=None; self.countdown_value=0; self.countdown_title=""
        self.recording_overlay=None; self.overlay_after=None; self.overlay_hidden=False
        self.build(); self.setup_tray(); self.detector=AutoDetector(self); self.protocol("WM_DELETE_WINDOW",self.hide_to_tray)
    def build(self):
        left=tk.Frame(self,bg="#151820",width=190); left.pack(side="left",fill="y"); left.pack_propagate(False)
        tk.Label(left,text="MEETING",bg="#151820",fg="white",font=("Segoe UI",11,"bold")).pack(pady=(25,0)); tk.Label(left,text="HUB",bg="#151820",fg="#7c8cff",font=("Segoe UI",20,"bold")).pack(pady=(0,2))
        tk.Label(left,text="v"+VERSION,bg="#151820",fg="#6f7787",font=("Segoe UI",9)).pack(pady=(0,25))
        for name,cmd in [("Главная",self.home),("Новая встреча",self.new),("Встречи",self.meetings),("Задачи",self.tasks)]: tk.Button(left,text=name,command=cmd,anchor="w",bg="#151820",fg="#b0b7c5",bd=0,padx=20,pady=10).pack(fill="x")
        self.home()
    def setup_tray(self):
        menu=pystray.Menu(
            pystray.MenuItem(lambda item:self.tray_status(),lambda icon,item:False),
            pystray.MenuItem(lambda item:"✓ Автоподключение включено" if self.auto_enabled else "○ Автоподключение выключено",self.toggle_auto),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda item:"■ Принудительно остановить запись",self.tray_stop,enabled=lambda item:self.rec is not None),
            pystray.MenuItem(lambda item:"🔇 Включить микрофон" if self.mic_muted else "🔇 Выключить микрофон",self.tray_mute,enabled=lambda item:self.rec is not None),
            pystray.MenuItem("📁 Открыть записи",lambda icon,item:open_recordings_folder()),
            pystray.MenuItem("Последние записи",pystray.Menu(lambda:self.recording_items())),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Открыть Meeting Hub",lambda icon,item:self.show_window()),
            pystray.MenuItem("Выйти",lambda icon,item:self.quit_app())
        )
        menu=pystray.Menu(*list(menu), pystray.MenuItem("Meeting Hub v"+VERSION,lambda icon,item:None,enabled=False))
        self.icon=pystray.Icon("MeetingHub",tray_image(), "Meeting Hub v"+VERSION,menu)
        try:
            self.icon.run_detached()
        except Exception:
            threading.Thread(target=self.icon.run,daemon=True).start()
    def toggle_auto(self,icon,item):
        self.after(0,self.toggle_auto_enabled)
    def toggle_auto_enabled(self):
        self.auto_enabled=not self.auto_enabled
        if not self.auto_enabled: self.auto_mode=False
        self.refresh_tray()
        self.status.set("Автоподключение: " + ("включено" if self.auto_enabled else "выключено"))
        self.home()
    def tray_status(self):
        if self.rec: return ("🔴 Запись идёт" + (" • микрофон выключен" if self.mic_muted else ""))
        return "⚪ Ожидание встречи"
    def _recording_action(self,path):
        def action(icon,item):
            try: os.startfile(str(path))
            except Exception: pass
        return action
    def recording_items(self):
        files=sorted(RECORDINGS.glob("*.wav"),key=lambda p:p.stat().st_mtime,reverse=True)[:8]
        if not files: return [pystray.MenuItem("Записей пока нет",lambda i,j:None,enabled=False)]
        return [pystray.MenuItem(p.name,self._recording_action(p)) for p in files]
    def refresh_tray(self):
        try:
            self.icon.icon=tray_image(bool(self.rec),self.mic_muted)
            self.icon.title=self.tray_status()
            self.icon.update_menu()
        except Exception: pass
    def clear(self):
        for x in self.main.winfo_children(): x.destroy()
    def home(self):
        if not hasattr(self,"main"): self.main=tk.Frame(self,bg="#101218"); self.main.pack(fill="both",expand=True)
        self.clear(); tk.Label(self.main,text="Meeting Hub",bg="#101218",fg="white",font=("Segoe UI",24,"bold")).pack(anchor="w",padx=30,pady=30)
        c=db(); n=c.execute("select count(*) n from meetings").fetchone()["n"]; t=c.execute("select count(*) n from tasks where status='open'").fetchone()["n"]; c.close()
        tk.Label(self.main,text=f"Встреч: {n}    Открытых задач: {t}",bg="#101218",fg="#9ba3b3",font=("Segoe UI",12)).pack(anchor="w",padx=30)
        tk.Label(self.main,text="● Автоподключение к встречам включено",bg="#101218",fg="#45d19a").pack(anchor="w",padx=30,pady=(18,0))
        tk.Label(self.main,text="Запись управляется из системного трея: остановить запись или выключить микрофон можно прямо там.",bg="#101218",fg="#9ba3b3",wraplength=800,justify="left").pack(anchor="w",padx=30,pady=(10,0))
        ttk.Button(self.main,text="＋ Новая встреча",command=self.new).pack(anchor="w",padx=30,pady=25)
    def new(self):
        self.clear(); f=tk.Frame(self.main,bg="#101218"); f.pack(fill="both",expand=True,padx=30,pady=25); tk.Label(f,text="Новая встреча",bg="#101218",fg="white",font=("Segoe UI",20,"bold")).pack(anchor="w")
        for lab,var in [("Название",self.titlev),("Проект",self.project)]:
            tk.Label(f,text=lab,bg="#101218",fg="#aab2c2").pack(anchor="w",pady=(18,4)); tk.Entry(f,textvariable=var,bg="#1b202a",fg="white",insertbackground="white",bd=0).pack(fill="x",ipady=9)
        tk.Label(f,text="Meeting Hub автоматически обнаружит открытую встречу и начнёт запись.",bg="#101218",fg="#9ba3b3").pack(anchor="w",pady=20); tk.Label(f,textvariable=self.status,bg="#101218",fg="#45d19a").pack(anchor="w")
        self.rec_btn=tk.Button(f,text="● НАЧАТЬ ЗАПИСЬ",command=self.toggle,bg="#7c8cff",fg="white",bd=0,padx=20,pady=12); self.rec_btn.pack(anchor="w",pady=12)
    def show_window(self): self.deiconify(); self.lift(); self.focus_force()
    def hide_to_tray(self): self.withdraw()
    def show_recording_overlay(self):
        self.overlay_hidden=False
        if self.recording_overlay:
            self.recording_overlay.deiconify(); self.recording_overlay.lift(); return
        w=tk.Toplevel(self); self.recording_overlay=w
        w.overrideredirect(True); w.attributes("-topmost",True); w.configure(bg="#181a20")
        w.bind("<Enter>",lambda e:self.set_overlay_expanded(True))
        w.bind("<Leave>",lambda e:self.set_overlay_expanded(False))
        body=tk.Frame(w,bg="#181a20",padx=10,pady=7); body.pack()
        self.overlay_dot=tk.Label(body,text="●",bg="#181a20",fg="#20a8d8",font=("Segoe UI",13,"bold")); self.overlay_dot.pack(side="left",padx=(0,6))
        self.overlay_bars=tk.Canvas(body,width=54,height=22,bg="#181a20",highlightthickness=0); self.overlay_bars.pack(side="left")
        self.overlay_timer=tk.Label(body,text="00:00",bg="#181a20",fg="#d8dce5",font=("Segoe UI",9,"bold")); self.overlay_timer.pack(side="left",padx=7)
        self.overlay_controls=tk.Frame(body,bg="#181a20")
        self.overlay_mute=tk.Label(self.overlay_controls,text="🎤",bg="#181a20",fg="#d8dce5",font=("Segoe UI",11),padx=7,pady=2,cursor="hand2")
        self.overlay_mute.bind("<ButtonRelease-1>",lambda e:self.toggle_mute())
        self.overlay_mute.pack(side="left")
        self.overlay_stop=tk.Label(self.overlay_controls,text="■",bg="#e84b55",fg="white",font=("Segoe UI",10,"bold"),padx=8,pady=3,cursor="hand2")
        self.overlay_stop.bind("<ButtonRelease-1>",lambda e:self.force_stop_recording())
        self.overlay_stop.pack(side="left",padx=3)
        self.overlay_close=tk.Label(self.overlay_controls,text="×",bg="#181a20",fg="#8d93a0",font=("Segoe UI",12),padx=5,pady=1,cursor="hand2")
        self.overlay_close.bind("<ButtonRelease-1>",lambda e:self.hide_recording_overlay())
        self.overlay_close.pack(side="left")
        self.set_overlay_expanded(False)
        w.update_idletasks(); self.position_overlay(); self.update_overlay()

    def position_overlay(self):
        if not self.recording_overlay: return
        self.recording_overlay.update_idletasks()
        sw=self.recording_overlay.winfo_screenwidth()
        self.recording_overlay.geometry(f"+{max(10,sw-self.recording_overlay.winfo_width()-18)}+18")

    def set_overlay_expanded(self,value):
        if not self.recording_overlay: return
        if value: self.overlay_controls.pack(side="left")
        else: self.overlay_controls.pack_forget()
        self.position_overlay()

    def hide_recording_overlay(self):
        self.overlay_hidden=True
        if self.recording_overlay: self.recording_overlay.withdraw()

    def destroy_recording_overlay(self):
        if self.overlay_after:
            try: self.after_cancel(self.overlay_after)
            except Exception: pass
        self.overlay_after=None
        if self.recording_overlay:
            try: self.recording_overlay.destroy()
            except Exception: pass
        self.recording_overlay=None

    def update_overlay(self):
        if not self.recording_overlay or not self.rec: return
        try:
            sec=int((datetime.now()-self.started).total_seconds()) if self.started else 0
            self.overlay_timer.config(text=f"{sec//60:02d}:{sec%60:02d}")
            self.overlay_mute.config(text="🔇" if self.mic_muted else "🎤")
            c=self.overlay_bars; c.delete("all"); level=getattr(self.rec,"level",0.0)
            vals=[max(2,min(20,int(level*(i+1)/6*20)+2)) for i in range(6)]
            for i,h in enumerate(vals): c.create_rectangle(i*9+1,22-h,i*9+7,22,fill="#20a8d8",outline="")
        except Exception: pass
        self.overlay_after=self.after(200,self.update_overlay)

    def start_recording(self,title=None,auto=False):
        if self.rec: return
        try:
            self.rec=Recorder(); self.rec.start(); self.started=datetime.now(); self.auto_mode=auto; self.processing=False
            if title: self.titlev.set(title[:120])
            self.status.set("🔴 Идёт запись" + (" • автоматически" if auto else ""))
            if hasattr(self,"rec_btn"): self.rec_btn.config(text="■ ОСТАНОВИТЬ")
            self.show_recording_overlay()
            self.refresh_tray()
        except Exception as e: self.rec=None; messagebox.showerror("Аудио",str(e))
    def auto_start(self,title):
        self.after(0,lambda:self.start_countdown(title))
    def start_countdown(self,title):
        if self.rec or self.processing or self.countdown_window is not None: return
        self.countdown_title=title or "Встреча"
        self.countdown_value=5
        self.pending=False
        try:
            w=tk.Toplevel(self)
            self.countdown_window=w
            w.overrideredirect(True)
            w.attributes("-topmost",True)
            w.configure(bg="#17191f")
            frame=tk.Frame(w,bg="#17191f",padx=14,pady=9)
            frame.pack()
            tk.Label(frame,text="◉",bg="#17191f",fg="#45b7e8",font=("Segoe UI",14,"bold")).pack(side="left",padx=(0,8))
            self.countdown_label=tk.Label(frame,text="",bg="#17191f",fg="white",font=("Segoe UI",10,"bold"))
            self.countdown_label.pack(side="left")
            cancel_lbl=tk.Label(frame,text="Отмена",bg="#343842",fg="white",font=("Segoe UI",9,"bold"),padx=12,pady=4,cursor="hand2")
            cancel_lbl.bind("<ButtonRelease-1>",lambda e:self.cancel_countdown())
            cancel_lbl.pack(side="left",padx=(14,0))
            w.update_idletasks()
            sw=w.winfo_screenwidth()
            x=max(10,sw-w.winfo_width()-18)
            w.geometry(f"+{x}+18")
            self._countdown_tick()
        except Exception:
            self.countdown_window=None
            self.start_recording(title,True)

    def _countdown_tick(self):
        w=self.countdown_window
        if w is None: return
        if not windows_meeting_title():
            self._close_countdown()
            self.force_stop_lock=False
            self.status.set("Встреча не обнаружена")
            return
        self.countdown_label.config(text=f"{self.countdown_title} — запись через {self.countdown_value}")
        if self.countdown_value<=1:
            self._close_countdown()
            self.start_recording(self.countdown_title,True)
            return
        self.countdown_value-=1
        self.countdown_after=self.after(1000,self._countdown_tick)

    def cancel_countdown(self):
        self._close_countdown()
        self.force_stop_lock=True
        self.status.set("Запись отменена")
        self.refresh_tray()

    def _close_countdown(self):
        if self.countdown_after:
            try: self.after_cancel(self.countdown_after)
            except Exception: pass
        self.countdown_after=None
        if self.countdown_window:
            try: self.countdown_window.destroy()
            except Exception: pass
        self.countdown_window=None

    def toggle(self):
        if not self.rec: self.start_recording()
        else: self.stop_recording()
    def auto_stop(self): self.stop_recording()
    def tray_stop(self,icon,item): self.after(0,self.force_stop_recording)
    def force_stop_recording(self):
        if not self.rec or self.processing: return
        self.force_stop_lock=True
        self.auto_mode=False
        self.stop_recording()
    def tray_mute(self,icon,item):
        self.after(0,self.toggle_mute)
    def toggle_mute(self):
        if not self.rec: return
        self.mic_muted=not self.mic_muted; self.rec.set_mute(self.mic_muted)
        self.status.set("🔴 Идёт запись • " + ("микрофон выключен" if self.mic_muted else "микрофон включён"))
        self.refresh_tray()
    def stop_recording(self):
        if not self.rec or self.processing: return
        self.destroy_recording_overlay()
        self.processing=True; p=RECORDINGS/(datetime.now().strftime("%Y%m%d_%H%M%S")+".wav"); r=self.rec; self.rec=None; self.auto_mode=False; self.mic_muted=False
        if hasattr(self,"rec_btn"): self.rec_btn.config(text="Обработка…",state="disabled")
        self.refresh_tray()
        source_p=RECORDINGS/(p.stem+"_source.wav"); result=r.stop(p,source_p); self.last_recording=result
        if not result:
            self.processing=False; self.status.set("Запись не сохранена"); self.meetings(); return
        try:
            c=db(); now=datetime.now().isoformat(timespec="seconds"); project=self.project.get() or ""; title=self.titlev.get() or "Новая встреча"
            c.execute("insert or ignore into projects(name,summary,created) values(?,?,?)",(project,"",now))
            c.execute("insert into meetings(title,project,started,audio,transcript,summary,decisions,questions,points,status) values(?,?,?,?,?,?,?,?,?,?)",(title,project,now,str(result),"","","","",json.dumps([],ensure_ascii=False),"transcribing"))
            mid=c.execute("select last_insert_rowid()").fetchone()[0]; self.pending_meeting_id=mid; c.commit(); c.close()
        except Exception as e:
            self.processing=False; messagebox.showerror("Сохранение встречи","Не удалось создать запись встречи в базе данных:\n"+str(e)); return
        self.status.set("Встреча сохранена. Расшифровка выполняется в фоне…")
        try: self.icon.notify("Встреча сохранена","Расшифровка выполняется в фоне")
        except Exception: pass
        threading.Thread(target=self.process_meeting_background,args=(mid,result,source_p),daemon=True).start()

    def process_meeting_background(self,mid,result,source_p):
        tr=""; err=""
        for attempt in range(1,4):
            tr=transcribe_stereo(source_p)
            if tr and not tr.startswith("[Ошибка транскрибации:"): break
            err=tr
            if attempt<3:
                self.after(0,lambda n=attempt:self.status.set(f"Расшифровка: повторная попытка {n+1}/3…")); time.sleep(15*attempt)
        if not tr or tr.startswith("[Ошибка транскрибации:"):
            self.after(0,lambda e=err:self.finish_transcription_failure(mid,e)); return
        try:
            c=db(); c.execute("update meetings set transcript=?,status=? where id=?",(tr,"transcribed",mid)); c.commit(); c.close()
        except Exception: pass
        self.after(0,lambda:self.finish_transcription_success(mid,tr))

    def finish_transcription_failure(self,mid,error):
        self.processing=False; self.pending_meeting_id=None; self.refresh_tray()
        try:
            c=db(); c.execute("update meetings set transcript=?,status=? where id=?",(error or "[Расшифровка не выполнена]","transcription_error",mid)); c.commit(); c.close()
        except Exception: pass
        if hasattr(self,"rec_btn"): self.rec_btn.config(text="● НАЧАТЬ ЗАПИСЬ",state="normal")
        self.status.set("Расшифровка пока не удалась. Встреча сохранена.")
        self.meetings()

    def finish_transcription_success(self,mid,tr):
        data=ai_analyze(tr,self.project.get())
        if data.get("ai_error"):
            self.processing=False; self.pending_meeting_id=mid; self.refresh_tray()
            if hasattr(self,"rec_btn"): self.rec_btn.config(text="● НАЧАТЬ ЗАПИСЬ",state="normal")
            self.status.set("Расшифровка готова. Локальный AI пока недоступен."); self.meetings(); return
        self.review(data,self.last_recording,tr)

    def save_meeting(self,d,path,tr):
        c=db(); project=d.get("project") or self.project.get() or ""; now=datetime.now().isoformat(timespec="seconds"); title=d.get("title") or self.titlev.get()
        try:
            mid=self.pending_meeting_id
            if mid:
                c.execute("update meetings set title=?,project=?,transcript=?,points=?,status=? where id=?",(title,project,tr,json.dumps(d.get("points",[]),ensure_ascii=False),"approved",mid))
                c.execute("delete from tasks where meeting_id=?",(mid,))
            else:
                c.execute("insert or ignore into projects(name,summary,created) values(?,?,?)",(project,"",now))
                c.execute("insert into meetings(title,project,started,audio,transcript,summary,decisions,questions,points,status) values(?,?,?,?,?,?,?,?,?,?)",(title,project,now,str(path),tr,"","","",json.dumps(d.get("points",[]),ensure_ascii=False),"approved"))
                mid=c.execute("select last_insert_rowid()").fetchone()[0]
            c.execute("insert or ignore into projects(name,summary,created) values(?,?,?)",(project,"",now))
            for x in d.get("tasks",[]) or []:
                c.execute("insert into tasks(meeting_id,project,task,owner,deadline,status,created) values(?,?,?,?,?,?,?)",(mid,project,x.get("task",""),x.get("owner",""),x.get("deadline",""),"open",now))
            c.commit(); c.close(); self.pending_meeting_id=None; self.status.set("Встреча сохранена")
            self.meetings()
            try: self.icon.notify("Встреча сохранена",f"{title} • {path.name}")
            except Exception: pass
        except Exception as e:
            try: c.rollback(); c.close()
            except Exception: pass
            messagebox.showerror("Сохранение встречи","Не удалось сохранить изменения:\n"+str(e))
    def meetings(self):
        self.clear(); tk.Label(self.main,text="Встречи",bg="#101218",fg="white",font=("Segoe UI",20,"bold")).pack(anchor="w",padx=30,pady=25)
        c=db(); rows=c.execute("select * from meetings order by id desc limit 50").fetchall(); c.close()
        for m in rows:
            tk.Button(self.main,text=f"{m['started'][:16]}  •  {m['title']}  •  {m['project'] or 'Без проекта'}",command=lambda x=m:self.show_meeting_detail(x),bg="#1b202a",fg="white",anchor="w",padx=15,pady=9,bd=0,activebackground="#252b38",activeforeground="white").pack(fill="x",padx=30,pady=2)
    def show_meeting_detail(self,m):
        self.clear()
        d=MeetingDetail(self.main,m,on_back=self.meetings)
        d.pack(fill="both",expand=True)
    def tasks(self):
        self.clear(); tk.Label(self.main,text="Задачи",bg="#101218",fg="white",font=("Segoe UI",20,"bold")).pack(anchor="w",padx=30,pady=25)
        c=db(); rows=c.execute("select * from tasks order by id desc").fetchall(); c.close()
        for m in rows:
            row=tk.Frame(self.main,bg="#1b202a"); row.pack(fill="x",padx=30,pady=2); tk.Label(row,text=("✓ " if m["status"]=="done" else "○ ")+m["task"],bg="#1b202a",fg="white").pack(side="left",padx=12,pady=9)
            if m["status"]=="open": tk.Button(row,text="Готово",command=lambda i=m["id"]:self.done(i),bg="#1b202a",fg="#45d19a",bd=0).pack(side="right",padx=10)
    def done(self,i):
        c=db(); c.execute("update tasks set status='done' where id=?",(i,)); c.commit(); c.close(); self.tasks()
    def quit_app(self):
        self.destroy_recording_overlay()
        if self.rec: self.stop_recording()
        try: self.icon.stop()
        except Exception: pass
        self.destroy()

if __name__=="__main__": App().mainloop()
