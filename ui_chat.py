"""Вкладка «Спросить ИИ»: вопросы по выбранной встрече (или по всем) к локальной Ollama."""
import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import chat
import privacy
import theme
import transcriber as tr_mod


class ChatUI:
    def build_chat_tab(self, nb):
        tab = ttk.Frame(nb, padding=(0, 8, 0, 0))
        nb.add(tab, text="Спросить ИИ")
        self.chat_histories = {}
        self.chat_busy = False
        self.chat_cancel = threading.Event()
        pal = self.pal
        self.chat_log = tk.Text(tab, wrap="word", height=10)
        theme.text_style(self.chat_log, pal)
        self.chat_log.config(state="disabled")
        self.chat_log.pack(fill="both", expand=True)
        self.chat_tags()
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(6, 0))
        self.chat_var = tk.StringVar()
        self.chat_entry = ttk.Entry(row, textvariable=self.chat_var)
        self.chat_entry.pack(side="left", fill="x", expand=True)
        self.chat_entry.bind("<Return>", lambda _e: self.chat_send())
        self.chat_btn = ttk.Button(row, text="Спросить", style="Accent.TButton", command=self.chat_send)
        self.chat_btn.pack(side="left", padx=(6, 0))
        ttk.Button(row, text="Очистить", command=self.chat_clear).pack(side="left", padx=(6, 0))
        self.chat_note = ttk.Label(tab, style="Small.TLabel", wraplength=560, justify="left",
                                   text=self.chat_note_text())
        self.chat_note.pack(anchor="w", pady=(4, 0))
        self.render_chat()

    def chat_tags(self):
        pal = self.pal
        self.chat_log.tag_configure("me", foreground=pal["me"], font=(theme.FONT, 10, "bold"))
        self.chat_log.tag_configure("ai", foreground=pal["they"], font=(theme.FONT, 10, "bold"))
        self.chat_log.tag_configure("hint", foreground=pal["muted"])

    def chat_restyle(self):
        theme.text_style(self.chat_log, self.pal)
        self.chat_log.config(state="disabled")
        self.chat_tags()

    # ---- журнал
    def _chat_key(self):
        sel = getattr(self, "selected", None)
        return str(sel) if sel is not None else ""

    def _chat_write(self, text, tag=None):
        self.chat_log.config(state="normal")
        self.chat_log.insert("end", text, tag or ())
        self.chat_log.see("end")
        self.chat_log.config(state="disabled")

    def render_chat(self):
        """Показать диалог выбранной встречи (вызывается при смене встречи)."""
        if not hasattr(self, "chat_log"):
            return
        if self.chat_busy and getattr(self, "_chat_shown", None) == self._chat_key():
            return                                     # идёт ответ — не стираем его на полуслове
        self._chat_shown = self._chat_key()
        if hasattr(self, "chat_note"):
            self.chat_note.config(text=self.chat_note_text())
        self.chat_log.config(state="normal")
        self.chat_log.delete("1.0", "end")
        self.chat_log.config(state="disabled")
        hist = self.chat_histories.get(self._chat_key(), [])
        if not hist:
            self._chat_write("Спросите, например: «О чём договорились?», «Какие задачи у меня?», "
                             "«Что говорил Иван про бюджет?»\n", "hint")
        for m in hist:
            self._chat_write(("Вы: " if m["role"] == "user" else "ИИ: "), "me" if m["role"] == "user" else "ai")
            self._chat_write(m["content"] + "\n\n")

    def chat_clear(self):
        self.chat_histories.pop(self._chat_key(), None)
        self.render_chat()

    # ---- вопрос
    def _chat_context(self, wav, question):
        if wav is None:
            return ""
        wav = Path(wav)
        meta, result, text = {}, {}, ""
        try:
            meta = json.loads(wav.with_suffix(".json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        try:
            result = json.loads(self.summary_path(wav).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        try:
            text = tr_mod.output_paths(wav)[0].read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            pass
        limit = chat.CLOUD_CONTEXT_CHARS if self.cloud_chat_on() and not self.is_confidential(wav) \
            else chat.MAX_CONTEXT_CHARS
        return chat.build_context(meta.get("title") or result.get("title"), meta.get("description"),
                                  chat.summary_text(result), text, question, limit=limit)

    def chat_note_text(self):
        who = ("Отвечает GigaChat (текст проходит маскирование личных данных); конфиденциальные встречи — "
               "только локальная модель." if self.cloud_chat_on() else "Отвечает локальная модель на этом компьютере.")
        return who + " Модель может ошибаться — важное проверяйте по расшифровке."

    def cloud_chat_on(self):
        return bool(self.settings.get("giga_enabled") and (self.settings.get("giga_code") or "").strip())

    def chat_send(self):
        question = self.chat_var.get().strip()
        if not question or self.chat_busy:
            return
        wav = self.selected
        key = self._chat_key()
        context = self._chat_context(wav, question)
        if not context.strip():
            self._chat_write("В этой встрече пока нет ни расшифровки, ни итогов — спрашивать не о чем.\n", "hint")
            return
        hist = self.chat_histories.setdefault(key, [])
        messages = chat.build_messages(context, hist, question)
        hist.append({"role": "user", "content": question})
        self.chat_var.set("")
        self._chat_write("Вы: ", "me")
        self._chat_write(question + "\n\n")
        self._chat_write("ИИ: ", "ai")
        self.chat_busy = True
        self.chat_btn.config(state="disabled")
        self.chat_cancel.clear()
        model = self.settings.get("ollama_model") or self.default_llm
        put = self._ui_calls.put

        def work():
            answer, error = "", ""
            try:
                router = self.llm_router(model)
                answer = router.stream(
                    messages, lambda t: put((self._chat_piece, (key, t))), self.chat_cancel,
                    meeting=Path(wav).stem if wav else None,
                    local_only=bool(wav) and self.is_confidential(wav))
                if "запасной" in router.name:
                    put((self._chat_piece, (key, "\n(GigaChat недоступен — ответила локальная модель)")))
            except Exception as e:
                error = str(e)
            put((self._chat_done, (key, answer, error)))
        threading.Thread(target=work, name="ai-chat", daemon=True).start()

    def _chat_piece(self, key, text):
        if key == self._chat_key():
            self._chat_write(text)

    def _chat_done(self, key, answer, error):
        self.chat_busy = False
        self.chat_btn.config(state="normal")
        hist = self.chat_histories.setdefault(key, [])
        if error:
            hint = ("Не удалось связаться с локальной моделью. Откройте ⚙ → «Расшифровка и итоги» → "
                    f"«Установить ИИ…». Подробности: {error[:150]}")
            if key == self._chat_key():
                self._chat_write(hint + "\n\n", "hint")
            if hist and hist[-1]["role"] == "user":
                hist.pop()
            return
        hist.append({"role": "assistant", "content": answer.strip() or "(пустой ответ)"})
        if key == self._chat_key():
            self._chat_write("\n\n")


    # ---- отдельный чат по всем встречам
    def open_all_chat(self):
        old = getattr(self, "all_chat_win", None)
        if old is not None and old.winfo_exists():
            old.lift()
            return
        win = self.all_chat_win = tk.Toplevel(self.root)
        win.title("Чат по всем встречам")
        win.geometry("720x560")
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)
        log = tk.Text(frame, wrap="word", height=10)
        theme.text_style(log, self.pal)
        log.tag_configure("me", foreground=self.pal["me"], font=(theme.FONT, 10, "bold"))
        log.tag_configure("ai", foreground=self.pal["they"], font=(theme.FONT, 10, "bold"))
        log.tag_configure("hint", foreground=self.pal["muted"])
        log.pack(fill="both", expand=True)
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(8, 0))
        var = tk.StringVar()
        entry = ttk.Entry(row, textvariable=var)
        entry.pack(side="left", fill="x", expand=True)
        btn = ttk.Button(row, text="Спросить", style="Accent.TButton")
        btn.pack(side="left", padx=(6, 0))
        note = ("Отвечает по итогам всех встреч. "
                + ("Конфиденциальные встречи не используются. " if self.cloud_chat_on() else "")
                + "Модель может ошибаться — важное проверяйте.")
        ttk.Label(frame, text=note, style="Small.TLabel", wraplength=680, justify="left").pack(anchor="w", pady=(6, 0))
        state = {"busy": False, "hist": []}

        def write(text, tag=None):
            if not log.winfo_exists():
                return
            log.config(state="normal")
            log.insert("end", text, tag or ())
            log.see("end")
            log.config(state="disabled")

        write("Спросите, например: «Что решили по бюджету?», «Какие у меня задачи на этой неделе?»\n", "hint")
        log.config(state="disabled")
        cancel = threading.Event()
        put = self._ui_calls.put

        def done(answer, error):
            state["busy"] = False
            if btn.winfo_exists():
                btn.config(state="normal")
            if error:
                write("Не удалось получить ответ: " + error[:150] + "\n\n", "hint")
                if state["hist"] and state["hist"][-1]["role"] == "user":
                    state["hist"].pop()
                return
            state["hist"].append({"role": "assistant", "content": answer.strip() or "(пустой ответ)"})
            write("\n\n")

        def send(_e=None):
            question = var.get().strip()
            if not question or state["busy"]:
                return
            skip = privacy.confidential_stems(self.rec_folder, self.privacy_words()) if self.cloud_chat_on() else ()
            limit = chat.CLOUD_CONTEXT_CHARS if self.cloud_chat_on() else chat.MAX_CONTEXT_CHARS
            context = chat.all_meetings_context(self.rec_folder, question, limit=limit, skip=skip)
            if not context.strip():
                write("Подходящих итогов встреч не нашлось.\n", "hint")
                return
            messages = chat.build_messages(context, state["hist"], question)
            state["hist"].append({"role": "user", "content": question})
            var.set("")
            write("Вы: ", "me")
            write(question + "\n\n")
            write("ИИ: ", "ai")
            state["busy"] = True
            btn.config(state="disabled")
            cancel.clear()
            model = self.settings.get("ollama_model") or self.default_llm

            def work():
                answer, error = "", ""
                try:
                    router = self.llm_router(model)
                    answer = router.stream(
                        messages, lambda t: put((write, (t,))), cancel, meeting="все встречи")
                    if "запасной" in router.name:
                        put((write, ("\n(GigaChat недоступен — ответила локальная модель)",)))
                except Exception as e:
                    error = str(e)
                put((done, (answer, error)))
            threading.Thread(target=work, name="ai-chat-all", daemon=True).start()

        btn.config(command=send)
        entry.bind("<Return>", send)
        win.bind("<Destroy>", lambda e: cancel.set() if e.widget is win else None)
        entry.focus_set()
