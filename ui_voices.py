# -*- coding: utf-8 -*-
"""Голоса участников в интерфейсе: определение после расшифровки, вкладка «Голоса», известные голоса."""
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox

import theme
import transcriber as tr_mod
import voices


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
        names = list(self.voice_profiles().names())
        for c in (voices.read_speakers(wav) or {}).get("clusters", []):
            if (c.get("name") or "").strip() and c["name"] not in names:
                names.append(c["name"])
        win = tk.Toplevel(self.root)
        win.title("Кто это говорит?")
        win.transient(self.root)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=f"Выбрано реплик: {len(idx)}. Кто их на самом деле говорит?",
                  wraplength=360, justify="left").pack(anchor="w")
        var = tk.StringVar()
        box = ttk.Combobox(frm, textvariable=var, values=sorted(names), width=34)
        box.pack(anchor="w", pady=(10, 4))
        box.focus_set()
        ttk.Label(frm, text="Выберите из списка или впишите нового человека. Программа запомнит его голос "
                  "по этим репликам.", style="Small.TLabel", wraplength=360, justify="left").pack(anchor="w")
        bar = ttk.Frame(frm)
        bar.pack(anchor="e", pady=(14, 0))

        def apply():
            name = var.get().strip()
            if not name:
                return
            win.destroy()
            self._run_reassign(wav, idx, name)
        ttk.Button(bar, text="Применить", style="Accent.TButton", command=apply).pack(side="left")
        ttk.Button(bar, text="Отмена", command=win.destroy).pack(side="left", padx=(8, 0))
        box.bind("<Return>", lambda _e: apply())

    def _run_reassign(self, wav, idx, name):
        profiles = self.voice_profiles()
        data_dir = self.data_dir
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

    def build_voices_tab(self, nb):
        self.voices_tab = ttk.Frame(nb, padding=(4, 10, 4, 0))
        nb.add(self.voices_tab, text="Голоса")
        self._voice_rows = []

    def render_voices(self, wav):
        tab = self.voices_tab
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
            ttk.Label(grid, text=voices.label_of(c) if not c.get("name") else f"Голос {c['id']}",
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
            var = tk.StringVar(value=c.get("name", ""))
            ttk.Combobox(grid, textvariable=var, values=names, width=22).grid(row=row, column=3, sticky="w")
            if c.get("auto"):
                ttk.Label(grid, text="узнан по голосу", style="Small.TLabel").grid(row=row, column=4, padx=8)
            self._voice_rows.append((c, var))
        bar = ttk.Frame(tab)
        bar.pack(anchor="w", pady=(14, 0))
        ttk.Button(bar, text="Сохранить имена", style="Accent.TButton",
                   command=lambda: self.save_voice_names(wav)).pack(side="left")
        ttk.Button(bar, text="Подписать по известным", command=lambda: self.rematch_voices(wav)).pack(
            side="left", padx=(8, 0))
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
