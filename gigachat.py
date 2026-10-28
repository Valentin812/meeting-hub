# -*- coding: utf-8 -*-
"""Итоги и чат через GigaChat (Сбер) с автоматическим переходом на локальную модель (Ollama).

Программа не хранит ключ Сбера: она ходит на сервер-посредник (server/giga-proxy) со своим кодом доступа.

Текст встречи уходит в облако Сбера только если пользователь сам включил это в настройках.
Любая ошибка облака (нет сети, ключ не принят, лимит токенов) — работа продолжается локальной моделью.
"""
import json
import threading
import time
import urllib.error
import urllib.request

import analyzer
import chat
import privacy

PROXY_URL = "https://swprtuowqpuwgthigweq.supabase.co/functions/v1/giga-proxy"
MODELS = ("GigaChat-2-Pro", "GigaChat-2", "GigaChat-2-Max")
DEFAULT_MODEL = MODELS[0]
COOLDOWN_SEC = 600
RETRY_DELAYS = (3, 8, 20)           # секунды: при «слишком много запросов» ждём и повторяем, а не сдаёмся
_QUEUE = threading.Lock()           # одна генерация за раз: у бесплатного тарифа один поток


class GigaError(Exception):
    pass


def _busy(error):
    text = str(error).lower()
    return "429" in text or "too many" in text or "503" in text or "слишком много" in text


class ProxyClient:
    """GigaChat через сервер-посредник: ключ Сбера хранится на сервере, в программе только код доступа."""

    def __init__(self, code, url=PROXY_URL, timeout=180, retry_delays=None):
        self.code = (code or "").strip()
        self.url = (url or PROXY_URL).rstrip("/")
        self.timeout = timeout
        self.retry_delays = RETRY_DELAYS if retry_delays is None else retry_delays

    def _request(self, data=None):
        if not self.code:
            raise GigaError("не указан код доступа")
        req = urllib.request.Request(self.url, data=data, method="POST" if data is not None else "GET", headers={
            "x-hub-token": self.code, "Content-Type": "application/json", "User-Agent": "MeetingHub"})
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            try:
                body = json.loads(body).get("error") or body
            except (ValueError, AttributeError):
                pass
            raise GigaError(f"сервис ответил {e.code}: {body}")
        except OSError as e:
            raise GigaError(f"нет связи с сервисом: {getattr(e, 'reason', e)}")

    def check(self):
        with self._request() as r:
            data = json.loads(r.read().decode("utf-8"))
        if not data.get("ok"):
            raise GigaError(data.get("error") or "сервис не готов")
        return f"Подключение работает: код принят ({data.get('user', '')})."

    def _queued(self, call, cancel_event=None):
        """Запросы идут по очереди; если сервис занят — ждём и повторяем несколько раз."""
        with _QUEUE:
            for delay in (*self.retry_delays, None):
                try:
                    return call()
                except GigaError as e:
                    if delay is None or not _busy(e):
                        raise
                    end = time.time() + delay
                    while time.time() < end:
                        if cancel_event is not None and cancel_event.is_set():
                            raise
                        time.sleep(0.2)

    @staticmethod
    def _payload(messages, model, stream, temperature):
        return json.dumps({"model": model or DEFAULT_MODEL, "messages": messages, "stream": stream,
                           "temperature": temperature}, ensure_ascii=False).encode("utf-8")

    def complete(self, messages, model=DEFAULT_MODEL, temperature=0.1, cancel_event=None):
        return self._queued(lambda: self._complete(messages, model, temperature), cancel_event)

    def _complete(self, messages, model, temperature):
        with self._request(self._payload(messages, model, False, temperature)) as r:
            raw = r.read().decode("utf-8")
        try:
            return json.loads(raw)["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise GigaError("GigaChat: неожиданный ответ сервиса")

    def stream(self, messages, model, on_text, cancel_event=None, temperature=0.3):
        return self._queued(lambda: self._stream(messages, model, on_text, cancel_event, temperature), cancel_event)

    def _stream(self, messages, model, on_text, cancel_event, temperature):
        """Ответ по кусочкам (SSE). Возвращает полный текст."""
        out = []
        resp = self._request(self._payload(messages, model, True, temperature))
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

    def __init__(self, local_model, client=None, giga_model=DEFAULT_MODEL, notify=None, clock=time.time,
                 mask=True, log=None):
        self.local_model, self.client, self.giga_model = local_model, client, giga_model
        self.notify, self.clock = notify, clock
        self.mask, self.log = mask, log          # log(meeting, what, chars, masked, ok, error) — журнал отправок
        self.name = "ollama"
        self.fallback_reason = ""
        self._skip_until = 0.0

    def _cloud_ready(self, local_only=False):
        return not local_only and self.client is not None and self.clock() >= self._skip_until

    def _log(self, meeting, what, chars, masked, ok, error=""):
        if self.log:
            try:
                self.log(meeting, what, chars, masked, ok, error)
            except Exception:
                pass

    def _failed(self, reason):
        self._skip_until = self.clock() + COOLDOWN_SEC
        self.fallback_reason = reason
        if self.notify:
            self.notify(reason)

    def chat_json(self, system, user, cancel_event=None, meeting=None, local_only=False):
        """Для итогов: словарь из ответа модели. local_only — закрытая встреча: только локальная модель."""
        if self._cloud_ready(local_only):
            masker = privacy.Masker() if self.mask else None
            sent = masker.mask(user) if masker else user
            ok, error = True, ""
            try:
                text = self.client.complete([{"role": "system", "content": system},
                                             {"role": "user", "content": sent}], self.giga_model,
                                            cancel_event=cancel_event)
                self.name = "gigachat"
                result = analyzer.parse_json(text)
                return masker.unmask_obj(result) if masker else result
            except analyzer.Cancelled:
                ok = False
                raise
            except (GigaError, ValueError, OSError, KeyError, TypeError) as e:
                ok, error = False, str(e)
                self._failed(error)
            finally:
                self._log(meeting, "итоги", len(system) + len(sent), masker.count if masker else 0, ok, error)
        self.name = "ollama (запасной)" if self.client is not None and not local_only else "ollama"
        return analyzer.ollama_chat(self.local_model, system, user, cancel_event)

    def stream(self, messages, on_text, cancel_event=None, meeting=None, local_only=False):
        """Для чата: ответ по кусочкам. local_only — закрытая встреча: только локальная модель."""
        if self._cloud_ready(local_only):
            got = []
            masker = privacy.Masker() if self.mask else None
            sent = masker.mask_messages(messages) if masker else messages
            un = privacy.StreamUnmasker(masker) if masker else None

            def piece(t):
                shown = un.feed(t) if un else t
                if shown:
                    got.append(shown)
                    on_text(shown)
            ok, error = True, ""
            try:
                self.client.stream(sent, self.giga_model, piece, cancel_event)
                tail = un.flush() if un else ""
                if tail:
                    got.append(tail)
                    on_text(tail)
                self.name = "gigachat"
                return "".join(got)
            except (GigaError, OSError) as e:
                ok, error = False, str(e)
                self._failed(error)
                if got:                                   # часть ответа уже показана — не дублируем
                    return "".join(got)
            finally:
                self._log(meeting, "чат", sum(len(m.get("content", "")) for m in sent),
                          masker.count if masker else 0, ok, error)
        self.name = "ollama (запасной)" if self.client is not None and not local_only else "ollama"
        return chat.stream_answer(self.local_model, messages, on_text, cancel_event)
