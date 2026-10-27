# -*- coding: utf-8 -*-
"""Итоги и чат через GigaChat (Сбер) с автоматическим переходом на локальную модель (Ollama).

Текст встречи уходит в облако Сбера только если пользователь сам включил это в настройках.
Любая ошибка облака (нет сети, ключ не принят, лимит токенов) — работа продолжается локальной моделью.
"""
import json
import time

import analyzer
import chat
from salute import SaluteClient, SaluteError

API_URL = "https://gigachat.devices.sberbank.ru/api/v1"
SCOPES = ("GIGACHAT_API_PERS", "GIGACHAT_API_B2B", "GIGACHAT_API_CORP")
MODELS = ("GigaChat-2-Pro", "GigaChat-2", "GigaChat-2-Max")
DEFAULT_MODEL = MODELS[0]
COOLDOWN_SEC = 600


class GigaClient(SaluteClient):
    def __init__(self, auth_key, scope="GIGACHAT_API_PERS", verify_tls=True, timeout=120, **kw):
        kw.setdefault("api_url", API_URL)
        super().__init__(auth_key, scope or SCOPES[0], verify_tls, timeout=timeout, **kw)

    def check(self):
        self._token = ""
        self.token()
        return "Подключение работает: ключ принят."

    def _payload(self, messages, model, stream, temperature):
        return json.dumps({"model": model or DEFAULT_MODEL, "messages": messages, "stream": stream,
                           "temperature": temperature}, ensure_ascii=False).encode("utf-8")

    def complete(self, messages, model=DEFAULT_MODEL, temperature=0.1):
        raw = self._request(self.api_url + "/chat/completions", self._payload(messages, model, False, temperature),
                            self._auth({"Content-Type": "application/json", "Accept": "application/json"}), "POST")
        data = self._json(raw, "ответ GigaChat")
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise SaluteError("GigaChat: неожиданный ответ сервиса")

    def stream(self, messages, model, on_text, cancel_event=None, temperature=0.3):
        """Ответ по кусочкам (SSE). Возвращает полный текст."""
        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            self.api_url + "/chat/completions", data=self._payload(messages, model, True, temperature),
            headers=self._auth({"Content-Type": "application/json", "Accept": "text/event-stream"}), method="POST")
        out = []
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx)
        except urllib.error.HTTPError as e:
            raise SaluteError(f"сервис ответил {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
        except OSError as e:
            raise SaluteError(f"нет связи с сервисом: {e}")
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    break
                line = resp.readline()
                if not line:
                    break
                line = line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                try:
                    piece = json.loads(body)["choices"][0].get("delta", {}).get("content") or ""
                except (ValueError, KeyError, IndexError, TypeError):
                    continue
                if piece:
                    out.append(piece)
                    on_text(piece)
        finally:
            resp.close()
        return "".join(out)


class LlmRouter:
    """Куда отправлять запросы: сначала GigaChat (если включён), при ошибке — локальная Ollama."""

    def __init__(self, local_model, client=None, giga_model=DEFAULT_MODEL, notify=None, clock=time.time):
        self.local_model, self.client, self.giga_model = local_model, client, giga_model
        self.notify, self.clock = notify, clock
        self.name = "ollama"
        self.fallback_reason = ""
        self._skip_until = 0.0

    def _cloud_ready(self):
        return self.client is not None and self.clock() >= self._skip_until

    def _failed(self, reason):
        self._skip_until = self.clock() + COOLDOWN_SEC
        self.fallback_reason = reason
        if self.notify:
            self.notify(reason)

    def chat_json(self, system, user, cancel_event=None):
        """Для итогов: словарь из ответа модели."""
        if self._cloud_ready():
            try:
                text = self.client.complete([{"role": "system", "content": system},
                                             {"role": "user", "content": user}], self.giga_model)
                self.name = "gigachat"
                return analyzer.parse_json(text)
            except analyzer.Cancelled:
                raise
            except (SaluteError, ValueError, OSError, KeyError, TypeError) as e:
                self._failed(str(e))
        self.name = "ollama (запасной)" if self.client is not None else "ollama"
        return analyzer.ollama_chat(self.local_model, system, user, cancel_event)

    def stream(self, messages, on_text, cancel_event=None):
        """Для чата: ответ по кусочкам."""
        if self._cloud_ready():
            got = []
            try:
                text = self.client.stream(messages, self.giga_model, lambda t: (got.append(t), on_text(t)),
                                          cancel_event)
                self.name = "gigachat"
                return text
            except (SaluteError, OSError) as e:
                self._failed(str(e))
                if got:                                   # часть ответа уже показана — не дублируем
                    return "".join(got)
        self.name = "ollama (запасной)" if self.client is not None else "ollama"
        return chat.stream_answer(self.local_model, messages, on_text, cancel_event)
