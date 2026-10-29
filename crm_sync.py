# -*- coding: utf-8 -*-
"""Meeting Hub — отправка протоколов встреч в CRM «Этажи» (вкладка «Протоколы»).

Вход в CRM — логин и пароль сотрудника (отдельного ключа доступа у API нет). Сессия действует 8 часов,
поэтому пароль хранится на этом компьютере в зашифрованном средствами Windows виде (DPAPI: расшифровать
его может только эта учётная запись Windows на этом компьютере) и вход обновляется сам.
Правила те же, что для Канбана: конфиденциальные встречи не отправляются, расшифровка — только если
это разрешено в настройках.

Запрос загрузки протокола пока не описан (в справке по API есть только задачи), поэтому PROTOCOL_PATH
не задан и отправка не выполняется: подключение и проверку входа можно настроить уже сейчас.
"""
import json
import os
import time
import urllib.error
import urllib.request

PROTOCOL_PATH = None          # будет известен после описания API протоколов
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


def push(client, wav, with_transcript=False):
    """Отправить протокол встречи во вкладку «Протоколы». Пока запрос не описан — CrmError."""
    if not ready():
        raise CrmError("загрузка протоколов в CRM ещё не настроена: нужен описанный запрос API протоколов")
    import kanban_sync
    payload = kanban_sync.build_payload(wav, with_transcript)
    if not payload["summary"] and not payload["transcript"].strip():
        raise CrmError("у встречи ещё нет итогов")
    payload["requestKey"] = "meetinghub-" + payload["external_id"]
    return client.call("POST", PROTOCOL_PATH, payload)
