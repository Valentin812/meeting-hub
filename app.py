import os, json, sqlite3, threading, subprocess, wave, tempfile, ctypes, time, shutil
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import numpy as np
import soundcard as sc
import soundfile as sf
import pystray
from PIL import Image, ImageDraw

APP=Path(__file__).resolve().parent
VERSION="1.1.33"
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
    # Внутренние записи Meeting Hub: WAV с L=микрофон, R=системный звук.
    try:
        with wave.open(str(path),"rb") as w:
            ch=w.getnchannels(); rate=w.getframerate(); raw=w.readframes(w.getnframes())
        if ch>=2:
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
                    if not got:
                        segs,_=model.transcribe(str(p),beam_size=1,vad_filter=False,language="ru")
                        for s in segs:
                            t=clean(s.text)
                            if t: out.append((float(s.start),label,t))
            out.sort()
            text="\n".join(f"[{int(x//60):02d}:{x%60:04.1f}] {sp}: {t}" for x,sp,t in out)
            if text: return text
            return transcribe_file(path)
    except Exception:
        pass

    # Внешние MP3/M4A/FLAC/OGG и другие форматы: faster-whisper умеет декодировать их сам.
    # Если файл действительно стерео и каналы отличаются, сохраняем разделение каналов.
    try:
        from faster_whisper import WhisperModel
        from faster_whisper.audio import decode_audio
        left,right=decode_audio(str(path),sampling_rate=16000,split_stereo=True)
        if len(left) and len(right):
            lm=float(np.sqrt(np.mean(left*left))); rm=float(np.sqrt(np.mean(right*right)))
            corr=1.0
            if lm>1e-5 and rm>1e-5:
                corr=float(np.corrcoef(left,right)[0,1])
            if abs(corr)<0.995 or abs(lm-rm)>0.01:
                model=WhisperModel(WHISPER,device="cpu",compute_type="int8"); out=[]
                for audio,label in ((left,"Вы"),(right,"Собеседники")):
                    segs,_=model.transcribe(audio,beam_size=1,vad_filter=True,language="ru")
                    for s in segs:
                        t=clean(s.text)
                        if t: out.append((float(s.start),label,t))
                out.sort()
                text="\n".join(f"[{int(x//60):02d}:{x%60:04.1f}] {sp}: {t}" for x,sp,t in out)
                if text: return text
    except Exception:
        pass

    # Моно/смешанный внешний файл: таймкоды сохраняем, но спикера честно не угадываем.
    return transcribe_file(path)

def transcribe_file(path):
    try:
        from faster_whisper import WhisperModel
        model=WhisperModel(WHISPER,device="cpu",compute_type="int8")
        segs,_=model.transcribe(str(path),beam_size=1,vad_filter=True,language="ru")
        out=[]
        for s in segs:
            t=clean(s.text)
            if t:
                out.append(f"[{int(float(s.start)//60):02d}:{float(s.start)%60:04.1f}] Собеседники: {t}")
        return "\n".join(out)
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
3) Короткое понятное название встречи по содержанию, 3-8 слов. Не используй дату, "Google Chrome", "Новая встреча" и другие технические названия.
4) Проект: используй указанный проект, если он задан. Если проект не задан или его нельзя уверенно определить по тексту — оставь пустым. Не выдумывай проект.
Не выдумывай задачи, людей или сроки. Если ответственный/срок не названы — оставь поле пустым.
Верни ТОЛЬКО JSON:
{{"title":"","project":"{project or ""}","points":[],"tasks":[{{"task":"","owner":"","deadline":""}}]}}
РАСШИФРОВКА:
{transcript[:30000]}"""
    exe=find_ollama()
    if not exe:
        return {"title":"","project":project,"points":[],"tasks":[],"ai_error":True,"error":"Локальный AI не найден."}
    si=None; flags=0
    if os.name=="nt":
        si=subprocess.STARTUPINFO(); si.dwFlags |= subprocess.STARTF_USESHOWWINDOW; si.wShowWindow=0
        flags=subprocess.CREATE_NO_WINDOW

    def run_ai(text_prompt):
        return subprocess.run(
            [exe,"run",OLLAMA,"--format","json"],
            input=text_prompt,capture_output=True,text=True,encoding="utf-8",errors="replace",
            timeout=300,startupinfo=si,creationflags=flags
        )

    def parse_json(raw):
        raw=(raw or "").strip(); a=raw.find("{"); b=raw.rfind("}")
        if a<0 or b<=a: return None
        raw=raw[a:b+1]
        fixed=[]; quoted=False; escaped=False
        for ch in raw:
            if escaped: fixed.append(ch); escaped=False; continue
            if ch=="\\": fixed.append(ch); escaped=True; continue
            if ch=='"': fixed.append(ch); quoted=not quoted; continue
            if quoted and ord(ch)<32: fixed.append("\\n" if ch=="\n" else ("\\t" if ch=="\t" else " "))
            else: fixed.append(ch)
        d=json.loads("".join(fixed))
        if not isinstance(d,dict): return None
        points=d.get("points",[])
        tasks=d.get("tasks",[])
        if not isinstance(points,list): points=[str(points)]
        if not isinstance(tasks,list): tasks=[]
        return {"title":d.get("title",""),"project":d.get("project",project),"points":[str(x) for x in points if clean(x)],"tasks":tasks}

    try:
        r=run_ai(prompt)
        d=parse_json(r.stdout)
        if d and d.get("points"): return d
        retry=f"""Верни только корректный JSON, без markdown и пояснений.
Формат: {{"title":"","project":"","points":["тезис"],"tasks":[{{"task":"","owner":"","deadline":""}}]}}
Сделай 3-7 коротких тезисов. Задачи — только явно поставленные на встрече. Если задач нет, tasks=[].
Текст встречи:
{transcript[:20000]}"""
        r2=run_ai(retry); d=parse_json(r2.stdout)
        if d and d.get("points"): return d
        raise RuntimeError((r2.stderr or r.stderr or "Ollama не вернул JSON или не сформировал тезисы").strip())
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

class AudioPlayer(tk.Frame):
    def __init__(self,parent,path):
        super().__init__(parent)
        self.path=str(path or ""); self.alias="MeetingHubAudio"; self.playing=False; self.duration=0; self.position=0
        self.scale=ttk.Scale(self,from_=0,to=100,orient="horizontal",command=self.seek)
        self.scale.pack(side="left",fill="x",expand=True,padx=(0,8))
        self.time_label=ttk.Label(self,text="00:00 / 00:00",width=13); self.time_label.pack(side="left",padx=(0,8))
        ttk.Button(self,text="▶",width=4,command=self.play).pack(side="left",padx=2)
        ttk.Button(self,text="Ⅱ",width=4,command=self.pause).pack(side="left",padx=2)
        ttk.Button(self,text="■",width=4,command=self.stop).pack(side="left",padx=2)
        self.after(250,self.update)
    def cmd(self,cmd,buf=None,size=0):
        return ctypes.windll.winmm.mciSendStringW(cmd,buf,size,None)
    def open_audio(self):
        if not Path(self.path).exists():
            messagebox.showerror("Запись","Файл записи не найден."); return False
        self.cmd(f"close {self.alias}")
        p=self.path.replace('"','')
        if self.cmd(f'open "{p}" type waveaudio alias {self.alias}')!=0:
            messagebox.showerror("Запись","Не удалось открыть запись."); return False
        buf=ctypes.create_unicode_buffer(64); self.cmd(f"status {self.alias} length",buf,64)
        try: self.duration=int(buf.value or 0)
        except: self.duration=0
        self.scale.configure(to=max(1,self.duration)); return True
    def play(self):
        if self.position<=0 and not self.playing:
            if not self.open_audio(): return
        self.cmd(f"play {self.alias} from {int(self.position)}"); self.playing=True
    def pause(self):
        if self.playing: self.cmd(f"pause {self.alias}"); self.playing=False
    def stop(self):
        self.cmd(f"stop {self.alias}"); self.position=0; self.playing=False; self.scale.set(0)
    def seek(self,value):
        try:
            self.position=int(float(value))
            if self.playing: self.cmd(f"play {self.alias} from {self.position}")
        except: pass
    def update(self):
        try:
            if self.playing:
                buf=ctypes.create_unicode_buffer(64); self.cmd(f"status {self.alias} position",buf,64)
                self.position=int(buf.value or 0)
                if self.duration and self.position>=self.duration: self.stop()
            self.scale.set(self.position)
            self.time_label.config(text=f"{self.position//60000:02d}:{(self.position//1000)%60:02d} / {self.duration//60000:02d}:{(self.duration//1000)%60:02d}")
        except: pass
        try: self.after(250,self.update)
        except: pass
    def destroy(self):
        try: self.cmd(f"stop {self.alias}"); self.cmd(f"close {self.alias}")
        except: pass
        super().destroy()


class MeetingDetail(tk.Frame):
    def __init__(self,parent,m,on_back=None):
        super().__init__(parent,bg="#101218")
        self.on_back=on_back
        self.app=parent.winfo_toplevel()
        self.meeting_id=m["id"]
        self.audio_path=m["audio"] or ""

        # Верхняя шапка и нижняя панель действий фиксированы.
        f=ttk.Frame(self,padding=18); f.pack(fill="both",expand=True)
        header=ttk.Frame(f); header.pack(fill="x",side="top")
        ttk.Button(header,text="← К встречам",command=self.go_back).pack(anchor="w",pady=(0,8))
        title_row=ttk.Frame(header); title_row.pack(fill="x",anchor="w")
        self.title_label=ttk.Label(title_row,text=m["title"] or "Без названия",font=("Segoe UI",18,"bold")); self.title_label.pack(side="left")
        ttk.Button(title_row,text="✎ Изменить название",command=self.edit_title).pack(side="left",padx=(12,0))
        ttk.Label(header,text=f"{m['started'][:16]}   •   {m['project'] or 'Без проекта'}").pack(anchor="w",pady=(2,10))

        footer=ttk.Frame(f); footer.pack(fill="x",side="bottom",pady=(8,0))
        ttk.Separator(f,orient="horizontal").pack(fill="x",side="bottom",before=footer)
        ttk.Label(footer,text="Запись",font=("Segoe UI",10,"bold")).pack(anchor="w",pady=(7,2))
        player=ttk.Frame(footer); player.pack(fill="x",pady=(0,7))
        self.audio_player=AudioPlayer(player,self.audio_path); self.audio_player.pack(fill="x")
        actions=ttk.Frame(footer); actions.pack(fill="x")
        ttk.Button(actions,text="📁 Открыть папку",command=lambda:open_recordings_folder()).pack(side="left")
        self.retry_btn=ttk.Button(actions,text="↻ Повторить транскрибацию",command=self.retry_transcription)
        if m["status"] in ("transcribing","transcription_error") or not (m["transcript"] or "").strip():
            self.retry_btn.pack(side="left",padx=(8,0))
        self.retry_ai_btn=ttk.Button(actions,text="✨ Сформировать тезисы и задачи",command=self.retry_ai_analysis)
        if (m["transcript"] or "").strip() and not (m["points"] or "").strip():
            self.retry_ai_btn.pack(side="left",padx=(8,0))
        ttk.Button(actions,text="🗑 Удалить встречу",command=lambda:self.delete_meeting(m["id"])).pack(side="right")

        # Скроллится только содержимое встречи, а не вся страница.
        content=ttk.Frame(f)
        content.pack(fill="both",expand=True,side="top")
        canvas=tk.Canvas(content,bg="#f0f0f0",highlightthickness=0,bd=0)
        scrollbar=ttk.Scrollbar(content,orient="vertical",command=canvas.yview)
        inner=ttk.Frame(canvas)
        inner_window=canvas.create_window((0,0),window=inner,anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left",fill="both",expand=True)
        scrollbar.pack(side="right",fill="y")
        def on_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(inner_window,width=canvas.winfo_width())
        inner.bind("<Configure>",on_configure)
        canvas.bind("<Configure>",lambda e:canvas.itemconfigure(inner_window,width=e.width))

        ttk.Label(inner,text="Тезисы",font=("Segoe UI",10,"bold")).pack(anchor="w",pady=(0,2))
        t=tk.Text(inner,height=6,wrap="word")
        t.pack(fill="x",pady=(0,10))
        val=m["points"] or ""
        try: val="\n".join(json.loads(val))
        except Exception: pass
        t.insert("1.0",val); t.config(state="disabled")

        ttk.Label(inner,text="Задачи",font=("Segoe UI",10,"bold")).pack(anchor="w",pady=(0,2))
        q=tk.Text(inner,height=5,wrap="word")
        q.pack(fill="x",pady=(0,10))
        c=db(); tasks=c.execute("select * from tasks where meeting_id=? order by id",(m["id"],)).fetchall(); c.close()
        q.insert("1.0","\n".join(f"{x['task']} | {x['owner'] or ''} | {x['deadline'] or ''}" for x in tasks))
        q.config(state="disabled")

        ttk.Label(inner,text="Расшифровка",font=("Segoe UI",10,"bold")).pack(anchor="w",pady=(0,2))
        tr=tk.Text(inner,height=18,wrap="word")
        tr.pack(fill="both",expand=True)
        tr.insert("1.0",m["transcript"] or "")
        tr.config(state="disabled")

        self.transcribe_action=ttk.Label(inner,text="",foreground="#666")
        self.transcribe_action.pack(anchor="w",pady=(5,2))
        self.transcribe_progress=ttk.Progressbar(inner,mode="indeterminate")
        if m["status"]=="transcribing":
            self.show_transcription_progress()
        elif m["status"]=="transcription_error":
            self.transcribe_action.config(text="⚠ Транскрибация не завершилась. Можно запустить повторно.")
        elif (m["transcript"] or "").strip() and not (m["points"] or "").strip():
            self.after(300,self.auto_ai_if_needed)

    def show_transcription_progress(self):
        self.transcribe_action.config(text="⏳ Идёт транскрибация. Можно продолжать пользоваться программой.")
        if not self.transcribe_progress.winfo_ismapped():
            self.transcribe_progress.pack(anchor="w",fill="x",pady=(0,6))
        self.transcribe_progress.start(12)
        if hasattr(self,"retry_btn"):
            self.retry_btn.config(text="⏳ Идёт транскрибация…",state="disabled")

    def edit_title(self):
        c=db(); row=c.execute("select title from meetings where id=?",(self.meeting_id,)).fetchone(); c.close()
        current=(row["title"] if row else "") or "Без названия"
        w=tk.Toplevel(self); w.title("Название встречи"); w.geometry("520x150"); w.transient(self.winfo_toplevel()); w.grab_set()
        ttk.Label(w,text="Название встречи").pack(anchor="w",padx=15,pady=(15,5))
        v=tk.StringVar(value=current)
        e=ttk.Entry(w,textvariable=v); e.pack(fill="x",padx=15); e.focus_set(); e.select_range(0,"end")
        def save():
            title=v.get().strip() or "Без названия"
            c=db(); c.execute("update meetings set title=? where id=?",(title,self.meeting_id)); c.commit(); c.close()
            self.title_label.config(text=title)
            w.destroy()
        ttk.Button(w,text="Сохранить",command=save).pack(anchor="e",padx=15,pady=12)
        e.bind("<Return>",lambda e:save()); w.bind("<Escape>",lambda e:w.destroy())

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
        if not messagebox.askyesno("Удалить встречу","Удалить встречу, расшифровку, задачи и все связанные аудиофайлы?\n\nЭто действие нельзя отменить."):
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
            messagebox.showerror("Удаление встречи","Не удалось удалить встречу:\n"+str(e))

    def auto_ai_if_needed(self):
        if getattr(self.app,"processing",False):
            return
        c=db()
        row=c.execute("select transcript,project,points,audio from meetings where id=?",(self.meeting_id,)).fetchone()
        task_count=c.execute("select count(*) n from tasks where meeting_id=?",(self.meeting_id,)).fetchone()["n"]
        c.close()
        if not row or not (row["transcript"] or "").strip():
            return
        # Анализ считается отсутствующим, если нет тезисов ИЛИ нет записанных задач.
        # Это также чинит старые встречи, где транскрибация уже была, а AI не отработал.
        if (row["points"] or "").strip() and task_count > 0:
            return
        self.transcribe_action.config(text="⏳ Тезисы или задачи не найдены — AI запускается автоматически…")
        self.app.pending_meeting_id=self.meeting_id
        self.app.last_recording=Path(row["audio"]) if row["audio"] else None
        self.app.processing=True
        threading.Thread(target=self.app.process_ai_background,args=(self.meeting_id,row["transcript"],row["project"] or ""),daemon=True).start()

    def retry_ai_analysis(self):
        c=db(); row=c.execute("select transcript,project,title,audio from meetings where id=?",(self.meeting_id,)).fetchone(); c.close()
        if not row or not (row["transcript"] or "").strip():
            messagebox.showinfo("AI-анализ","Сначала дождитесь расшифровки встречи.")
            return
        tr=row["transcript"]
        project=row["project"] or ""
        self.app.pending_meeting_id=self.meeting_id
        self.app.last_recording=Path(row["audio"]) if row["audio"] else None
        self.app.processing=True
        if hasattr(self,"retry_ai_btn"):
            self.retry_ai_btn.config(text="⏳ Формирую тезисы и задачи…",state="disabled")
        self.transcribe_action.config(text="⏳ AI формирует тезисы и задачи…")
        threading.Thread(target=self.app.process_ai_background,args=(self.meeting_id,tr,project),daemon=True).start()

    def retry_transcription(self):
        audio=self.audio_path
        if not audio or not Path(audio).exists():
            messagebox.showerror("Транскрибация","Файл записи не найден.")
            return
        source=self.source_path(audio)
        if not Path(source).exists():
            source=audio
        c=db()
        c.execute("update meetings set status=?,transcript=? where id=?",("transcribing","",self.meeting_id))
        c.commit(); c.close()
        self.app.pending_meeting_id=self.meeting_id
        self.app.last_recording=Path(audio)
        self.app.processing=True
        self.app.status.set("Расшифровка выполняется в фоне…")
        self.show_transcription_progress()
        threading.Thread(
            target=self.app.process_meeting_background,
            args=(self.meeting_id,Path(audio),Path(source)),
            daemon=True
        ).start()

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

class MeetingHubTrayIcon(pystray.Icon):
    """Tray icon: show the context menu on both left and right mouse clicks."""
    def _on_notify(self,wparam,lparam):
        try:
            from pystray._util import win32 as _pystray_win32
            if lparam in (_pystray_win32.WM_LBUTTONUP,_pystray_win32.WM_RBUTTONUP):
                lparam=_pystray_win32.WM_RBUTTONUP
        except Exception:
            pass
        return super()._on_notify(wparam,lparam)


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
        tk.Label(left,text="v"+VERSION,bg="#151820",fg="#6f7787",font=("Segoe UI",9)).pack(pady=(0,12))
        self.update_label=tk.Label(left,text="",bg="#151820",fg="#6fe0a1",font=("Segoe UI",9),cursor="hand2",wraplength=160,justify="left")
        self.update_label.pack(fill="x",padx=18,pady=(0,8))
        for name,cmd in [("Главная",self.home),("Новая встреча",self.new),("Встречи",self.meetings),("Задачи",self.tasks),("📥 Загрузить запись",self.import_recording),("Проверить обновления",self.check_updates_now)]:
            tk.Button(left,text=name,command=cmd,anchor="w",bg="#151820",fg="#b0b7c5",bd=0,padx=20,pady=10).pack(fill="x")
        self.home()
        threading.Thread(target=self.check_updates_background,daemon=True).start()
    def check_updates_background(self):
        try:
            url="https://raw.githubusercontent.com/Valentin812/meeting-hub/main/app.py"
            req=urllib.request.Request(url,headers={"User-Agent":"MeetingHub"})
            remote=urllib.request.urlopen(req,timeout=5).read().decode("utf-8","ignore")
            m=re.search(r'VERSION="([^"]+)"',remote)
            if m and self.version_tuple(m.group(1))>self.version_tuple(VERSION):
                rv=m.group(1)
                self.after(0,lambda:self.show_update_notice(rv))
        except Exception:
            pass
    def version_tuple(self,v):
        try: return tuple(int(x) for x in v.split("."))
        except: return (0,)
    def show_update_notice(self,remote):
        if not hasattr(self,"update_label"): return
        self.update_label.config(text=f"🟢 Доступна новая версия {remote}\nНажмите для проверки",fg="#6fe0a1")
        self.update_label.bind("<Button-1>",lambda e:self.check_updates_now())
    def check_updates_now(self):
        self.update_label.config(text="Проверяю обновления…",fg="#c7ccd6")
        threading.Thread(target=self._check_updates_now,daemon=True).start()
    def _check_updates_now(self):
        try:
            url="https://raw.githubusercontent.com/Valentin812/meeting-hub/main/app.py"
            req=urllib.request.Request(url,headers={"User-Agent":"MeetingHub"})
            remote=urllib.request.urlopen(req,timeout=5).read().decode("utf-8","ignore")
            m=re.search(r'VERSION="([^"]+)"',remote)
            rv=m.group(1) if m else VERSION
            if self.version_tuple(rv)>self.version_tuple(VERSION):
                self.after(0,lambda:self.show_update_notice(rv))
                self.after(0,lambda:messagebox.showinfo("Обновление Meeting Hub",f"Доступна новая версия {rv}.\n\nПока обновление нужно установить новым установщиком."))
            else:
                self.after(0,lambda:self.update_label.config(text="✓ У вас последняя версия",fg="#7f8898"))
        except Exception:
            self.after(0,lambda:self.update_label.config(text="Не удалось проверить обновления",fg="#d78b8b"))
    def import_recording(self):
        path=filedialog.askopenfilename(title="Выберите запись встречи",filetypes=[
            ("Аудио/видео","*.wav *.mp3 *.m4a *.flac *.ogg *.mp4 *.webm"),
            ("Все файлы","*.*")])
        if not path: return
        src=Path(path)
        if not src.exists(): return
        try:
            stamp=datetime.now().strftime("%Y%m%d_%H%M%S")
            dst=RECORDINGS/f"{stamp}_{src.name}"
            shutil.copy2(src,dst)
            now=datetime.now().isoformat(timespec="seconds")
            title=src.stem[:120] or "Импортированная встреча"
            c=db()
            c.execute("insert into meetings(title,project,started,audio,transcript,summary,decisions,questions,points,status) values(?,?,?,?,?,?,?,?,?,?)",
                      (title,"",now,str(dst),"","","","",json.dumps([],ensure_ascii=False),"transcribing"))
            mid=c.execute("select last_insert_rowid()").fetchone()[0]
            c.commit(); c.close()
            self.status.set("Запись загружена. Расшифровка выполняется…")
            self.meetings()
            # Для внешнего файла передаём сам исходник: faster-whisper умеет читать WAV/MP3/M4A/FLAC/OGG.
            # Если это стерео с разными каналами — transcribe_stereo сохранит таймкоды и разделение по каналам.
            threading.Thread(target=self.process_meeting_background,args=(mid,str(dst),str(dst)),daemon=True).start()
        except Exception as e:
            messagebox.showerror("Загрузка записи","Не удалось загрузить запись:\n"+str(e))


    def setup_tray(self):
        # Только основные действия — без списка последних записей и версии.
        menu=pystray.Menu(
            pystray.MenuItem(lambda item:self.tray_status(),lambda icon,item:False),
            pystray.MenuItem(lambda item:"✓ Автоподключение включено" if self.auto_enabled else "○ Автоподключение выключено",self.toggle_auto),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda item:"▶ Принудительно начать запись",self.tray_start,enabled=lambda item:self.rec is None and self.countdown_window is None and not self.processing),
            pystray.MenuItem(lambda item:"■ Принудительно остановить запись",self.tray_stop,enabled=lambda item:self.rec is not None),
            pystray.MenuItem(lambda item:"🔇 Включить микрофон" if self.mic_muted else "🔇 Выключить микрофон",self.tray_mute,enabled=lambda item:self.rec is not None),
            pystray.MenuItem("📁 Открыть записи",lambda icon,item:open_recordings_folder()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Открыть Meeting Hub",lambda icon,item:self.show_window()),
            pystray.MenuItem("Выйти",lambda icon,item:self.quit_app())
        )
        self.icon=MeetingHubTrayIcon("MeetingHub",tray_image(), "Meeting Hub v"+VERSION,menu)
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
    def tray_start(self,icon,item): self.after(0,self.force_start_recording)
    def force_start_recording(self):
        if self.rec or self.processing: return
        self._close_countdown()
        self.force_stop_lock=False
        self.start_recording(self.titlev.get() or "Новая встреча",False)
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
        project=self.project.get()
        threading.Thread(target=self.process_ai_background,args=(mid,tr,project),daemon=True).start()
        self.status.set("Расшифровка готова. Формирую тезисы и задачи…")
    def process_ai_background(self,mid,tr,project):
        data={}
        for attempt in range(1,4):
            data=ai_analyze(tr,project)
            points=data.get("points") if isinstance(data,dict) else None
            if points:
                break
            if attempt<3:
                time.sleep(5*attempt)
        self.after(0,lambda d=data:self.finish_ai(mid,tr,d))

    def finish_ai(self,mid,tr,data):
        if data.get("ai_error") or not data.get("points"):
            self.processing=False; self.pending_meeting_id=mid; self.refresh_tray()
            if hasattr(self,"rec_btn"): self.rec_btn.config(text="● НАЧАТЬ ЗАПИСЬ",state="normal")
            self.status.set("Расшифровка готова. AI пока не сформировал тезисы и задачи. При открытии встречи попробую снова.")
            self.meetings(); return
        self.processing=False
        self.pending_meeting_id=mid
        if self.last_recording is None:
            try:
                c=db(); row=c.execute("select audio from meetings where id=?",(mid,)).fetchone(); c.close()
                if row and row["audio"]: self.last_recording=Path(row["audio"])
            except Exception: pass
        if hasattr(self,"retry_ai_btn"):
            try: self.retry_ai_btn.destroy()
            except Exception: pass
        # AI-результат сохраняем автоматически. Пользователь может потом изменить название встречи.
        self.save_meeting(data,self.last_recording,tr)

    def review(self,data,path,tr):
        # Показываем пользователю результат локального AI перед записью тезисов и задач.
        Review(self,data,lambda d:self.save_meeting(d,path,tr))

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
            row=tk.Frame(self.main,bg="#1b202a")
            row.pack(fill="x",padx=30,pady=2)
            tk.Button(row,text=f"{m['started'][:16]}  •  {m['title']}  •  {m['project'] or 'Без проекта'}",command=lambda x=m:self.show_meeting_detail(x),bg="#1b202a",fg="white",anchor="w",padx=15,pady=9,bd=0,activebackground="#252b38",activeforeground="white").pack(side="left",fill="x",expand=True)
            status=m['status'] or ''
            if status=='transcribing':
                st='⏳ Транскрибация…'
            elif status=='transcription_error':
                st='⚠ Ошибка'
            elif status in ('transcribed','approved'):
                st='✓ Готово'
            else:
                st=''
            if st:
                tk.Label(row,text=st,bg="#1b202a",fg="#8f96a3",font=("Segoe UI",9)).pack(side="right",padx=15)
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
