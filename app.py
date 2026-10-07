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
VERSION="1.1.17"
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
                for s in segs:
                    t=clean(s.text)
                    if t: out.append((float(s.start),label,t))
        out.sort(); return "\n".join(f"[{int(x//60):02d}:{x%60:04.1f}] {sp}: {t}" for x,sp,t in out)
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
        r=subprocess.run([exe,"run",OLLAMA,prompt,"--format","json"],capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=300)
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
        super().__init__(parent,bg="#101218"); self.m=m; self.on_back=on_back
        f=ttk.Frame(self,padding=18); f.pack(fill="both",expand=True)
        top=ttk.Frame(f); top.pack(fill="x")
        ttk.Button(top,text="← К встречам",command=self.go_back).pack(side="left")
        ttk.Button(top,text="✏ Редактировать название",command=self.edit_title).pack(side="left",padx=8)
        ttk.Button(top,text="🗑 Удалить встречу",command=self.delete_meeting).pack(side="right")
        self.header=ttk.Label(f,text=m["title"] or "Без названия",font=("Segoe UI",18,"bold")); self.header.pack(anchor="w",pady=(10,0))
        self.meta=ttk.Label(f,text=f"{m['started'][:16]}   •   {m['project'] or 'Без проекта'}"); self.meta.pack(anchor="w",pady=(2,12))
        ttk.Label(f,text="Статус: "+(m["status"] or "")).pack(anchor="w",pady=(0,8))
        ttk.Label(f,text="Тезисы",font=("Segoe UI",10,"bold")).pack(anchor="w")
        t=tk.Text(f,height=7); t.pack(fill="x",pady=(2,10)); val=m["points"] or ""
        try: val="\n".join(json.loads(val))
        except Exception: pass
        t.insert("1.0",val); t.config(state="disabled")
        ttk.Label(f,text="Задачи",font=("Segoe UI",10,"bold")).pack(anchor="w")
        q=tk.Text(f,height=6); q.pack(fill="x",pady=(2,10)); c=db(); tasks=c.execute("select * from tasks where meeting_id=? order by id",(m["id"],)).fetchall(); c.close()
        q.insert("1.0","\n".join(f"{x['task']} | {x['owner'] or ''} | {x['deadline'] or ''}" for x in tasks)); q.config(state="disabled")
        ttk.Label(f,text="Расшифровка",font=("Segoe UI",10,"bold")).pack(anchor="w")
        tr=tk.Text(f); tr.pack(fill="both",expand=True); tr.insert("1.0",m["transcript"] or ""); tr.config(state="disabled")
        row=ttk.Frame(f); row.pack(fill="x",pady=(8,0))
        ttk.Button(row,text="▶ Открыть запись",command=lambda:self.play(m["audio"])).pack(side="left")
        ttk.Button(row,text="▶ Открыть исходник",command=lambda:self.play(self.source_path(m["audio"]))).pack(side="left",padx=8)
        ttk.Button(row,text="📁 Папка записей",command=open_recordings_folder).pack(side="left",padx=8)
        src=self.source_path(m["audio"]); ttk.Label(f,text="Запись: "+(m["audio"] or "не найден")+"\nИсходник: "+src,wraplength=820).pack(anchor="w",pady=(8,0))
    def go_back(self):
        if self.on_back: self.on_back()
    def edit_title(self):
        w=tk.Toplevel(self); w.title("Название встречи"); w.geometry("430x130"); w.transient(self.winfo_toplevel()); w.grab_set()
        ttk.Label(w,text="Название:").pack(anchor="w",padx=15,pady=(15,4))
        v=tk.StringVar(value=self.m["title"] or ""); e=ttk.Entry(w,textvariable=v); e.pack(fill="x",padx=15); e.focus_set()
        def save():
            title=v.get().strip() or "Без названия"
            c=db(); c.execute("update meetings set title=? where id=?",(title,self.m["id"])); c.commit(); c.close()
            self.m["title"]=title; self.header.config(text=title); w.destroy()
        ttk.Button(w,text="Сохранить",command=save).pack(anchor="e",padx=15,pady=12)
    def delete_meeting(self):
        if not messagebox.askyesno("Удалить встречу","Удалить встречу, задачи и связанные аудиозаписи? Это действие нельзя отменить."): return
        paths=[]
        if self.m["audio"]:
            p=Path(self.m["audio"]); paths.extend([p,p.with_name(p.stem+"_source.wav")])
        c=db(); c.execute("delete from tasks where meeting_id=?",(self.m["id"],)); c.execute("delete from meetings where id=?",(self.m["id"],)); c.commit(); c.close()
        for p in paths:
            try:
                if p.exists(): p.unlink()
            except Exception: pass
        self.go_back()
    def source_path(self,path):
        if not path: return ""
        p=Path(path); return str(p.with_name(p.stem+"_source.wav"))
    def play(self,path):
        if not path or not Path(path).exists(): messagebox.showerror("Запись","Файл записи не найден"); return
        try: os.startfile(path)
        except Exception as e: messagebox.showerror("Запись",str(e))


