"""Чат с локальным ИИ (Ollama) о встрече: ответы строятся только по итогам и расшифровке."""
import json
import re
import urllib.request

OLLAMA_URL = "http://localhost:11434/api/chat"
MAX_CONTEXT_CHARS = 9000          # ~3 тыс. токенов: помещается в окно qwen2.5:3b вместе с историей
MAX_HISTORY = 8                   # последних реплик диалога

SYSTEM = ("Ты помощник по записи рабочей встречи. Отвечай по-русски, коротко и по делу, обычным текстом. "
          "Опирайся ТОЛЬКО на материалы встречи ниже. Если в них ответа нет — так и скажи, ничего не выдумывай. "
          "Когда цитируешь, указывай, кто говорил.")


def _words(text):
    return {w for w in re.findall(r"\w{4,}", (text or "").lower())}


def build_context(title, description, summary, transcript, question, limit=MAX_CONTEXT_CHARS):
    """Заголовок и итоги берём целиком, а из расшифровки — самые подходящие вопросу куски."""
    head = []
    if title:
        head.append(f"Название: {title}")
    if description:
        head.append(f"Описание: {description}")
    if summary:
        head.append("Итоги встречи:\n" + summary.strip())
    head_text = "\n".join(head)
    room = max(0, limit - len(head_text) - 40)
    lines = [l for l in (transcript or "").splitlines() if l.strip()]
    if sum(len(l) + 1 for l in lines) <= room:
        picked = lines
    else:
        q = _words(question)
        scored = sorted(range(len(lines)), key=lambda i: (-len(q & _words(lines[i])), i))
        keep, used = set(), 0
        for i in scored:
            for j in (i, i + 1):                       # реплика и ответ на неё
                if j < len(lines) and j not in keep and used + len(lines[j]) + 1 <= room:
                    keep.add(j)
                    used += len(lines[j]) + 1
            if used >= room:
                break
        picked = [lines[i] for i in sorted(keep)]
    body = "\n".join(picked)
    return (head_text + ("\n\nРасшифровка:\n" + body if body else "")).strip()


def build_messages(context, history, question):
    msgs = [{"role": "system", "content": SYSTEM + "\n\nМАТЕРИАЛЫ ВСТРЕЧИ:\n" + (context or "(пусто)")}]
    for m in list(history)[-MAX_HISTORY:]:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            msgs.append({"role": m["role"], "content": m["content"]})
    msgs.append({"role": "user", "content": question})
    return msgs


def stream_answer(model, messages, on_text, cancel_event=None, url=OLLAMA_URL, num_ctx=8192):
    """Пишет ответ по кусочкам в on_text(кусок). Возвращает полный текст."""
    payload = {"model": model, "stream": True, "messages": messages,
               "options": {"temperature": 0.3, "num_ctx": num_ctx}}
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    out = []
    response = urllib.request.urlopen(req, timeout=900)
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                break
            line = response.readline()
            if not line:
                break
            part = json.loads(line.decode("utf-8"))
            if part.get("error"):
                raise RuntimeError(part["error"])
            piece = part.get("message", {}).get("content", "")
            if piece:
                out.append(piece)
                on_text(piece)
            if part.get("done"):
                break
    finally:
        response.close()
    return "".join(out)
