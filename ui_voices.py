# -*- coding: utf-8 -*-
"""Голоса участников в интерфейсе: определение после расшифровки, вкладка «Голоса», известные голоса."""
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox

import theme
import transcriber as tr_mod
import voices
import widgets


class VoicesUI:
    # ------------------------------------------------------------------
    # Движок: определить голоса встречи в фоне
    # ------------------------------------------------------------------
    def voice_profiles(self):
        if getattr(self, "_voice_profiles", None) is None:
            self._voice_profiles = voices.Profiles(self.data_dir / "voices.json")
        return self._voice_profiles

    def voices_wanted(self):
        return bool(self.settings.get("voices_enabled", True))

    def voice_busy(self):
        return bool(getattr(self, "voice_jobs", None))

    def run_voices(self, wav, then=None, manual=False, speakers=None):
        """Определить голоса встречи. then() вызывается в потоке интерфейса в любом случае."""
        wav = Path(wav)
        jobs = self.__dict__.setdefault("voice_jobs", set())
        if str(wav) in jobs:
            return
        jobs.add(str(wav))
        self.refresh_status()
        profiles = self.voice_profiles()
        data_dir = self.data_dir

        def finish(note="", result=None, error=""):
            jobs.discard(str(wav))
            if error:
                self._journal_note("Голоса не определены", f"{wav.name}: {error}", "auto" if not manual else "user")
                if manual:
                    messagebox.showwarning("Голоса", error, parent=self.root)
            elif result is not None:
                n = len(result["clusters"])
                known = sum(1 for c in result["clusters"] if c.get("name"))
                self._journal_note("Голоса определены", f"{wav.name}: голосов {n}, узнано {known}", "auto",
                                   file=wav.name)
                if n > known:
                    self.last_event_text = "Голоса определены — подпишите их на вкладке «Голоса»."
                    if n >= 2 and not manual:
                        self.toast(f"В записи найдено голосов: {n}, не подписано: {n - known}. "
                                   "Подпишите их — программа запомнит людей и в следующих встречах "
                                   "подпишет сама.", seconds=60,
                                   action=("Подписать", lambda: self.open_voices_tab(wav)))
            self.refresh_records()
            if self.selected == wav:
                self.show_meeting(wav, force=True)
            self.refresh_status()
            if then is not None:
                then()

        def work():
            try:
                ok, why = voices.available()
                if not ok:
                    raise voices.VoicesError(why)
                if not voices.model_ready(data_dir):
                    self._ui_calls.put((lambda: self._journal_note(
                        "Скачиваю модель голосов", "один раз, около 27 МБ", "auto"), ()))
                    voices.download_model(data_dir)
                emb = getattr(self, "_embedder", None)
                if emb is None:
                    emb = self._embedder = voices.Embedder(voices.model_path(data_dir))
                result = voices.analyze(wav, emb, profiles, speakers=speakers)
                err = ""
            except Exception as e:
                self.log.exception("Голоса")
                result, err = None, str(e)
            self._ui_calls.put((lambda: finish(result=result, error=err), ()))

        threading.Thread(target=work, name="voices", daemon=True).start()

    # ------------------------------------------------------------------
    # Вкладка «Голоса»
    # ------------------------------------------------------------------
    def open_voices_tab(self, wav):
        wav = Path(wav)
        self.selected = wav
        if hasattr(self, "records_tree") and self.records_tree.exists(str(wav)):
            self.records_tree.select(str(wav))
        self.show_meeting(wav, force=True)
        try:
            self.detail_nb.select(self.voices_tab)
        except tk.TclError:
            pass
        self.show_window()

    # ------------------------------------------------------------------
    # Исправить голос у выделенных реплик (вкладка «Расшифровка»)
    # ------------------------------------------------------------------
    def selected_segments(self):
        """Номера реплик, которых касается выделение мышью в расшифровке."""
        text = self.tr_text
        try:
            first, last = text.index("sel.first"), text.index("sel.last")
        except tk.TclError:                                # выделения нет — берём реплику, по которой щёлкнули
            last_seg = getattr(self, "_last_seg", None)
            return [last_seg] if last_seg is not None and last_seg < len(getattr(self, "_segments", [])) else []
        hit = []
        for i, (_s, _e, tag) in enumerate(getattr(self, "_segments", [])):
            ranges = text.tag_ranges(tag)
            if ranges and text.compare(ranges[0], "<", last) and text.compare(ranges[-1], ">", first):
                hit.append(i)
        return hit

    def fix_selected_voice(self):
        wav = self.selected
        if wav is None:
            return
        wav = Path(wav)
        idx = self.selected_segments()
        if not idx:
            messagebox.showinfo("Голоса", "Щёлкните по реплике с неверным голосом (или выделите мышью несколько "
                                "реплик) и нажмите кнопку ещё раз. Можно и правой кнопкой мыши по реплике.",
                                parent=self.root)
            return
        if not wav.exists():
            messagebox.showinfo("Голоса", "Аудио удалено — по звуку голос не запомнить.", parent=self.root)
            return
        data = tr_mod.output_paths(wav)[1]
        try:
            import json
            segs = json.loads(data.read_text(encoding="utf-8")).get("segments") or []
        except (OSError, ValueError):
            segs = []
        mine = [i for i in idx if i < len(segs) and (segs[i].get("speaker") == tr_mod.SPEAKERS[0])]
        idx = [i for i in idx if i not in mine]
        if not idx:
            messagebox.showinfo("Голоса", "Выделены только ваши реплики (микрофон) — они подписаны сами.",
                                parent=self.root)
            return
        self.ask_person(wav, f"Выбрано реплик: {len(idx)}. Кто их на самом деле говорит?",
                        lambda name: self._run_reassign(wav, idx, name))

    NEW_PERSON = "＋ Новый участник…"

    def person_names(self, wav):
        names = list(self.voice_profiles().names())
        for c in (voices.read_speakers(wav) or {}).get("clusters", []):
            if (c.get("name") or "").strip() and c["name"] not in names:
                names.append(c["name"])
        return sorted(names)

    def ask_person(self, wav, question, on_name, parent=None):
        """Окно «Кто это говорит?»: выбрать человека из списка или добавить нового."""
        win = tk.Toplevel(parent or self.root)
        win.title("Кто это говорит?")
        win.transient(parent or self.root)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=question, wraplength=380, justify="left").pack(anchor="w")
        var = tk.StringVar()
        row = ttk.Frame(frm)
        row.pack(anchor="w", fill="x", pady=(10, 4))
        box = ttk.Combobox(row, textvariable=var, values=self.person_names(wav) + [self.NEW_PERSON], width=30)
        box.pack(side="left")
        hint = ttk.Label(frm, text="Выберите из списка или нажмите «Новый участник» и впишите имя. Программа "
                         "запомнит его голос по этим репликам.", style="Small.TLabel", wraplength=380,
                         justify="left")

        def new_person(_e=None):
            var.set("")
            hint.config(text="Впишите имя нового участника и нажмите «Применить».")
            box.focus_set()
            box.icursor("end")
        ttk.Button(row, text="＋ Новый участник", command=new_person).pack(side="left", padx=(8, 0))
        box.bind("<<ComboboxSelected>>", lambda _e: new_person() if var.get() == self.NEW_PERSON else None)
        hint.pack(anchor="w")
        box.focus_set()
        bar = ttk.Frame(frm)
        bar.pack(anchor="e", pady=(14, 0))

        def apply():
            name = var.get().strip()
            if not name or name == self.NEW_PERSON:
                return
            win.destroy()
            on_name(name)
        ttk.Button(bar, text="Применить", style="Accent.TButton", command=apply).pack(side="left")
        ttk.Button(bar, text="Отмена", command=win.destroy).pack(side="left", padx=(8, 0))
        box.bind("<Return>", lambda _e: apply())
        return win

    def _run_reassign(self, wav, idx, name, quiet=False, then=None):
        """Перенести реплики к человеку и запомнить голос (в фоне). quiet — без обновления протокола (игра)."""
        profiles = self.voice_profiles()
        data_dir = self.data_dir
        if not quiet:
            self.last_event_text = "Запоминаю голос и исправляю подписи…"
            self.refresh_status()

        def work():
            try:
                ok, why = voices.available()
                if not ok:
                    raise voices.VoicesError(why)
                if not voices.model_ready(data_dir):
                    voices.download_model(data_dir)
                emb = getattr(self, "_embedder", None)
                if emb is None:
                    emb = self._embedder = voices.Embedder(voices.model_path(data_dir))
                voices.reassign_segments(wav, idx, name, emb, profiles)
                err = ""
            except Exception as e:
                self.log.exception("Исправление голоса")
                err = str(e)

            def done():
                if then is not None:
                    then(err)
                    return
                self.last_event_text = ""
                if err:
                    messagebox.showwarning("Голоса", err, parent=self.root)
                else:
                    self._journal_note("Голос исправлен", f"{wav.name}: реплик {len(idx)} → {name}", "user",
                                       file=wav.name)
                    self._voices_changed(wav)
                self.refresh_status()
            self._ui_calls.put((done, ()))
        threading.Thread(target=work, name="voice-fix", daemon=True).start()

    def clip_is_other(self, wav, cluster_id, clip):
        idx = voices.segments_for_clip(wav, cluster_id, clip)
        if not idx:
            messagebox.showinfo("Голоса", "Не нашлась реплика этого отрывка — определите голоса заново.",
                                parent=self.root)
            return
        self.ask_person(wav, "Кто говорит в этом отрывке?", lambda name: self._run_reassign(wav, idx, name))

    # ------------------------------------------------------------------
    # Игра «Кто говорит?»: программа предлагает реплику и имя, вы подтверждаете или исправляете
    # ------------------------------------------------------------------
    def voice_game(self, wav=None):
        wav = Path(wav or self.selected or "")
        if not wav.name or not wav.exists():
            messagebox.showinfo("Кто говорит?", "Выберите встречу с сохранённым аудио.", parent=self.root)
            return
        if not voices.read_speakers(wav):
            messagebox.showinfo("Кто говорит?", "Сначала определите голоса этой встречи (вкладка «Голоса»).",
                                parent=self.root)
            return
        rounds = voices.game_rounds(wav)
        if not rounds:
            messagebox.showinfo("Кто говорит?", "Подпишите хотя бы один голос на вкладке «Голоса» — "
                                "тогда будет что проверять.", parent=self.root)
            return
        win = tk.Toplevel(self.root)
        win.title("Кто говорит?")
        win.transient(self.root)
        win.minsize(560, 320)
        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)
        step = ttk.Label(frm, style="Small.TLabel")
        step.pack(anchor="w")
        quote = ttk.Label(frm, wraplength=520, justify="left", font=(theme.FONT, 11))
        quote.pack(anchor="w", fill="x", pady=(10, 10))
        ask = ttk.Label(frm, font=(theme.FONT, 13, "bold"))
        ask.pack(anchor="w")
        btns = ttk.Frame(frm)
        btns.pack(anchor="w", pady=(14, 0))
        note = ttk.Label(frm, style="Small.TLabel", wraplength=520, justify="left")
        note.pack(anchor="w", pady=(14, 0))
        state = {"n": 0, "yes": 0, "fixed": 0, "busy": False}

        def show():
            if not win.winfo_exists():
                return
            if state["n"] >= len(rounds):
                finish()
                return
            _i, a, b, text, name = rounds[state["n"]]
            step.config(text=f"Вопрос {state['n'] + 1} из {len(rounds)}")
            quote.config(text="«" + (text if len(text) < 400 else text[:400] + "…") + "»")
            ask.config(text=f"Это {name}?")
            self.play_clip(wav, {"start": a, "end": min(b, a + 12.0)})

        def next_round(err=""):
            state["busy"] = False
            if err:
                note.config(text="Не получилось: " + err[:150])
            state["n"] += 1
            show()

        def yes():
            if state["busy"]:
                return
            i, _a, _b, _t, name = rounds[state["n"]]
            state["busy"], state["yes"] = True, state["yes"] + 1
            note.config(text=f"Запоминаю: это {name}.")
            self._run_reassign(wav, [i], name, quiet=True, then=next_round)

        def no():
            if state["busy"]:
                return
            i = rounds[state["n"]][0]

            def chosen(other):
                state["busy"], state["fixed"] = True, state["fixed"] + 1
                note.config(text=f"Исправляю: это {other}.")
                self._run_reassign(wav, [i], other, quiet=True, then=next_round)
            self.ask_person(wav, "Кто это на самом деле?", chosen, parent=win)

        def skip_many():
            if state["busy"]:
                return
            state["many"] = state.get("many", 0) + 1
            note.config(text="Пропущено: в отрывке говорят несколько человек — для обучения он не годится.")
            next_round()

        def finish():
            if state.get("done"):
                return
            state["done"] = True
            for w in btns.winfo_children():
                w.destroy()
            step.config(text="Готово")
            quote.config(text="")
            ask.config(text=f"Подтверждено: {state['yes']}, исправлено: {state['fixed']}"
                       + (f", пропущено: {state['many']}" if state.get("many") else ""))
            note.config(text="Голоса запомнены — в следующих встречах люди будут узнаваться точнее.")
            ttk.Button(btns, text="Закрыть", style="Accent.TButton", command=win.destroy).pack(side="left")
            if state["yes"] or state["fixed"]:
                self._journal_note("Игра «Кто говорит?»", f"{wav.name}: подтверждено {state['yes']}, "
                                   f"исправлено {state['fixed']}", "user", file=wav.name)
                self._voices_changed(wav)

        ttk.Button(btns, text="Да", style="Accent.TButton", width=8, command=yes).pack(side="left")
        ttk.Button(btns, text="Нет, это…", command=no).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Не знаю", command=lambda: next_round()).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Пропустить: говорят несколько", command=lambda: skip_many()).pack(
            side="left", padx=(8, 0))
        ttk.Button(btns, text="▶ Ещё раз", command=lambda: self.play_clip(
            wav, {"start": rounds[state["n"]][1], "end": min(rounds[state["n"]][2], rounds[state["n"]][1] + 12.0)})
                   if state["n"] < len(rounds) else None).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Закончить", command=finish).pack(side="left", padx=(8, 0))
        win.bind("<Return>", lambda _e: yes())
        widgets.flowify(btns)
        win.protocol("WM_DELETE_WINDOW", lambda: (finish(), win.destroy()))
        show()

    def build_voices_tab(self, nb):
        """Вкладка с прокруткой: голосов на встрече может быть много."""
        self.voices_tab = page = ttk.Frame(nb)
        nb.add(page, text="Голоса")
        bg = ttk.Style(page).lookup("TFrame", "background") or self.pal["bg"]
        cv = self.voices_canvas = tk.Canvas(page, highlightthickness=0, bd=0, background=bg)
        sb = ttk.Scrollbar(page, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        body = self.voices_body = ttk.Frame(cv, padding=(4, 10, 12, 10))
        wid = cv.create_window((0, 0), window=body, anchor="nw")

        def fit(_e=None):
            cv.itemconfigure(wid, width=cv.winfo_width())
            cv.configure(scrollregion=(0, 0, cv.winfo_width(), max(body.winfo_reqheight(), cv.winfo_height())))
        body.bind("<Configure>", fit)
        cv.bind("<Configure>", fit)

        def wheel(e):
            w = self.root.winfo_containing(e.x_root, e.y_root)
            while w is not None:
                if w is cv:
                    if body.winfo_reqheight() > cv.winfo_height():
                        cv.yview_scroll(-1 if e.delta > 0 else 1, "units")
                    return
                w = getattr(w, "master", None)
        self.root.bind_all("<MouseWheel>", wheel, add="+")
        self._voice_rows = []

    def render_voices(self, wav):
        tab = self.voices_body
        try:
            self.voices_canvas.configure(background=ttk.Style(tab).lookup("TFrame", "background") or self.pal["bg"])
            self.voices_canvas.yview_moveto(0)
        except tk.TclError:
            pass
        for child in tab.winfo_children():
            child.destroy()
        self._voice_rows = []
        wav = Path(wav)
        has_text = tr_mod.has_transcript(wav)
        sp = voices.read_speakers(wav)

        def note(text, style="Muted.TLabel"):
            ttk.Label(tab, text=text, style=style, wraplength=520, justify="left").pack(anchor="w", pady=(0, 8))

        if not has_text:
            note("Голоса определяются после расшифровки встречи.")
            return
        if sp is None:
            note("Программа может различить, кто из участников что сказал, и подписать реплики именами. "
                 "Вы послушаете короткие отрывки и напишете имена — программа запомнит голоса и в следующих "
                 "встречах подпишет людей сама.")
            if not self.voices_wanted():
                note("Определение голосов выключено: Настройки → «Расшифровка и итоги».", "Small.TLabel")
            elif self.voice_busy():
                note("Определяю голоса…", "Small.TLabel")
            else:
                ttk.Button(tab, text="Определить голоса", style="Accent.TButton",
                           command=lambda: self.run_voices(wav, manual=True)).pack(anchor="w")
                self._count_row(tab, wav, None, "Определить")
            return
        clusters = sp.get("clusters") or []
        if not clusters:
            note("Отдельных голосов не найдено: участники говорили слишком коротко или звук один.")
            self._count_row(tab, wav, sp, "Определить заново")
            return
        note("Нажмите ▶, чтобы послушать отрывок, и впишите имя. «Вы» — это ваш микрофон, он подписан сам.")
        names = self.voice_profiles().names()
        grid = ttk.Frame(tab)
        grid.pack(fill="x")
        for row, c in enumerate(clusters[:10]):
            sec = int(c.get("seconds", 0))
            ttk.Label(grid, text=f"Голос {c['id']}",
                      font=(theme.FONT, 10, "bold"), width=11).grid(row=row, column=0, sticky="w", pady=6)
            ttk.Label(grid, text=f"{sec // 60} мин {sec % 60:02d} с", style="Muted.TLabel", width=9).grid(
                row=row, column=1, sticky="w")
            clips = ttk.Frame(grid)
            clips.grid(row=row, column=2, sticky="w", padx=(4, 12))
            for k, clip in enumerate(c.get("clips") or []):
                b = ttk.Button(clips, text=f"▶ {k + 1}", width=5,
                               command=lambda cl=clip: self.play_clip(wav, cl))
                b.pack(side="left", padx=(0, 4))
                if not wav.exists():
                    b.state(["disabled"])
            if wav.exists() and c.get("clips"):
                mb = ttk.Menubutton(grid, text="Не тот?")
                menu = tk.Menu(mb, tearoff=False)
                for k, clip in enumerate(c["clips"]):
                    menu.add_command(label=f"Отрывок {k + 1} — это другой человек…",
                                     command=lambda cl=clip, cid=c["id"]: self.clip_is_other(wav, cid, cl))
                mb["menu"] = menu
                mb.grid(row=row, column=3, sticky="w", padx=(0, 12))
            var = tk.StringVar(value=c.get("name", ""))
            cb = ttk.Combobox(grid, textvariable=var, values=names + [self.NEW_PERSON], width=22)
            cb.grid(row=row, column=4, sticky="w")
            cb.bind("<<ComboboxSelected>>", lambda _e, v=var, w=cb: (v.set(""), w.focus_set())
                    if v.get() == self.NEW_PERSON else None)
            if c.get("auto"):
                ttk.Label(grid, text="узнан по голосу", style="Small.TLabel").grid(row=row, column=5, padx=8)
            elif not c.get("name") and c.get("guess"):
                ttk.Label(grid, text=f"похоже на {c['guess']} — не уверен", style="Small.TLabel").grid(
                    row=row, column=5, padx=8)
            self._voice_rows.append((c, var))
        bar = ttk.Frame(tab)
        bar.pack(anchor="w", pady=(14, 0))
        ttk.Button(bar, text="Сохранить имена", style="Accent.TButton",
                   command=lambda: self.save_voice_names(wav)).pack(side="left")
        ttk.Button(bar, text="Подписать по известным", command=lambda: self.rematch_voices(wav)).pack(
            side="left", padx=(8, 0))
        ttk.Button(bar, text="🎯 Игра «Кто говорит?»", command=lambda: self.voice_game(wav)).pack(
            side="left", padx=(8, 0))
        widgets.flowify(bar)
        self._count_row(tab, wav, sp, "Определить заново")
        found = len(clusters)
        ttk.Label(tab, text=f"Найдено голосов: {found}. Если людей было больше или меньше — укажите число и "
                  "нажмите «Определить заново». Один человек может оказаться двумя голосами: впишите обоим одно имя.",
                  style="Small.TLabel", wraplength=520, justify="left").pack(anchor="w", pady=(6, 0))
        if not wav.exists():
            note("Аудио удалено — отрывки недоступны, но имена записать можно.", "Small.TLabel")

    def _count_row(self, parent, wav, sp, button_text):
        """«Сколько было собеседников?» — если автоматика ошиблась, число можно задать вручную."""
        row = ttk.Frame(parent)
        row.pack(anchor="w", pady=(10, 0))
        ttk.Label(row, text="Сколько было собеседников (кроме вас)?").pack(side="left")
        cur = int((sp or {}).get("speakers") or 0)
        var = tk.StringVar(value=str(cur) if cur else "авто")
        ttk.Combobox(row, textvariable=var, values=["авто"] + [str(i) for i in range(1, 11)], width=6,
                     state="readonly").pack(side="left", padx=8)

        def go():
            n = int(var.get()) if var.get().isdigit() else None
            if sp is not None and not messagebox.askyesno(
                    "Голоса", "Определить голоса заново? Имена, которые вы уже подписали, вернутся сами "
                    "там, где голос узнаётся; остальные придётся вписать снова.", parent=self.root):
                return
            self.run_voices(Path(wav), manual=True, speakers=n)
        ttk.Button(row, text=button_text, command=go).pack(side="left")
        found = len((sp or {}).get("clusters") or [])
        if sp is not None:
            def plus(delta):
                n = max(1, found + delta)
                if messagebox.askyesno("Голоса", f"Искать {n} собеседн. вместо {found}? Подписанные имена "
                                       "подставятся там, где голос узнаётся.", parent=self.root):
                    self.run_voices(Path(wav), manual=True, speakers=n)
            ttk.Button(row, text="＋ Ещё голос", command=lambda: plus(1)).pack(side="left", padx=(8, 0))
            if found > 1:
                ttk.Button(row, text="− Лишний", command=lambda: plus(-1)).pack(side="left", padx=(4, 0))
        widgets.flowify(row)
        return row

    def play_clip(self, wav, clip):
        wav = Path(wav)
        if not wav.exists():
            return
        try:
            self._ensure_player(wav)
            self.player.seek(float(clip["start"]))
            self.player.play()
        except Exception as e:
            self._player_failed(wav, e)
            return
        self._player_update()
        end = float(clip["end"])
        if getattr(self, "_clip_job", None) is not None:
            self.root.after_cancel(self._clip_job)

        def stop_at():
            self._clip_job = None
            p = self.player
            if p is None or not p.playing:
                return
            if p.position >= end - 0.05:
                p.pause()
                self._player_update()
            else:
                self._clip_job = self.root.after(150, stop_at)
        self._clip_job = self.root.after(150, stop_at)

    def _voices_changed(self, wav):
        """После новых имён: протокол и подписи обновляются; итоги — по желанию."""
        for ext in (".protocol.html", ".protocol.docx"):
            if wav.with_name(wav.stem + ext).exists():
                self.make_protocol(wav)
                break
        self.refresh_records()
        self.show_meeting(wav, force=True)

    def save_voice_names(self, wav):
        wav = Path(wav)
        profiles = self.voice_profiles()
        changed, confirmed = False, 0
        try:
            for c, var in self._voice_rows:
                name = var.get().strip()
                if name != (c.get("name") or ""):
                    voices.name_voice(wav, c["id"], name, profiles)
                    changed = True
            for c, var in self._voice_rows:                  # узнанные верно: тоже обучают программу
                if c.get("auto") and var.get().strip() == (c.get("name") or "") and \
                        voices.confirm_voice(wav, c["id"], profiles):
                    confirmed += 1
        except voices.VoicesError as e:
            messagebox.showwarning("Голоса", str(e), parent=self.root)
            return
        if not changed:
            if confirmed:
                self._journal_note("Голоса подтверждены", f"{wav.name}: {confirmed}", "user", file=wav.name)
                self.render_voices(wav)
            return
        self._journal_note("Голоса подписаны", wav.name, "user", file=wav.name)
        self._voices_changed(wav)
        if self.summary_path(wav).exists() and messagebox.askyesno(
                "Голоса", "Имена записаны в расшифровку и протокол.\n\nОбновить итоги встречи, чтобы в них "
                "тоже были имена?", parent=self.root):
            self.request_summary(wav, rerun=True)

    def rematch_voices(self, wav):
        voices.rematch(wav, self.voice_profiles())
        self._voices_changed(Path(wav))

    # ------------------------------------------------------------------
    # Известные голоса (в настройках)
    # ------------------------------------------------------------------
    def open_known_voices(self, parent=None):
        profiles = self.voice_profiles()
        win = tk.Toplevel(parent or self.root)
        win.title("Известные голоса")
        win.transient(parent or self.root)
        win.geometry("420x360")
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Люди, чьи голоса программа узнаёт. Новые добавляются, когда вы подписываете "
                  "голос на вкладке «Голоса».", style="Small.TLabel", wraplength=380, justify="left").pack(
            anchor="w", pady=(0, 8))
        lb = tk.Listbox(frm, activestyle="none", exportselection=False, font=(theme.FONT, 10),
                        highlightthickness=0, borderwidth=1, relief="flat")
        lb.pack(fill="both", expand=True)

        def fill():
            lb.delete(0, "end")
            for n in profiles.names():
                lb.insert("end", f"{n}   ({len(profiles.people[n])} отпечатков)")

        def current():
            sel = lb.curselection()
            return profiles.names()[sel[0]] if sel else None

        def rename():
            old = current()
            if not old:
                return
            from tkinter import simpledialog
            new = simpledialog.askstring("Переименовать", "Новое имя:", initialvalue=old, parent=win)
            if new and new.strip():
                profiles.rename(old, new.strip())
                profiles.save()
                fill()

        def delete():
            name = current()
            if name and messagebox.askyesno("Удалить голос", f"Забыть голос «{name}»? Подписи в уже "
                                            "расшифрованных встречах не изменятся.", parent=win):
                profiles.forget(name)
                profiles.save()
                fill()
        row = ttk.Frame(frm)
        row.pack(fill="x", pady=(10, 0))
        ttk.Button(row, text="Закрыть", command=win.destroy).pack(side="right")
        ttk.Button(row, text="Удалить", command=delete).pack(side="right", padx=(0, 8))
        ttk.Button(row, text="Переименовать", command=rename).pack(side="right", padx=(0, 8))
        fill()
