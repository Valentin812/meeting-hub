# -*- coding: utf-8 -*-
"""Meeting Hub — отправка протоколов встреч в CRM «Этажи» (вкладка «Протоколы»).

Вход в CRM — логин и пароль сотрудника (отдельного ключа доступа у API нет). Сессия действует 8 часов,
поэтому пароль хранится на этом компьютере в зашифрованном средствами Windows виде (DPAPI: расшифровать
его может только эта учётная запись Windows на этом компьютере) и вход обновляется сам.
Правила те же, что для Канбана: конфиденциальные встречи не отправляются, расшифровка — только если
это разрешено в настройках.

Загрузка (справка «API загрузки протоколов встреч CRM «Этажи»», package.json 2.83.1):
POST /api/protocols {title, protocol, transcript, source:"upload", requestKey} создаёт протокол;
повторная отправка изменённой встречи — PATCH /api/protocols/{id} с актуальным version.
Отдельных полей для участников, решений и задач API не принимает — они входят в текст протокола;
задачи CRM предлагает сама (ИИ протоколов), их подтверждает человек.
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

PROTOCOL_PATH = "/api/protocols"
TITLE_MAX, PROTOCOL_MAX, TRANSCRIPT_MAX = 200, 60000, 250000
SESSION_SEC = 8 * 3600


class CrmError(Exception):
    pass


# ---------------------------------------------------------------------------
# Пароль: шифрование Windows (DPAPI)
# ---------------------------------------------------------------------------
def _dpapi(data, protect):
    if os.name != "nt":
        raise CrmError("хранение пароля доступно только в Windows")
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buf = ctypes.create_string_buffer(data, len(data))
    inp = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(inp), "MeetingHub CRM", None, None, None, 0, ctypes.byref(out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise CrmError("не удалось " + ("зашифровать" if protect else "расшифровать") + " пароль")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect_password(password):
    return _dpapi(password.encode("utf-8"), True).hex()


def unprotect_password(blob):
    if not blob:
        raise CrmError("пароль от CRM не сохранён — подключите CRM в настройках")
    return _dpapi(bytes.fromhex(blob), False).decode("utf-8")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _request(method, url, body=None, headers=None, timeout=30, opener=urllib.request.urlopen):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          "User-Agent": "MeetingHub", **(headers or {})})
    try:
        with opener(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")[:300]
        try:
            raw = json.loads(raw).get("error") or raw
        except (ValueError, AttributeError):
            pass
        err = CrmError(f"CRM ответила {e.code}: {raw}")
        err.code = e.code
        raise err
    except (OSError, ValueError) as e:
        raise CrmError(f"нет связи с CRM: {getattr(e, 'reason', e)}")


def normalize_url(url):
    url = (url or "").strip().rstrip("/")
    if url and not url.startswith(("https://", "http://")):
        url = "https://" + url
    return url


class Client:
    """Вход по логину и паролю, сессия кэшируется и обновляется сама (при 401 — один повторный вход)."""

    def __init__(self, base_url, product_id, login, password, opener=urllib.request.urlopen, clock=time.time):
        self.base = normalize_url(base_url)
        self.product, self.login_name, self.password = str(product_id or "").strip(), (login or "").strip(), password
        self.opener, self.clock = opener, clock
        self.token, self.until = None, 0.0
        if not self.base or not self.login_name:
            raise CrmError("укажите адрес CRM и логин")

    def _login(self):
        data = _request("POST", self.base + "/api/auth/login", {"login": self.login_name, "password": self.password},
                        opener=self.opener)
        if not data.get("token"):
            raise CrmError("CRM не подтвердила вход")
        self.token = data["token"]
        self.until = self.clock() + min(float(data.get("expiresIn") or SESSION_SEC), SESSION_SEC) - 300
        return data.get("operator") or {}

    def call(self, method, path, body=None):
        if not self.token or self.clock() >= self.until:
            self._login()
        headers = {"Authorization": "Bearer " + self.token}
        if self.product:
            headers["X-Product-Id"] = self.product
        try:
            return _request(method, self.base + path, body, headers, opener=self.opener)
        except CrmError as e:
            if getattr(e, "code", None) != 401:
                raise
            self._login()                                 # сессию отозвали или истекла раньше срока
            headers["Authorization"] = "Bearer " + self.token
            return _request(method, self.base + path, body, headers, opener=self.opener)

    def me(self):
        data = self.call("GET", "/api/me")
        return data.get("name") or data.get("login") or data.get("email") or "вход выполнен"


def ready():
    return PROTOCOL_PATH is not None


GENERIC = ("Вы", "Собеседники", "Участники")


def _participants(wav):
    """Имена говоривших из расшифровки (подписанные голоса); безымянные «Голос N» не берём."""
    import transcriber as tr
    try:
        segs = json.loads(tr.output_paths(wav)[1].read_text(encoding="utf-8")).get("segments") or []
    except (OSError, ValueError):
        return []
    names = []
    for s_ in segs:
        n = (s_.get("speaker") or "").strip()
        if n and n not in GENERIC and not n.startswith("Голос ") and n not in names:
            names.append(n)
    return names


def _item_text(x, key):
    return (x.get(key) or x.get("text") or "").strip() if isinstance(x, dict) else str(x).strip()


def build_protocol(wav, with_transcript=False):
    """Тело запроса POST /api/protocols из готовой встречи (итоги, решения, поручения, вопросы)."""
    import kanban_sync
    p = kanban_sync.build_payload(wav, with_transcript)
    title = p["title"]
    when = (p.get("held_at") or "")[:10]
    if when and when[:4].isdigit():
        date = f"{when[8:10]}.{when[5:7]}.{when[:4]}"
        if date not in title:
            title = f"{title} — {date}"
    parts = []
    summary = p["summary"] if isinstance(p["summary"], list) else [p["summary"]]
    if p.get("description") or any(str(x).strip() for x in summary):
        lines = [p["description"]] if p.get("description") else []
        lines += ["- " + str(x).strip() for x in summary if str(x).strip()]
        parts.append("КРАТКО О ВСТРЕЧЕ\n" + "\n".join(lines))
    people = _participants(wav)
    if people:
        parts.append("УЧАСТНИКИ\n" + "; ".join(people) + ".")
    decisions = [_item_text(d, "text") for d in p["decisions"]]
    if any(decisions):
        parts.append("РЕШЕНИЯ\n" + "\n".join("- " + d for d in decisions if d))
    tasks = []
    for t in p["tasks"]:
        line = _item_text(t, "task")
        if not line:
            continue
        if isinstance(t, dict) and (t.get("owner") or "").strip():
            line += "; ответственный: " + t["owner"].strip()
        if isinstance(t, dict) and (t.get("deadline") or "").strip():
            line += "; срок: " + t["deadline"].strip()
        tasks.append("- " + line)
    if tasks:
        parts.append("ПОРУЧЕНИЯ\n" + "\n".join(tasks))
    questions = [_item_text(q, "question") for q in p["open_questions"]]
    if any(questions):
        parts.append("ОТКРЫТЫЕ ВОПРОСЫ\n" + "\n".join("- " + q for q in questions if q))
    protocol = "\n\n".join(parts).replace("\x00", "")[:PROTOCOL_MAX]
    transcript = (p["transcript"] or "").replace("\x00", "")[:TRANSCRIPT_MAX]
    if not protocol.strip() and not transcript.strip():
        raise CrmError("у встречи ещё нет итогов")
    body = {"title": (title.strip() or "Встреча")[:TITLE_MAX], "protocol": protocol, "transcript": transcript,
            "source": "upload"}
    digest = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    stem = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "_-") else "-" for ch in p["external_id"])[:80]
    body["requestKey"] = f"meetinghub-{stem}-{digest[:16]}"
    return body


def push(client, wav, with_transcript=False):
    """Отправить протокол встречи. Первый раз — POST, дальше — PATCH того же протокола (id и version
    хранятся в .json встречи). Возвращает id протокола в CRM."""
    import importer
    import privacy
    body = build_protocol(wav, with_transcript)
    meta = privacy.read_meta(wav)
    pid, version = meta.get("crm_protocol_id"), meta.get("crm_protocol_version")
    if pid:
        patch = {k: body[k] for k in ("title", "protocol", "transcript")}
        try:
            data = client.call("PATCH", f"{PROTOCOL_PATH}/{pid}", dict(patch, version=version))
        except CrmError as e:
            code = getattr(e, "code", None)
            if code == 404:                                      # протокол удалили в CRM — загружаем заново
                pid = None
            elif code == 409:                                    # версия устарела: перечитываем и пробуем ещё раз
                fresh = (client.call("GET", f"{PROTOCOL_PATH}/{pid}").get("item") or {})
                if fresh.get("protected"):
                    raise CrmError("протокол в CRM защищён от изменений — снимите защиту, чтобы обновить")
                data = client.call("PATCH", f"{PROTOCOL_PATH}/{pid}", dict(patch, version=fresh.get("version")))
            else:
                raise
    if not pid:
        data = client.call("POST", PROTOCOL_PATH, body)
    item = data.get("item") or data
    if not item.get("id"):
        raise CrmError("CRM не вернула номер протокола")
    importer.write_meta(wav, {"crm_protocol_id": str(item["id"]), "crm_protocol_version": item.get("version")})
    return str(item["id"])
