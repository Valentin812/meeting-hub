# -*- coding: utf-8 -*-
"""Итоги встречи по расшифровке: длинный текст режется на части, части объединяются.

Локальная модель (Ollama) видит ограниченный кусок текста, поэтому часовая встреча целиком не помещается.
Здесь: разбиение по репликам, анализ каждой части, слияние без повторов и общее резюме из резюме частей.
Модель подключается как llm(system, user) -> dict, поэтому её можно заменить (например, на ГигаЧат).
"""
import json
import re
import urllib.request

OLLAMA_URL = "http://localhost:11434/api/chat"
CHUNK_CHARS = 6000
OVERLAP_LINES = 3
LISTS = ("decisions", "tasks", "open_questions", "uncertain_fragments")
KEYS = {"decisions": "text", "tasks": "task", "open_questions": "question", "uncertain_fragments": "text"}

SYSTEM_PROMPT = """Ты — помощник по протоколированию рабочих встреч на русском языке.
Работай ТОЛЬКО с фактами из переданной расшифровки. Не выдумывай решения, задачи, имена, суммы и сроки.
Распознавание речи может содержать ошибки. Если смысл неясен, пометь фрагмент как требующий проверки.
Различай:
- summary: короткое резюме без повторов;
- decisions: только явно принятые решения;
- tasks: только явные поручения/действия. Если ответственный или срок не назван, оставь поле пустым;
- open_questions: вопросы, которые остались без ответа или требуют уточнения.
Верни строго JSON без markdown и без текста вокруг, по схеме:
{
 "title": "название встречи по сути, 3–7 слов, без кавычек и точки",
 "description": "1–2 предложения: о чём была встреча",
 "summary": ["..."],
 "decisions": [{"text":"...", "evidence":"дословный короткий фрагмент"}],
 "tasks": [{"task":"...", "owner":"", "deadline":"", "evidence":"дословный короткий фрагмент", "confidence":"high|medium|low"}],
 "open_questions": [{"question":"...", "evidence":"дословный короткий фрагмент"}],
 "uncertain_fragments": [{"text":"...", "reason":"..."}]
}
Не превращай предложение или обсуждение в решение, если согласие не выражено явно.
Фразы «просит добавить», «можно добавить», «надо бы», «обсудили» сами по себе НЕ доказывают, что решение принято.
Если нет явного подтверждения согласия, не добавляй фразу в decisions.
Если неясно, было ли поручение принято, добавь фрагмент в uncertain_fragments, а не в tasks.
Не выдумывай формулировки вроде «отменять не нужно», если этого нет в расшифровке.
Не дублируй одну и ту же фразу в decisions и tasks без отдельного явного основания.
В evidence приводи только короткую цитату, реально присутствующую в расшифровке.
"""

MERGE_PROMPT = """Ты — помощник по протоколированию встреч на русском языке.
Тебе дали краткие резюме последовательных частей одной встречи. Объедини их в итоговое резюме:
5–8 коротких пунктов без повторов, только факты из переданных резюме, ничего не выдумывай.
Также придумай название встречи по сути (3–7 слов, без кавычек и точки) и описание в 1–2 предложения.
Верни строго JSON без markdown: {"title": "...", "description": "...", "summary": ["..."]}
"""


class Cancelled(InterruptedError):
    pass


def ollama_chat(model, system, user, cancel_event=None, num_ctx=8192):
    payload = {
        "model": model, "stream": True, "format": "json",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "options": {"temperature": 0.1, "num_thread": 2, "num_ctx": num_ctx},
    }
    req = urllib.request.Request(OLLAMA_URL, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    chunks = []
    response = urllib.request.urlopen(req, timeout=900)
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise Cancelled("Анализ остановлен для встречи.")
            line = response.readline()
            if not line:
                break
            part = json.loads(line.decode("utf-8"))
            if part.get("error"):
                raise RuntimeError(part["error"])
            text = part.get("message", {}).get("content", "")
            if text:
                chunks.append(text)
            if part.get("done"):
                break
    finally:
        response.close()
    if cancel_event is not None and cancel_event.is_set():
        raise Cancelled("Анализ остановлен для встречи.")
    return parse_json("".join(chunks))


def parse_json(content):
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            raise ValueError("Модель вернула ответ, который не удалось прочитать как JSON.")
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("Модель вернула не объект JSON.")
    return data


def split_transcript(text, limit=CHUNK_CHARS, overlap=OVERLAP_LINES):
    """Разбить по строкам (репликам) на части не длиннее limit; соседние части перекрываются на несколько строк."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(text) <= limit:
        return [text.strip()]
    chunks, cur, size = [], [], 0
    for ln in lines:
        while len(ln) > limit:                       # очень длинная строка без переносов
            if cur:
                chunks.append("\n".join(cur)); cur, size = [], 0
            chunks.append(ln[:limit]); ln = ln[limit:]
        if size + len(ln) + 1 > limit and cur:
            chunks.append("\n".join(cur))
            cur = cur[-overlap:] if overlap else []
            size = sum(len(x) + 1 for x in cur)
        cur.append(ln); size += len(ln) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def _norm(s):
    return re.sub(r"[\W_]+", " ", str(s).lower()).strip()


def clean_title(text, limit=70):
    t = re.sub(r"\s+", " ", str(text or "")).strip().strip("\"'«»“”„ ").rstrip(".!")
    if t.lower() in ("название встречи", "встреча", "без названия", "..."):
        return ""
    return (t[:limit].rsplit(" ", 1)[0] if len(t) > limit else t).strip()


def clean_description(text, limit=260):
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    if t in ("...", "описание"):
        return ""
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return t


def merge_results(parts):
    out = {"title": "", "description": "", "summary": [], **{k: [] for k in LISTS}}
    for p in parts:
        out["title"] = out["title"] or clean_title(p.get("title"))
        out["description"] = out["description"] or clean_description(p.get("description"))
    seen = {k: set() for k in LISTS}
    for p in parts:
        for line in p.get("summary") or []:
            if _norm(line) and _norm(line) not in {_norm(x) for x in out["summary"]}:
                out["summary"].append(line)
        for k in LISTS:
            for item in p.get(k) or []:
                if not isinstance(item, dict):
                    continue
                key = _norm(item.get(KEYS[k], ""))
                if key and key not in seen[k]:
                    seen[k].add(key)
                    out[k].append(item)
    return out


def analyze_transcript(transcript, llm, cancel_event=None, progress=None):
    """Итоги встречи. llm(system, user) -> dict. Для длинных текстов — по частям и общее резюме."""
    chunks = split_transcript(transcript)
    parts = []
    for i, chunk in enumerate(chunks):
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled("Анализ остановлен для встречи.")
        head = "Проанализируй расшифровку встречи. Верни JSON по схеме из системного сообщения."
        if len(chunks) > 1:
            head += f" Это часть {i + 1} из {len(chunks)}: анализируй только её."
        parts.append(llm(SYSTEM_PROMPT, head + "\n\nРАСШИФРОВКА:\n" + chunk))
        if progress:
            progress((i + 1) / len(chunks))
    merged = merge_results(parts)
    if len(chunks) > 1:
        bullets = [f"Часть {i + 1}: " + " ".join(map(str, p.get("summary") or [])) for i, p in enumerate(parts)]
        try:
            final = llm(MERGE_PROMPT, "РЕЗЮМЕ ЧАСТЕЙ:\n" + "\n".join(bullets))
            if final.get("summary"):
                merged["summary"] = [str(x) for x in final["summary"]]
            merged["title"] = clean_title(final.get("title")) or merged["title"]
            merged["description"] = clean_description(final.get("description")) or merged["description"]
        except Cancelled:
            raise
        except Exception:
            pass                                     # остаются объединённые резюме частей
    return merged
