# -*- coding: utf-8 -*-
"""Meeting Hub — защита данных при работе с облаком.

* «Конфиденциально»: такая встреча никогда не уходит в GigaChat и Канбан (итоги и чат — только локальная модель).
  Метку можно поставить вручную, а можно задать слова: встречи с ними в названии/описании считаются закрытыми.
* Маскирование: перед отправкой текста в GigaChat телефоны, почты, карты, счета, ИНН и подобные номера, суммы и
  ФИО с отчеством заменяются метками [ТЕЛЕФОН-1], [СУММА-2]… Ответ облака приходит с метками, и программа
  подставляет настоящие значения обратно: соответствие живёт только в памяти на время запроса.
* Журнал отправок: что, куда и когда ушло в облако (без самих текстов).
"""
import json
import re
import threading
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Конфиденциальные встречи
# ---------------------------------------------------------------------------
def parse_words(text):
    return [w.strip().casefold() for w in re.split(r"[,;\n]", text or "") if w.strip()]


def is_confidential(meta, words=()):
    """Явная метка (True/False) важнее слов; иначе ищем слова в названии, окне и описании."""
    meta = meta or {}
    if meta.get("confidential") is not None:
        return bool(meta["confidential"])
    hay = " ".join(str(meta.get(k) or "") for k in ("title", "window", "meeting", "description")).casefold()
    return any(w and w in hay for w in words)


def read_meta(wav):
    try:
        return json.loads(Path(wav).with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def wav_confidential(wav, words=()):
    return is_confidential(read_meta(wav), words)


def set_confidential(wav, value):
    import importer
    importer.write_meta(wav, {"confidential": bool(value)})


def confidential_stems(folder, words=()):
    """Имена записей (stem) закрытых встреч в папке."""
    out = set()
    for p in Path(folder).glob("*.json"):
        name = p.name
        if name.endswith((".summary.json", ".transcript.json")) or "." in name[:-5]:
            continue
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and is_confidential(meta, words):
            out.add(name[:-5])
    return out


# ---------------------------------------------------------------------------
# Маскирование
# ---------------------------------------------------------------------------
_CAP = r"[А-ЯЁ][а-яё]+"
_PATR = r"[А-ЯЁ][а-яё]*(?:ович|евич|ьич|овн|евн|ичн)[а-яё]*"
_MONEY_UNIT = (r"(?:тыс\.?|тысяч\w*|млн\.?|миллион\w*|млрд\.?|миллиард\w*|руб\w*|₽|р\.|"
               r"долл\w*|евро|usd|eur|\$|€)")
RULES = (
    ("ПОЧТА", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("ДОКУМЕНТ", re.compile(r"(?:(?<=ИНН)|(?<=КПП)|(?<=ОГРН)|(?<=ОГРНИП)|(?<=БИК)|(?<=СНИЛС)|(?<=паспорт)|(?<=Паспорт))"
                            r"[\s:№-]*(\d[\d\s-]{4,22}\d)", re.I)),
    ("СЧЁТ", re.compile(r"(?<!\d)\d{20}(?!\d)")),
    ("КАРТА", re.compile(r"(?<!\d)(?:\d{4}[ -]){3}\d{4}(?!\d)|(?<!\d)\d{16}(?!\d)")),
    ("ТЕЛЕФОН", re.compile(r"(?<![\d])(?:\+7|8|7)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)")),
    ("СУММА", re.compile(r"\d[\d\s.,]*\s*" + _MONEY_UNIT + r"(?!\w)", re.I)),
    ("ЛИЦО", re.compile(r"\b" + _CAP + r"\s+" + _CAP + r"\s+" + _PATR + r"\b(?!\s+" + _CAP + r")|\b" + _CAP + r"\s+" + _PATR
                        + r"\s+" + _CAP + r"\b|\b" + _CAP + r"\s+" + _PATR + r"\b")),
)
LABEL_RE = re.compile(r"\[(?:ПОЧТА|ДОКУМЕНТ|СЧЁТ|КАРТА|ТЕЛЕФОН|СУММА|ЛИЦО)-\d+\]")


class Masker:
    """Заменяет чувствительные значения метками и возвращает их обратно. Один экземпляр — один запрос."""

    def __init__(self):
        self.values = {}       # метка -> исходное значение
        self._labels = {}     # (тип, значение) -> метка
        self._counts = {}

    def _label(self, kind, value):
        key = (kind, re.sub(r"\s+", " ", value.strip().casefold()))
        if key not in self._labels:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            label = f"[{kind}-{self._counts[kind]}]"
            self._labels[key] = label
            self.values[label] = value
        return self._labels[key]

    def mask(self, text):
        text = str(text or "")
        for kind, rx in RULES:
            def sub(m, kind=kind):
                if kind == "ДОКУМЕНТ":
                    return m.group(0).replace(m.group(1), self._label(kind, m.group(1)))
                return self._label(kind, m.group(0))
            text = rx.sub(sub, text)
        return text

    def unmask(self, text):
        if not self.values or not text:
            return text
        return LABEL_RE.sub(lambda m: self.values.get(m.group(0), m.group(0)), text)

    def unmask_obj(self, obj):
        if isinstance(obj, str):
            return self.unmask(obj)
        if isinstance(obj, list):
            return [self.unmask_obj(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self.unmask_obj(v) for k, v in obj.items()}
        return obj

    def mask_messages(self, messages):
        """Маскируем все сообщения: материалы встречи в чате лежат внутри системного."""
        return [dict(m, content=self.mask(m.get("content", ""))) for m in messages]

    @property
    def count(self):
        return len(self.values)


class StreamUnmasker:
    """Подставляет значения обратно в потоке кусочков: метка, разрезанная между кусочками, ждёт продолжения."""

    def __init__(self, masker):
        self.masker, self.buf = masker, ""

    def feed(self, chunk):
        text = self.buf + chunk
        cut = text.rfind("[")
        if cut >= 0 and "]" not in text[cut:] and len(text) - cut <= 24:
            self.buf, text = text[cut:], text[:cut]
        else:
            self.buf = ""
        return self.masker.unmask(text)

    def flush(self):
        text, self.buf = self.buf, ""
        return self.masker.unmask(text)


# ---------------------------------------------------------------------------
# Журнал отправок в облако
# ---------------------------------------------------------------------------
class CloudLog:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def add(self, service, meeting, what, chars=0, masked=0, ok=True, error=""):
        entry = {"time": datetime.now().isoformat(timespec="seconds"), "service": service, "meeting": meeting or "",
                 "what": what, "chars": int(chars), "masked": int(masked), "ok": bool(ok)}
        if error:
            entry["error"] = str(error)[:200]
        try:
            with self._lock, self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return entry

    def read(self, limit=300):
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for ln in lines[-limit:]:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out

    def services_for(self, meeting):
        """Куда уходила эта встреча (успешно): ['GigaChat', 'Канбан']."""
        seen = []
        for e in self.read(2000):
            if e.get("meeting") == meeting and e.get("ok") and e.get("service") not in seen:
                seen.append(e["service"])
        return seen
