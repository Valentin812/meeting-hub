import os, json, sqlite3, threading, subprocess, wave, tempfile, ctypes
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox
import numpy as np
import soundcard as sc
import soundfile as sf

APP=Path(__file__).resolve().parent
DATA=Path(os.getenv("LOCALAPPDATA",APP))/ "Meeting Hub"
DATA.mkdir(parents=True,exist_ok=True)
RECORDINGS=DATA/"recordings"; RECORDINGS.mkdir(exist_ok=True)
DB=DATA/"meetinghub.db"
WHISPER=os.getenv("MH_WHISPER_MODEL","base")
OLLAMA=os.getenv("MH_OLLAMA_MODEL","llama3.2:3b")

def db():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c

def init():
    c=db()
    c.executescript("""CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY,name TEXT UNIQUE,summary TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS meetings(id INTEGER PRIMARY KEY,title TEXT,project TEXT,started TEXT,audio TEXT,transcript TEXT,summary TEXT,decisions TEXT,questions TEXT,points TEXT,status TEXT);
    CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY,meeting_id INTEGER,project TEXT,task TEXT,owner TEXT,deadline TEXT,status TEXT,created TEXT);""")
    c.commit(); c.close()
init()

def clean(s): return " ".join(str(s or "").split())

def transcribe_file(path):
    try:
        from faster_whisper import WhisperModel
        model=WhisperModel(WHISPER,device="cpu",compute_type="int8")
        segs,_=model.transcribe(str(path),beam_size=1,vad_filter=True)
        return " ".join(clean(x.text) for x in segs if clean(x.text))
    except Exception as e: return "[Ошибка транскрибации: %s]"%e

def transcribe_stereo(path):
    try:
        with wave.open(str(path),"rb") as w:
            ch=w.getnchannels(); rate=w.getframerate(); raw=w.readframes(w.getnframes())
        if ch<2: return transcribe_file(path)
        a=np.frombuffer(raw,np.int16).reshape(-1,ch)
        from faster_whisper import WhisperModel
        model=WhisperModel(WHISPER,device="cpu",compute_type="int8")
        out=[]
        with tempfile.TemporaryDirectory() as td:
            for i,label in ((0,"Вы"),(1,"Собеседники")):
                p=Path(td)/f"c{i}.wav"
                with wave.open(str(p),"wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(a[:,i].tobytes())
                segs,_=model.transcribe(str(p),beam_size=1,vad_filter=True)
                for s in segs:
                    t=clean(s.text)
                    if t: out.append((float(s.start),label,t))
        out.sort()
        return "\n".join(f"[{int(x//60):02d}:{x%60:04.1f}] {sp}: {t}" for x,sp,t in out)
    except Exception as e: return "[Ошибка транскрибации: %s]"%e

def find_ollama():
    candidates=[
        os.getenv("OLLAMA_EXE",""),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Ollama\ollama.exe"),
        r"C:\Program Files\Ollama\ollama.exe"]
    for p in candidates:
        if p and Path(p).exists(): return p
    try:
        r=subprocess.run(["where","ollama"],capture_output=True,text=True,timeout=5)
        if r.returncode==0 and r.stdout.strip(): return r.stdout.splitlines()[0].strip()
    except Exception: pass
    return None

def ai_analyze(transcript,project=""):
    prompt=f"""Ты локальный ассистент Meeting Hub. Проанализируй НОВУЮ встречу.
Проект: {project or "не указан"}
Не выдумывай людей, сроки и факты. Не копируй старые задачи.
Верни ТОЛЬКО JSON:
{{"title":"","project":"","summary":"","project_summary":"","points":[],"decisions":[],"questions":[],"tasks":[{{"task":"","owner":"","deadline":""}}]}}
РАСШИФРОВКА:
{transcript}"""
    exe=find_ollama()
    if not exe:
        return {"title":"","project":project,"summary":"Локальный AI не найден. Установите Ollama и модель "+OLLAMA+".","project_summary":"","points":[],"decisions":[],"questions":[],"tasks":[],"ai_error":True}
    try:
        r=subprocess.run([exe,"run",OLLAMA,prompt],capture_output=True,text=True,timeout=300)
        s=r.stdout.strip(); a=s.find("{"); b=s.rfind("}")
        if a>=0 and b>a: return json.loads(s[a:b+1])
        if r.returncode!=0: raise RuntimeError(r.stderr.strip() or "Ollama завершил работу с ошибкой")
        return {"summary":s[:5000]}
    except Exception as e:
        return {"title":"","project":project,"summary":"Ошибка локального AI: "+str(e),"project_summary":"","points":[],"decisions":[],"questions":[],"tasks":[],"ai_error":True}

class Recorder:
    def __init__(self,mic=None,speaker=None):
        self.mic=mic; self.speaker=speaker; self.rate=48000; self.run=False; self.m=[]; self.s=[]
    def start(self):
        self.run=True
        mic=sc.get_microphone(self.mic,include_loopback=False) if self.mic else sc.default_microphone()
        sp=sc.get_speaker(self.speaker) if self.speaker else sc.default_speaker()
        loop=sc.get_microphone(id=str(sp.id),include_loopback=True)
        def rec(dev,out):
            try:
                with dev.recorder(samplerate=self.rate,channels=1) as r:
                    while self.run: out.append(r.record(int(self.rate*.25)))
            except Exception: pass
        threading.Thread(target=rec,args=(mic,self.m),daemon=True).start()
        threading.Thread(target=rec,args=(loop,self.s),daemon=True).start()
    def stop(self,path):
        self.run=False
        import time; time.sleep(.5)
        if not self.m or not self.s: return None
        m=np.concatenate(self.m); s=np.concatenate(self.s); n=min(len(m),len(s))
        sf.write(str(path),np.column_stack((m[:n,0],s[:n,0])),self.rate,subtype="PCM_16")
        return path

MEETING_PROCS=("zoom.exe","ms-teams.exe","teams.exe","webex.exe","webexmta.exe","skype.exe")
TITLE_KEYS=("zoom meeting","microsoft teams","google meet","webex","meeting","встреч","созвон","звонок","конферен","conference")

def windows_meeting_title():
    found=[]
    user32=ctypes.windll.user32
    EnumProc=ctypes.WINFUNCTYPE(ctypes.c_bool,ctypes.c_void_p,ctypes.c_void_p)
    def cb(hwnd,lparam):
        if not user32.IsWindowVisible(hwnd): return True
        buf=ctypes.create_unicode_buffer(512); user32.GetWindowTextW(hwnd,buf,512)
        title=buf.value.strip()
        if not title: return True
        pid=ctypes.c_ulong(); user32.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
        process=""
        h=user32.OpenProcess(0x1000|0x0010,False,pid.value)
        if h:
            pbuf=ctypes.create_unicode_buffer(512); size=ctypes.c_ulong(512)
            if user32.QueryFullProcessImageNameW(h,0,pbuf,ctypes.byref(size)): process=Path(pbuf.value).name.lower()
            user32.CloseHandle(h)
        low=title.lower()
        if process in MEETING_PROCS or any(k in low for k in TITLE_KEYS):
            found.append((title,process))
        return True
    user32.EnumWindows(EnumProc(cb),0)
    return found[0] if found else None

class Review(tk.Toplevel):
    def __init__(self,parent,data,save):
        super().__init__(parent); self.title("Проверка AI-разбора"); self.geometry("850x700"); self.grab_set()
        self.data=data; self.save=save
        f=ttk.Frame(self,padding=15); f.pack(fill="both",expand=True)
        self.vars={}
        for k,label in [("project","Проект"),("summary","Итог"),("project_summary","Состояние проекта")]:
            ttk.Label(f,text=label).pack(anchor="w"); v=tk.StringVar(value=data.get(k,"")); ttk.Entry(f,textvariable=v).pack(fill="x",pady=(0,8)); self.vars[k]=v
        for k,label in [("points","Ключевые пункты"),("decisions","Решения"),("questions","Вопросы")]:
            ttk.Label(f,text=label).pack(anchor="w"); t=tk.Text(f,height=4); t.pack(fill="x",pady=(0,8)); t.insert("1.0","\n".join(data.get(k,[]) or [])); self.vars[k]=t
        ttk.Label(f,text="Задачи: задача | ответственный | срок").pack(anchor="w")
        self.tasks=tk.Text(f,height=7); self.tasks.pack(fill="both",expand=True)
        for x in data.get("tasks",[]) or []: self.tasks.insert("end",f"{x.get('task','')} | {x.get('owner','')} | {x.get('deadline','')}\n")
        if data.get("ai_error"):
            ttk.Label(f,text="⚠ "+data.get("summary",""),foreground="#c65").pack(anchor="w",pady=5)
        ttk.Button(f,text="✓ Подтвердить и сохранить",command=self.apply).pack(anchor="e",pady=10)
    def apply(self):
        d=dict(self.data); d["project"]=self.vars["project"].get(); d["summary"]=self.vars["summary"].get(); d["project_summary"]=self.vars["project_summary"].get()
        for k in ("points","decisions","questions"): d[k]=[clean(x) for x in self.vars[k].get("1.0","end").splitlines() if clean(x)]
        d["tasks"]=[]
        for line in self.tasks.get("1.0","end").splitlines():
            p=[x.strip() for x in line.split("|")]
            if p and p[0]: d["tasks"].append({"task":p[0],"owner":p[1] if len(p)>1 else "","deadline":p[2] if len(p)>2 else ""})
        self.save(d); self.destroy()

class MeetingDetail(tk.Toplevel):
    def __init__(self,parent,m):
        super().__init__(parent); self.title("Встреча — "+(m["title"] or "Без названия")); self.geometry("900x750")
        f=ttk.Frame(self,padding=18); f.pack(fill="both",expand=True)
        ttk.Label(f,text=m["title"] or "Без названия",font=("Segoe UI",18,"bold")).pack(anchor="w")
        ttk.Label(f,text=f"{m['started'][:16]}   •   {m['project'] or 'Без проекта'}").pack(anchor="w",pady=(2,12))
        for label,key in [("Итог","summary"),("Ключевые пункты","points"),("Решения","decisions"),("Вопросы","questions")]:
            ttk.Label(f,text=label,font=("Segoe UI",10,"bold")).pack(anchor="w")
            t=tk.Text(f,height=4); t.pack(fill="x",pady=(2,8)); val=m[key] or ""
            if key in ("points","decisions","questions"):
                try: val="\n".join(json.loads(val))
                except Exception: pass
            t.insert("1.0",val); t.config(state="disabled")
        ttk.Label(f,text="Расшифровка",font=("Segoe UI",10,"bold")).pack(anchor="w")
        tr=tk.Text(f); tr.pack(fill="both",expand=True); tr.insert("1.0",m["transcript"] or ""); tr.config(state="disabled")
        bar=ttk.Frame(f); bar.pack(fill="x",pady=(8,0))
        ttk.Button(bar,text="▶ Открыть запись",command=lambda:self.play(m["audio"])).pack(side="left")
    def play(self,path):
        if not path or not Path(path).exists(): messagebox.showerror("Запись","Файл записи не найден"); return
        try: os.startfile(path)
        except Exception as e: messagebox.showerror("Запись",str(e))

class AutoDetector:
    def __init__(self,app): self.app=app; self.run=True; self.last=None; threading.Thread(target=self.loop,daemon=True).start()
    def loop(self):
        import time
        while self.run:
            try:
                hit=windows_meeting_title()
                if hit and not self.app.rec:
                    self.app.auto_start(hit[0])
                elif not hit and self.app.rec and self.app.auto_mode:
                    self.app.auto_stop()
            except Exception: pass
            time.sleep(2)

class App(tk.Tk):
    def __init__(self):
        super().__init__(); self.title("Meeting Hub"); self.geometry("1050x700"); self.configure(bg="#101218")
        self.rec=None; self.started=None; self.auto_mode=False
        self.project=tk.StringVar(); self.titlev=tk.StringVar(value="Новая встреча"); self.status=tk.StringVar(value="Готов")
        self.build(); self.detector=AutoDetector(self)
    def build(self):
        left=tk.Frame(self,bg="#151820",width=190); left.pack(side="left",fill="y"); left.pack_propagate(False)
        tk.Label(left,text="MEETING",bg="#151820",fg="white",font=("Segoe UI",11,"bold")).pack(pady=(25,0))
        tk.Label(left,text="HUB",bg="#151820",fg="#7c8cff",font=("Segoe UI",20,"bold")).pack(pady=(0,25))
        for name,cmd in [("Главная",self.home),("Новая встреча",self.new),("Встречи",self.meetings),("Задачи",self.tasks)]:
            tk.Button(left,text=name,command=cmd,anchor="w",bg="#151820",fg="#b0b7c5",bd=0,padx=20,pady=10).pack(fill="x")
    def clear(self):
        for x in self.main.winfo_children(): x.destroy()
    def home(self):
        if not hasattr(self,"main"):
            self.main=tk.Frame(self,bg="#101218"); self.main.pack(fill="both",expand=True)
        self.clear(); tk.Label(self.main,text="Meeting Hub",bg="#101218",fg="white",font=("Segoe UI",24,"bold")).pack(anchor="w",padx=30,pady=30)
        c=db(); n=c.execute("select count(*) n from meetings").fetchone()["n"]; t=c.execute("select count(*) n from tasks where status='open'").fetchone()["n"]; c.close()
        tk.Label(self.main,text=f"Встреч: {n}    Открытых задач: {t}",bg="#101218",fg="#9ba3b3",font=("Segoe UI",12)).pack(anchor="w",padx=30)
        tk.Label(self.main,text="● Автоподключение к встречам: включено",bg="#101218",fg="#45d19a").pack(anchor="w",padx=30,pady=(18,0))
        ttk.Button(self.main,text="＋ Новая встреча",command=self.new).pack(anchor="w",padx=30,pady=25)
    def new(self):
        if not hasattr(self,"main"): self.main=tk.Frame(self,bg="#101218"); self.main.pack(fill="both",expand=True)
        self.clear(); f=tk.Frame(self.main,bg="#101218"); f.pack(fill="both",expand=True,padx=30,pady=25)
        tk.Label(f,text="Новая встреча",bg="#101218",fg="white",font=("Segoe UI",20,"bold")).pack(anchor="w")
        for lab,var in [("Название",self.titlev),("Проект",self.project)]:
            tk.Label(f,text=lab,bg="#101218",fg="#aab2c2").pack(anchor="w",pady=(18,4)); tk.Entry(f,textvariable=var,bg="#1b202a",fg="white",insertbackground="white",bd=0).pack(fill="x",ipady=9)
        tk.Label(f,text="Meeting Hub сам начнёт запись при обнаружении Zoom / Teams / Meet / Webex.",bg="#101218",fg="#9ba3b3").pack(anchor="w",pady=20)
        tk.Label(f,textvariable=self.status,bg="#101218",fg="#45d19a").pack(anchor="w")
        self.rec_btn=tk.Button(f,text="● НАЧАТЬ ЗАПИСЬ",command=self.toggle,bg="#7c8cff",fg="white",bd=0,padx=20,pady=12); self.rec_btn.pack(anchor="w",pady=12)
    def start_recording(self,title=None,auto=False):
        if self.rec: return
        try:
            self.rec=Recorder(); self.rec.start(); self.started=datetime.now(); self.auto_mode=auto
            if title: self.titlev.set(title[:120])
            self.status.set("Идёт запись…")
            if hasattr(self,"rec_btn"): self.rec_btn.config(text="■ ОСТАНОВИТЬ")
        except Exception as e: self.rec=None; messagebox.showerror("Аудио",str(e))
    def auto_start(self,title):
        self.after(0,lambda:self.start_recording(title,True))
    def toggle(self):
        if not self.rec: self.start_recording()
        else: self.stop_recording()
    def auto_stop(self): self.after(0,self.stop_recording)
    def stop_recording(self):
        if not self.rec: return
        p=RECORDINGS/(datetime.now().strftime("%Y%m%d_%H%M%S")+".wav"); r=self.rec; self.rec=None; self.auto_mode=False
        if hasattr(self,"rec_btn"): self.rec_btn.config(text="Обработка…",state="disabled")
        r.stop(p)
        def work():
            tr=transcribe_stereo(p); data=ai_analyze(tr,self.project.get())
            self.after(0,lambda:self.review(data,p,tr))
        threading.Thread(target=work,daemon=True).start()
    def review(self,data,path,tr):
        if hasattr(self,"rec_btn"): self.rec_btn.config(text="● НАЧАТЬ ЗАПИСЬ",state="normal")
        Review(self,data,lambda d:self.save_meeting(d,path,tr))
    def save_meeting(self,d,path,tr):
        c=db(); project=d.get("project") or self.project.get() or ""; now=datetime.now().isoformat(timespec="seconds")
        c.execute("insert or ignore into projects(name,summary,created) values(?,?,?)",(project,d.get("project_summary",""),now))
        c.execute("insert into meetings(title,project,started,audio,transcript,summary,decisions,questions,points,status) values(?,?,?,?,?,?,?,?,?,?)",(d.get("title") or self.titlev.get(),project,now,str(path),tr,d.get("summary",""),json.dumps(d.get("decisions",[]),ensure_ascii=False),json.dumps(d.get("questions",[]),ensure_ascii=False),json.dumps(d.get("points",[]),ensure_ascii=False),"approved"))
        mid=c.execute("select last_insert_rowid()").fetchone()[0]
        for x in d.get("tasks",[]) or []: c.execute("insert into tasks(meeting_id,project,task,owner,deadline,status,created) values(?,?,?,?,?,?,?)",(mid,project,x.get("task",""),x.get("owner",""),x.get("deadline",""),"open",now))
        c.commit(); c.close(); self.home()
    def meetings(self):
        if not hasattr(self,"main"): self.main=tk.Frame(self,bg="#101218"); self.main.pack(fill="both",expand=True)
        self.clear(); tk.Label(self.main,text="Встречи",bg="#101218",fg="white",font=("Segoe UI",20,"bold")).pack(anchor="w",padx=30,pady=25)
        c=db(); rows=c.execute("select * from meetings order by id desc limit 50").fetchall(); c.close()
        for m in rows:
            b=tk.Button(self.main,text=f"{m['started'][:16]}  •  {m['title']}  •  {m['project'] or 'Без проекта'}",command=lambda x=m:MeetingDetail(self,x),bg="#1b202a",fg="white",anchor="w",padx=15,pady=9,bd=0,activebackground="#252b38",activeforeground="white")
            b.pack(fill="x",padx=30,pady=2)
    def tasks(self):
        if not hasattr(self,"main"): self.main=tk.Frame(self,bg="#101218"); self.main.pack(fill="both",expand=True)
        self.clear(); tk.Label(self.main,text="Задачи",bg="#101218",fg="white",font=("Segoe UI",20,"bold")).pack(anchor="w",padx=30,pady=25)
        c=db(); rows=c.execute("select * from tasks order by id desc").fetchall(); c.close()
        for m in rows:
            row=tk.Frame(self.main,bg="#1b202a"); row.pack(fill="x",padx=30,pady=2)
            tk.Label(row,text=("✓ " if m["status"]=="done" else "○ ")+m["task"],bg="#1b202a",fg="white").pack(side="left",padx=12,pady=9)
            if m["status"]=="open": tk.Button(row,text="Готово",command=lambda i=m["id"]:self.done(i),bg="#1b202a",fg="#45d19a",bd=0).pack(side="right",padx=10)
    def done(self,i):
        c=db(); c.execute("update tasks set status='done' where id=?",(i,)); c.commit(); c.close(); self.tasks()

if __name__=="__main__":
    App().mainloop()
