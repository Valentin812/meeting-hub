# -*- coding: utf-8 -*-
"""Meeting Hub — отправка готовых встреч в Канбан (вкладка «Встречи»).

Подключение: один раз входите в Канбан (почта и пароль вводятся в окне и НЕ сохраняются), сервер выдаёт
длинный ключ доступа, программа хранит только его. Ключ можно отозвать в Канбане. Дальше программа сама
отправляет итоги, решения, задачи и расшифровку каждой готовой встречи. Повторная отправка обновляет встречу.
"""
import json
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_URL = "https://vdlfdnhfiiltdyrucdtn.supabase.co"
PUBLIC_KEY = "sb_publishable_uXYluucSC1Grq-Tub20S6Q_rvDJj7HH"      # открытый ключ приложения Канбан
FUNCTION = "/functions/v1/meeting-intake"


class KanbanError(Exception):
    pass


def _request(url, body, headers, timeout=30, opener=urllib.request.urlopen):
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "MeetingHub", **headers})
    try:
        with opener(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")[:300]
        try:
            data = json.loads(raw)
            raw = data.get("error_description") or data.get("msg") or data.get("error") or raw
        except (ValueError, AttributeError):
            pass
        raise KanbanError(f"Канбан ответил {e.code}: {raw}")
    except (OSError, ValueError) as e:
        raise KanbanError(f"нет связи с Канбаном: {getattr(e, 'reason', e)}")


def connect(email, password, label="Meeting Hub", base_url=BASE_URL, key=PUBLIC_KEY, opener=urllib.request.urlopen):
    """Войти в Канбан и получить ключ доступа для программы. Возвращает (ключ, почта). Пароль нигде не хранится."""
    if not email.strip() or not password:
        raise KanbanError("укажите почту и пароль от Канбана")
    session = _request(f"{base_url}/auth/v1/token?grant_type=password",
                       {"email": email.strip(), "password": password}, {"apikey": key}, opener=opener)
    jwt = session.get("access_token")
    if not jwt:
        raise KanbanError("Канбан не подтвердил вход")
    data = _request(base_url + FUNCTION, {"action": "issue_token", "label": label},
                    {"apikey": key, "Authorization": "Bearer " + jwt}, opener=opener)
    if not data.get("token"):
        raise KanbanError(data.get("error") or "Канбан не выдал ключ доступа")
    return data["token"], data.get("email") or email.strip()


def check(token, base_url=BASE_URL, key=PUBLIC_KEY, opener=urllib.request.urlopen):
    if not (token or "").strip():
        raise KanbanError("Канбан не подключён")
    data = _request(base_url + FUNCTION, {"action": "check"}, {"apikey": key, "x-meeting-token": token}, 20, opener)
    if not data.get("ok"):
        raise KanbanError(data.get("error") or "ключ не принят")
    return data.get("email", "")


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _held_at(wav, meta):
    stem = Path(wav).stem
    for text in (meta.get("recorded_at"), meta.get("started")):
        try:
            return datetime.fromisoformat(text).astimezone().isoformat(timespec="seconds")
        except (TypeError, ValueError):
            pass
    import re
    m = re.search(r"(20\d\d)(\d\d)(\d\d)[_-]?(\d\d)?(\d\d)?", stem)
    if m:
        try:
            y, mo, d, h, mi = (int(x or 0) for x in m.groups())
            return datetime(y, mo, d, h, mi).astimezone().isoformat(timespec="seconds")
        except ValueError:
            return None
    return None


def build_payload(wav):
    """Данные встречи для Канбана: из .json, .summary.json и текста расшифровки рядом с записью."""
    wav = Path(wav)
    meta = _read(wav.with_suffix(".json"))
    summary = _read(wav.with_name(wav.stem + ".summary.json"))
    try:
        transcript = wav.with_suffix(".txt").read_text(encoding="utf-8")
    except OSError:
        transcript = ""
    title = (meta.get("title") or summary.get("title") or meta.get("window") or meta.get("meeting")
             or wav.stem).strip()
    return {
        "external_id": wav.stem, "title": title, "held_at": _held_at(wav, meta),
        "duration_sec": meta.get("duration_sec"), "meeting_app": meta.get("meeting") or "",
        "description": meta.get("description") or summary.get("description") or "",
        "summary": summary.get("summary") or [], "decisions": summary.get("decisions") or [],
        "tasks": summary.get("tasks") or [], "open_questions": summary.get("open_questions") or [],
        "transcript": transcript,
    }


def push(token, wav, base_url=BASE_URL, key=PUBLIC_KEY, opener=urllib.request.urlopen):
    """Отправить встречу. Возвращает id встречи в Канбане. Встреча без итогов и расшифровки не отправляется."""
    payload = build_payload(wav)
    if not payload["transcript"].strip() and not payload["summary"]:
        raise KanbanError("у встречи ещё нет расшифровки")
    data = _request(base_url + FUNCTION, {"action": "push", "meeting": payload},
                    {"apikey": key, "x-meeting-token": token}, 60, opener)
    if not data.get("ok"):
        raise KanbanError(data.get("error") or "Канбан не принял встречу")
    return data.get("id", "")
