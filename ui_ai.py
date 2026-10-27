"""Окно «Установить ИИ»: Ollama и модель для итогов и чата ставятся из программы, с прогрессом."""
import threading
import tkinter as tk
import webbrowser
from tkinter import ttk, messagebox

import ai_setup


class AiSetupUI:
    def ai_model(self):
        return (self.settings.get("ollama_model") or self.default_llm).strip()

    def _save_ai_model(self, model):
        if self.settings.get("ollama_model") != model:
            self.settings["ollama_model"] = model
            self.save_settings()

    def offer_ai_setup(self, why=""):
        """Предложить установить ИИ (не чаще раза за запуск, если не просили сами)."""
        if getattr(self, "_ai_offered", False):
            return
        self._ai_offered = True
        if messagebox.askyesno("ИИ для итогов", (why + "\n\n" if why else "") +
                               "Для итогов, задач и чата нужна локальная модель — она работает на этом компьютере, "
                               "записи никуда не отправляются. Скачать и установить её сейчас? Это около 2–3 ГБ, "
                               "делается один раз.", parent=self.root):
            self.open_ai_setup()

    def open_ai_setup(self):
        if getattr(self, "_ai_win", None) is not None and self._ai_win.winfo_exists():
            self._ai_win.lift()
            return
        model = self.ai_model()
        ram = ai_setup.ram_gb()
        better = ai_setup.upgrade_for(model, ram)
        ready = ai_setup.status(model) == "ok"
        if better:
            rec, size = better
            ask = (f"В вашем компьютере {ram:.0f} ГБ памяти — подойдёт более сильная модель «{rec}» "
                   f"(загрузка около {size:g} ГБ, итоги точнее, но готовятся немного дольше). Использовать её?"
                   if ram else f"Подойдёт более сильная модель «{rec}». Использовать её?")
            if messagebox.askyesno("ИИ для итогов", ask, parent=self.root):
                model = rec
                ready = ai_setup.status(model) == "ok"
        if ready:
            self._save_ai_model(model)
            messagebox.showinfo("ИИ для итогов", f"Всё готово: модель «{model}» установлена и работает.",
                                parent=self.root)
            return
        win = self._ai_win = tk.Toplevel(self.root)
        win.title("Установка ИИ")
        win.transient(self.root)
        win.resizable(False, False)
        box = ttk.Frame(win, padding=20)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text="Установка ИИ для итогов и чата", style="H2.TLabel").pack(anchor="w")
        label = ttk.Label(box, text="Начинаю…", style="Muted.TLabel", width=56)
        label.pack(anchor="w", pady=(8, 6))
        bar = ttk.Progressbar(box, mode="determinate", maximum=100, length=440)
        bar.pack(fill="x")
        ttk.Label(box, style="Small.TLabel", wraplength=440, justify="left", text=(
            "Сначала программа Ollama (официальная, с ollama.com), затем модель. Окно можно оставить "
            "открытым и заниматься своими делами. Если интернет пропадёт — нажмите «Установить ИИ» ещё раз, "
            "загрузка продолжится.")).pack(anchor="w", pady=(8, 0))
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(14, 0))
        link = ttk.Button(row, text="Скачать Ollama вручную", command=lambda: webbrowser.open(ai_setup.SITE))
        link.pack(side="left")
        cancel = threading.Event()
        btn = ttk.Button(row, text="Отмена", command=cancel.set)
        btn.pack(side="right")
        win.protocol("WM_DELETE_WINDOW", cancel.set)

        def progress(stage, done, total):
            def ui():
                if not win.winfo_exists():
                    return
                if total:
                    bar.config(mode="determinate")
                    bar["value"] = done * 100 / total
                    label.config(text=f"{stage}: {done / 1048576:.0f} из {total / 1048576:.0f} МБ")
                else:
                    label.config(text=stage + "…")
            self._ui_calls.put((ui, ()))

        def work():
            try:
                ai_setup.setup(model, self.data_dir / "updates", progress, cancel)
                self._ui_calls.put((self._save_ai_model, (model,)))
                self._ui_calls.put((finish, ("",)))
            except Exception as e:
                self._ui_calls.put((finish, (str(e) or "Неизвестная ошибка",)))

        def finish(error):
            try:
                win.destroy()
            except tk.TclError:
                pass
            if not error:
                self.last_event_text = "ИИ установлен — итоги и чат работают."
                self._journal_note("ИИ", f"Установлена модель {model}", "user")
                messagebox.showinfo("ИИ для итогов", "Готово! Итоги появятся у новых встреч сами. Для уже "
                                    "записанных нажмите «Подготовить итоги» в карточке встречи.", parent=self.root)
            elif "отменен" not in error:
                messagebox.showerror("ИИ для итогов", error + f"\n\nМожно поставить вручную: {ai_setup.SITE}\n"
                                     f"и затем в командной строке: ollama pull {model}", parent=self.root)
            self.refresh_status()
        threading.Thread(target=work, name="ai-setup", daemon=True).start()
