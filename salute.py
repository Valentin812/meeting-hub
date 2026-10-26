# -*- coding: utf-8 -*-
"""Облачное распознавание SaluteSpeech (Сбер) с автоматическим переходом на локальную модель.

Аудио отправляется в облако Сбера только если пользователь сам включил это в настройках.
Любая ошибка (нет сети, ключ не принят, сервис не ответил, неожиданный ответ) — расшифровка
продолжается локальной моделью, ничего не теряется.
"""
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_URL = "https://smartspeech.sber.ru/rest/v1"
RATE = 16000
LANGS = {"ru": "ru-RU", "en": "en-US", "kk": "kk-KZ"}
COOLDOWN_SEC = 600


class SaluteError(Exception):
    pass


def _seconds(value):
    try:
        return float(str(value).strip().rstrip("s"))
    except (TypeError, ValueError):
        return None


def parse_result(data):
    """Ответ data:download → сегменты [{'start','end','text'}]. Терпимо к вариантам формата."""
    if isinstance(data, dict):
        data = data.get("results") or data.get("result") or [data]
    out = []
    for item in data or []:
        if not isinstance(item, dict):
            continue
        results = item.get("results") or []
        text = " ".join((r.get("normalized_text") or r.get("text") or "").strip()
                        for r in results if isinstance(r, dict)).strip()
        if not text:
            continue
        start = end = None
        if results and isinstance(results[0], dict):
            start, end = _seconds(results[0].get("start")), _seconds(results[-1].get("end"))
        if start is None:
            start = _seconds(item.get("processed_audio_start")) or 0.0
        if end is None:
            end = _seconds(item.get("processed_audio_end")) or start
        out.append({"start": round(start, 2), "end": round(max(end, start), 2), "text": text})
    out.sort(key=lambda s: s["start"])
    return out


class SaluteClient:
    def __init__(self, auth_key, scope="SALUTE_SPEECH_PERS", verify_tls=True, oauth_url=OAUTH_URL,
                 api_url=API_URL, timeout=60, poll_sec=2.0, max_wait_sec=3600):
        self.auth_key = (auth_key or "").strip()
        if self.auth_key.lower().startswith("basic "):
            self.auth_key = self.auth_key[6:].strip()
        self.scope = scope or "SALUTE_SPEECH_PERS"
        self.oauth_url, self.api_url = oauth_url, api_url.rstrip("/")
        self.timeout, self.poll_sec, self.max_wait_sec = timeout, poll_sec, max_wait_sec
        self._token, self._expires = "", 0.0
        self.ctx = None
        if not verify_tls:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    # --- HTTP
    def _request(self, url, data=None, headers=None, method=None):
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            raise SaluteError(f"сервис ответил {e.code}: {body}")
        except ssl.SSLError as e:
            raise SaluteError("ошибка сертификата сервиса. Установите сертификат Минцифры или включите "
                              f"«Не проверять сертификат Сбера» в настройках ({e})")
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), ssl.SSLError):
                raise SaluteError("ошибка сертификата сервиса. Установите сертификат Минцифры или включите "
                                  f"«Не проверять сертификат Сбера» в настройках ({e.reason})")
            raise SaluteError(f"нет связи с сервисом: {e.reason}")
        except OSError as e:
            raise SaluteError(f"нет связи с сервисом: {e}")

    def token(self):
        if not self.auth_key:
            raise SaluteError("не указан ключ авторизации")
        if self._token and time.time() < self._expires - 60:
            return self._token
        body = urllib.parse.urlencode({"scope": self.scope}).encode()
        raw = self._request(self.oauth_url, body, {
            "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
            "RqUID": str(uuid.uuid4()), "Authorization": "Basic " + self.auth_key}, "POST")
        try:
            data = json.loads(raw)
            self._token = data["access_token"]
            exp = float(data.get("expires_at") or 0)
            self._expires = exp / 1000 if exp > 1e11 else (exp or time.time() + 1800)
        except (ValueError, KeyError, TypeError):
            raise SaluteError("не удалось получить токен: неожиданный ответ сервиса")
        return self._token

    def _auth(self, extra=None):
        h = {"Authorization": "Bearer " + self.token()}
        h.update(extra or {})
        return h

    def _json(self, raw, what):
        try:
            return json.loads(raw)
        except ValueError:
            raise SaluteError(f"{what}: неожиданный ответ сервиса")

    def check(self):
        """Проверка ключа: получить токен. Возвращает текст для пользователя."""
        self._token = ""
        self.token()
        return "Подключение работает: ключ принят."

    # --- распознавание
    def transcribe_pcm(self, pcm, language="ru", should_stop=None):
        """pcm — 16-битный моно 16 кГц (bytes). Возвращает сегменты [{'start','end','text'}]."""
        stop = should_stop or (lambda _p=None: None)
        stop()
        up = self._json(self._request(
            self.api_url + "/data:upload", pcm,
            self._auth({"Content-Type": f"audio/x-pcm;bit=16;rate={RATE}"}), "POST"), "загрузка")
        file_id = (up.get("result") or {}).get("request_file_id")
        if not file_id:
            raise SaluteError("загрузка: сервис не вернул идентификатор файла")
        stop()
        options = {"model": "general", "audio_encoding": "PCM_S16LE", "sample_rate": RATE,
                   "channels_count": 1, "language": LANGS.get(language or "ru", "ru-RU")}
        task = self._json(self._request(
            self.api_url + "/speech:async_recognize",
            json.dumps({"options": options, "request_file_id": file_id}).encode(),
            self._auth({"Content-Type": "application/json"}), "POST"), "задача")
        task_id = (task.get("result") or {}).get("id")
        if not task_id:
            raise SaluteError("задача: сервис не вернул идентификатор")
        deadline = time.time() + max(self.max_wait_sec, len(pcm) / (RATE * 2) * 3 + 300)
        while True:
            stop()
            st = self._json(self._request(
                self.api_url + "/task:get?" + urllib.parse.urlencode({"id": task_id}),
                None, self._auth(), "GET"), "статус")
            res = st.get("result") or {}
            status = res.get("status")
            if status == "DONE":
                out_id = res.get("response_file_id")
                if not out_id:
                    raise SaluteError("статус: нет файла с результатом")
                break
            if status in ("ERROR", "CANCELED"):
                raise SaluteError(f"распознавание не удалось: {res.get('error') or status}")
            if time.time() > deadline:
                raise SaluteError("сервис слишком долго обрабатывает файл")
            time.sleep(self.poll_sec)
        data = self._json(self._request(
            self.api_url + "/data:download?" + urllib.parse.urlencode({"response_file_id": out_id}),
            None, self._auth(), "GET"), "результат")
        return parse_result(data)


class SaluteWithFallback:
    """Движок для transcribe_file: сначала SaluteSpeech, при любой ошибке — локальная модель."""

    def __init__(self, client, local, notify=None, clock=time.time):
        self.client, self.local, self.notify, self.clock = client, local, notify, clock
        self.name = "salutespeech"
        self.fallback_reason = ""
        self._skip_until = 0.0

    # Подготовку и загрузку локальной модели ведёт очередь — запасной вариант всегда готов.
    @property
    def size(self):
        return self.local.size

    @property
    def model(self):
        return self.local.model

    def load(self):
        self.local.load()

    def model_present(self):
        return self.local.model_present()

    def downloaded_mb(self):
        return self.local.downloaded_mb()

    def transcribe(self, samples, language="ru", should_stop=None):
        import numpy as np
        if self.clock() >= self._skip_until:
            try:
                pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
                segments = self.client.transcribe_pcm(pcm, language, should_stop)
                self.name = "salutespeech"
                return segments
            except SaluteError as e:
                reason = str(e)
            except (OSError, ValueError, KeyError, TypeError) as e:
                reason = f"{type(e).__name__}: {e}"
            self._skip_until = self.clock() + COOLDOWN_SEC
            self.fallback_reason = reason
            if self.notify:
                self.notify(reason)
        self.name = "local (запасной)"
        return self.local.transcribe(samples, language, should_stop)
