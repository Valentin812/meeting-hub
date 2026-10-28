# -*- coding: utf-8 -*-
"""Meeting Hub — автоматические названия встреч.

Порядок: название, введённое вручную, не трогаем никогда. Иначе сразу после расшифровки берём название
из окна встречи (если оно что-то говорит, а не просто «Zoom»), а если нет — главные слова из разговора.
Когда готовы итоги, их название от ИИ заменяет временное. К любому автоназванию добавляется дата записи.
"""
import json
import re
from pathlib import Path

import importer
from meeting_detector import strip_browser_suffix

DATE_RE = re.compile(r"\s*[·•]\s*\d{2}\.\d{2}\.\d{4}\s*$")
STAMP_RE = re.compile(r"(20\d\d)[-_.]?(\d\d)[-_.]?(\d\d)")
SPLIT_RE = re.compile(r"\s+[-–—|:·]\s+|\s*\|\s*")
ROOM_RE = re.compile(r"^[a-z]{3}-[a-z]{4}-[a-z]{3}$|^[\d\s\-_.:/]+$|^https?://", re.I)
GENERIC = {
    "zoom", "zoom meeting", "zoom webinar", "zoom workplace", "microsoft teams", "teams", "meeting", "meet",
    "google meet", "телемост", "яндекс телемост", "telemost", "webex", "cisco webex", "jitsi", "jitsi meet",
    "skype", "discord", "встреча", "собрание", "конференция", "звонок", "видеозвонок", "call", "chat", "чат",
    "meeting in", "новая вкладка", "new tab", "без названия", "untitled", "файл",
}
STOP = set("""это как так что чтобы если когда тогда потом можно нужно надо будет была были было есть
которые который которая которое этого этой этот этом тоже чтоб просто вообще сейчас сегодня вчера завтра
давайте давай хорошо ладно спасибо пожалуйста здравствуйте привет конечно наверное например значит
смотрите слушайте короче сначала теперь почему поэтому потому только очень более менее несколько
здесь тут там туда сюда уже ещё еще всё все всех всем всего себя себе меня тебя тебе нас вас вам нам
мне мной они его её них ним ними этих этими такой такая такие таким также чего чему чем кого кому
кто какой какая какие каким какую через после перед между около другой другие других другим
сказал сказала говорит говорят думаю думаем считаю считаем сделать сделаем делать работать
вопрос вопросы момент вариант время раз раза слово слова человек люди""".split())


def date_text(stem):
    m = STAMP_RE.search(stem or "")
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else ""


def with_date(title, stem):
    """«Название · 26.10.2026». Дата не дублируется."""
    base = DATE_RE.sub("", (title or "").strip()).strip()
    day = date_text(stem)
    if not base or not day or day in base:
        return base
    return f"{base} · {day}"


def from_window(window, meeting=""):
    """Название из заголовка окна встречи; "" если там только название программы или код комнаты."""
    text = strip_browser_suffix(window or "")
    for part in SPLIT_RE.split(text):
        p = part.strip(" \t-–—|:·\"'«»")
        low = p.lower()
        if len(p) < 3 or low in GENERIC or low == (meeting or "").lower() or ROOM_RE.match(p):
            continue
        if any(g in low for g in ("google chrome", "microsoft edge", "mozilla firefox", "yandex")):
            continue
        return p[:70].strip()
    return ""


def from_words(text, top=3, min_words=40):
    """Главные слова из разговора: «Обсуждение: сроки, релиз, бюджет». "" если текста мало."""
    words = re.findall(r"[а-яёa-z]{5,}", (text or "").lower())
    if len(words) < min_words:
        return ""
    count, surface = {}, {}
    for w in words:
        if w in STOP:
            continue
        stem = w[:5]
        count[stem] = count.get(stem, 0) + 1
        forms = surface.setdefault(stem, {})
        forms[w] = forms.get(w, 0) + 1
    ranked = sorted(count.items(), key=lambda kv: -kv[1])
    picked = [s for s, n in ranked if n >= 3][:top]
    if not picked:
        return ""
    names = [max(surface[s].items(), key=lambda kv: kv[1])[0] for s in picked]
    return "Обсуждение: " + ", ".join(names)


def transcript_text(wav):
    js = Path(wav).with_name(Path(wav).stem + ".transcript.json")
    try:
        data = json.loads(js.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return " ".join(str(s.get("text", "")) for s in data.get("segments", []))


def apply_initial_name(wav):
    """Сразу после расшифровки: временное название, если своего нет. Возвращает записанное название или ""."""
    wav = Path(wav)
    try:
        meta = json.loads(wav.with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    if meta.get("title_by_user") or meta.get("imported"):
        return ""
    if meta.get("title") and meta.get("title_source") in (None, "", "ai"):
        return ""                                   # уже есть настоящее название (от ИИ или заданное раньше)
    name, source = from_window(meta.get("window"), meta.get("meeting")), "window"
    if not name:
        name, source = from_words(transcript_text(wav)), "words"
    if not name:
        return ""
    title = with_date(name, wav.stem)
    if title == meta.get("title"):
        return ""
    importer.write_meta(wav, {"title": title, "title_auto": True, "title_source": source})
    return title


def backfill(folder):
    """Один раз: названия для старых записей без названия (по итогам, если они есть, иначе по окну/словам).
    Названия, введённые вручную или заданные раньше, не трогаем. Возвращает число переименованных."""
    done = 0
    for wav in sorted(Path(folder).glob("*.wav")):
        if ".part" in wav.name or ".mic." in wav.name or ".system." in wav.name:
            continue
        try:
            meta = json.loads(wav.with_suffix(".json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        if meta.get("title_by_user") or (meta.get("title") and not meta.get("title_auto")):
            continue
        summary = {}
        try:
            summary = json.loads(wav.with_name(wav.stem + ".summary.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        before = meta.get("title")
        if summary.get("title"):
            importer.auto_name(wav, summary)
        else:
            apply_initial_name(wav)
        try:
            after = json.loads(wav.with_suffix(".json").read_text(encoding="utf-8")).get("title")
        except (OSError, ValueError):
            after = before
        done += 1 if after != before else 0
    return done
