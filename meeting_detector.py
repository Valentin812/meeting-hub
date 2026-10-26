# -*- coding: utf-8 -*-
"""Meeting Hub v5 — логика обнаружения встреч.

Модуль не зависит ни от Windows, ни от Tkinter: на вход подаются наблюдения
(заголовки окон и список приложений, которые прямо сейчас держат микрофон),
на выходе — переходы конечного автомата с объяснением причины.
Это единственный источник истины о состоянии встречи.

Модель (согласована с пользователем):
  * начало  — окно конференции открыто И это же приложение держит микрофон
              → 5-секундный отсчёт с возможностью отмены;
  * во время встречи выключенный микрофон и тишина ни на что не влияют;
  * конец   — окно конференции закрыто И микрофон свободен
              → вопрос «Встреча закончилась?», завершение через 30 секунд;
  * сомнительные случаи (браузер держит микрофон без известного окна,
    окно комнаты открыто без микрофона) — только вопрос пользователю;
  * ручное управление всегда главнее автоматики.
"""
from dataclasses import dataclass, field
from datetime import datetime
import re

ALGORITHM_VERSION = "5.0"

IDLE = "IDLE"
ASKING = "ASKING"
COUNTDOWN = "COUNTDOWN"
IN_MEETING = "IN_MEETING"
ENDING = "ENDING"

STATE_LABELS = {
    IDLE: "Встречи нет",
    ASKING: "Вопрос: это встреча?",
    COUNTDOWN: "Обнаружена встреча — отсчёт",
    IN_MEETING: "Идёт встреча",
    ENDING: "Вопрос: встреча закончилась?",
}

BROWSERS = {
    "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "brave.exe",
    "browser.exe", "vivaldi.exe", "yandex.exe",
}

# Настольные приложения для встреч: если такое приложение держит микрофон,
# это само по себе сильный признак звонка.
MEETING_APPS = {
    "zoom.exe": "Zoom",
    "ms-teams.exe": "Microsoft Teams",
    "teams.exe": "Microsoft Teams",
    "webex.exe": "Webex",
    "webexhost.exe": "Webex",
    "ciscocollabhost.exe": "Webex",
    "telemost.exe": "Яндекс Телемост",
    "skype.exe": "Skype",
}

# Своё приложение никогда не считается участником встречи.
OWN_PROCESSES = {"python.exe", "pythonw.exe", "meetinghub.exe"}

# strict=True — заголовок сам по себе указывает на комнату встречи,
# поэтому такое окно без микрофона может вызвать вопрос «это встреча?».
DEFAULT_PATTERNS = [
    # «Meet - abc-defg-hij - Google Chrome»: только в начале заголовка, иначе ловит «… | MRM meet - Google Chrome».
    {"pattern": "meet - ", "label": "Google Meet", "strict": True, "start": True},
    {"pattern": "google meet", "label": "Google Meet", "strict": False},
    {"pattern": "телемост", "label": "Яндекс Телемост", "strict": False},
    {"pattern": "telemost", "label": "Яндекс Телемост", "strict": False},
    {"pattern": "zoom meeting", "label": "Zoom", "strict": True},
    {"pattern": "zoom webinar", "label": "Zoom", "strict": True},
    {"pattern": "webex", "label": "Webex", "strict": False},
    {"pattern": "jitsi meet", "label": "Jitsi", "strict": False},
    {"pattern": "microsoft teams", "label": "Microsoft Teams", "strict": False},
]

COUNTDOWN_SEC = 5        # отсчёт перед режимом встречи
END_CONFIRM_SEC = 30     # сколько ждать ответа на «встреча закончилась?»
END_GRACE_SEC = 10       # сколько сигналы должны отсутствовать, прежде чем спросить
STRONG_STABLE_SEC = 2    # сильный сигнал должен держаться, чтобы не ловить мигание
WEAK_STABLE_SEC = 4      # браузер держит микрофон без известного окна
WINDOW_ONLY_SEC = 20     # окно комнаты открыто, микрофон выключен
ASK_TIMEOUT_SEC = 60     # вопрос «это встреча?» без ответа снимается

_DASHES = re.compile(r"[‒–—―−]")
_SPACES = re.compile(r"\s+")
_BROWSER_SUFFIX = re.compile(
    r"\s+-\s+(google chrome|microsoft​? edge|mozilla firefox|"
    r"яндекс браузер|yandex browser|opera|brave|vivaldi)\s*$",
    re.IGNORECASE,
)


def normalize(text):
    """Нижний регистр, единый вид тире и пробелов — для сравнения заголовков."""
    text = _DASHES.sub("-", str(text or ""))
    text = _SPACES.sub(" ", text).casefold()
    return text


def strip_browser_suffix(title):
    return _BROWSER_SUFFIX.sub("", _DASHES.sub("-", str(title or ""))).strip()


def suggest_pattern(title):
    """Предложить шаблон по заголовку окна: убрать имя браузера и код комнаты.

    «Meet – abc-defg-hij - Google Chrome» → «Meet - ».
    Пользователь всё равно видит и правит шаблон перед сохранением.
    """
    base = strip_browser_suffix(title)
    match = re.match(r"^(.{3,}?)\s+[-|:]\s+", base)
    if match:
        sep = re.search(r"\s+([-|:])\s+", base[len(match.group(1)):]).group(1)
        return match.group(1).strip() + " " + sep + " "
    return base


def app_matches(process, mic_id):
    """Сопоставить имя процесса окна и идентификатор из реестра микрофона.

    Для обычных программ в реестре лежит путь к exe (берём имя файла),
    для приложений из Store — имя пакета (например MSTeams_8wekyb3d8bbwe).
    """
    p = (process or "").lower()
    m = (mic_id or "").lower()
    if not p or not m:
        return False
    if p == m:
        return True
    stem = p[:-4] if p.endswith(".exe") else p
    stem = stem.replace("ms-", "")
    return len(stem) >= 4 and stem in m


@dataclass
class Observation:
    """Одно наблюдение за системой."""
    now: float
    windows: list = field(default_factory=list)  # [(title, process_name)]
    mic: object = None   # set(идентификаторов) | None, если проверить не удалось
    mic_error: str = ""


@dataclass
class Candidate:
    kind: str            # window+mic | app+mic | browser_mic | window_only | manual
    app: str = ""
    title: str = ""
    label: str = ""

    def describe(self):
        place = self.label or self.app or "неизвестное приложение"
        if self.kind == "window+mic":
            return f"{place}: окно встречи открыто и приложение использует микрофон"
        if self.kind == "app+mic":
            return f"{place}: приложение для встреч использует микрофон"
        if self.kind == "browser_mic":
            return f"{place}: браузер использует микрофон, но окно встречи не распознано"
        if self.kind == "window_only":
            return f"{place}: окно встречи открыто, микрофон не используется"
        return "ручное управление"

    def key(self):
        if self.kind == "window_only":
            return ("win", normalize(self.title))
        return ("mic", (self.app or "").lower())


@dataclass
class Decision:
    """Переход автомата — то, что попадает в журнал объяснений."""
    time: str
    prev: str
    new: str
    event: str
    reason: str
    initiator: str          # auto | user
    app: str = ""
    title: str = ""
    label: str = ""
    kind: str = ""
    algorithm: str = ALGORITHM_VERSION

    def as_dict(self):
        return dict(self.__dict__)


class MeetingDetector:
    def __init__(self, patterns=None, ignore_patterns=None, clock_label=None):
        self.state = IDLE
        self.set_patterns(patterns or [], ignore_patterns or [])
        self._clock_label = clock_label or (lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self.candidate = None          # кандидат для отсчёта / вопроса / текущей встречи
        self.countdown_until = None
        self.ask_until = None
        self.ending_until = None
        self.strong_since = None
        self.strong_key = None
        self.weak_since = {}
        self.asked = set()             # о чём уже спрашивали, пока сигнал не исчезнет
        self.suppress_auto = False     # после отмены/ручного завершения ждём, пока сигналы исчезнут
        self.seen_active = False       # во время встречи хотя бы раз видели сигналы
        self.inactive_since = None
        self.last_active_at = None     # последний момент, когда сигналы встречи были видны
        self.last_mic_error = ""
        self.last_signals = {"strong": None, "weak": [], "hits": [], "mic": None}

    # ---------- шаблоны ----------
    def set_patterns(self, patterns, ignore_patterns=()):
        merged = []
        seen = set()
        for item in list(DEFAULT_PATTERNS) + list(patterns):
            norm = normalize(item.get("pattern", ""))
            if len(norm.strip()) < 3 or norm in seen:
                continue
            seen.add(norm)
            merged.append({"pattern": norm, "label": item.get("label") or item.get("pattern", "").strip(),
                           "strict": bool(item.get("strict", True)), "start": bool(item.get("start", False))})
        self.patterns = merged
        self.ignore = [normalize(p.get("pattern", "") if isinstance(p, dict) else p)
                       for p in ignore_patterns]
        self.ignore = [p for p in self.ignore if len(p.strip()) >= 3]

    def match_title(self, title):
        low = normalize(title)
        if not low or any(p in low for p in self.ignore):
            return None
        # Самый длинный (самый конкретный) подходящий шаблон: выученный «… | mrm meet» важнее общего «meet - ».
        found = [item for item in self.patterns
                 if (low.lstrip().startswith(item["pattern"]) if item.get("start") else item["pattern"] in low)]
        return max(found, key=lambda item: len(item["pattern"]), default=None)

    # ---------- сигналы ----------
    def evaluate(self, obs):
        hits = []
        for title, proc in obs.windows:
            proc = (proc or "").lower()
            if proc in OWN_PROCESSES:
                continue
            item = self.match_title(title)
            if item:
                hits.append((title, proc, item))

        mic = None
        if obs.mic is not None:
            mic = {m.lower() for m in obs.mic if m and not any(app_matches(o, m) for o in OWN_PROCESSES)}

        strong = None
        weak = []
        if mic is not None:
            for title, proc, item in hits:
                if any(app_matches(proc, m) for m in mic):
                    strong = Candidate("window+mic", proc, title, item["label"])
                    break
            if strong is None:
                for m in sorted(mic):
                    for proc, label in MEETING_APPS.items():
                        if app_matches(proc, m):
                            strong = Candidate("app+mic", proc, "", label)
                            break
                    if strong:
                        break
            if strong is None:
                for m in sorted(mic):
                    if m in BROWSERS:
                        weak.append(Candidate("browser_mic", m, self._browser_title(obs, m), m))
        if strong is None:
            for title, proc, item in hits:
                if item["strict"] and proc in BROWSERS:
                    weak.append(Candidate("window_only", proc, title, item["label"]))
        return strong, weak, hits, mic

    def _browser_title(self, obs, browser):
        for title, proc in obs.windows:
            if (proc or "").lower() == browser and title.strip():
                return title
        return ""

    def _is_active(self, hits, mic):
        if mic is None:
            return True       # не знаем состояние микрофона — встречу не завершаем
        if hits:
            return True
        app = self.candidate.app if self.candidate else ""
        if app:
            return any(app_matches(app, m) for m in mic)
        return any(m in BROWSERS or any(app_matches(p, m) for p in MEETING_APPS) for m in mic)

    # ---------- переходы ----------
    def _go(self, new, event, reason, initiator="auto", cand=None):
        cand = cand or self.candidate
        d = Decision(time=self._clock_label(), prev=self.state, new=new, event=event, reason=reason,
                     initiator=initiator, app=(cand.app if cand else ""),
                     title=(cand.title[:160] if cand else ""), label=(cand.label if cand else ""),
                     kind=(cand.kind if cand else ""))
        self.state = new
        if new in (IDLE,):
            self.countdown_until = self.ask_until = self.ending_until = None
            self.inactive_since = None
            self.candidate = None
        return d

    def update(self, obs):
        """Обработать наблюдение. Возвращает список переходов (обычно пустой)."""
        now = obs.now
        out = []
        strong, weak, hits, mic = self.evaluate(obs)
        self.last_signals = {"strong": strong, "weak": weak, "hits": hits, "mic": mic}
        if obs.mic_error and obs.mic_error != self.last_mic_error:
            out.append(Decision(self._clock_label(), self.state, self.state, "Ошибка источника",
                                "Сигнал Windows недоступен: " + obs.mic_error, "auto"))
        self.last_mic_error = obs.mic_error

        # Подавление после отмены / ручного завершения действует, пока сигналы не исчезнут.
        if self.suppress_auto and strong is None and not weak:
            self.suppress_auto = False
        current_keys = {c.key() for c in weak} | ({strong.key()} if strong else set())
        self.asked &= current_keys
        self.weak_since = {k: v for k, v in self.weak_since.items() if k in current_keys}

        if strong is not None:
            if self.strong_key != strong.key():
                self.strong_key = strong.key()
                self.strong_since = now
        else:
            self.strong_key = None
            self.strong_since = None
        for c in weak:
            self.weak_since.setdefault(c.key(), now)

        if self.state == IDLE:
            if self.suppress_auto:
                return out
            if strong is not None and now - self.strong_since >= STRONG_STABLE_SEC:
                self.candidate = strong
                self.countdown_until = now + COUNTDOWN_SEC
                out.append(self._go(COUNTDOWN, "Кандидат на встречу", strong.describe(), cand=strong))
                return out
            for c in weak:
                need = WINDOW_ONLY_SEC if c.kind == "window_only" else WEAK_STABLE_SEC
                if c.key() in self.asked or now - self.weak_since[c.key()] < need:
                    continue
                self.asked.add(c.key())
                self.candidate = c
                self.ask_until = now + ASK_TIMEOUT_SEC
                out.append(self._go(ASKING, "Нужен ответ пользователя", c.describe(), cand=c))
                break

        elif self.state == COUNTDOWN:
            if self.candidate and self.candidate.kind in ("window+mic", "app+mic") and \
                    (strong is None or strong.key() != self.candidate.key()):
                out.append(self._go(IDLE, "Отсчёт прерван", "Сигнал встречи пропал до конца отсчёта"))
            elif now >= self.countdown_until:
                out.append(self._enter_meeting("Встреча началась",
                                               f"Отсчёт {COUNTDOWN_SEC} с завершён без отмены. "
                                               + self.candidate.describe()))

        elif self.state == ASKING:
            if strong is not None:
                self.candidate = strong
                self.countdown_until = now + COUNTDOWN_SEC
                out.append(self._go(COUNTDOWN, "Кандидат на встречу", strong.describe(), cand=strong))
            elif self.candidate is None or self.candidate.key() not in current_keys:
                out.append(self._go(IDLE, "Вопрос снят", "Сигнал пропал до ответа пользователя"))
            elif now >= self.ask_until:
                out.append(self._go(IDLE, "Вопрос снят", f"Нет ответа {ASK_TIMEOUT_SEC} с"))

        elif self.state == IN_MEETING:
            if self._is_active(hits, mic):
                self.seen_active = True
                self.inactive_since = None
                self.last_active_at = now
            elif self.seen_active:
                if self.inactive_since is None:
                    self.inactive_since = now
                elif now - self.inactive_since >= END_GRACE_SEC:
                    self.ending_until = now + END_CONFIRM_SEC
                    out.append(self._go(ENDING, "Похоже, встреча закончилась",
                                        f"Окно встречи закрыто и микрофон свободен {END_GRACE_SEC} с"))

        elif self.state == ENDING:
            if self._is_active(hits, mic):
                self.last_active_at = now
                self.ending_until = None
                self.inactive_since = None
                out.append(self._go(IN_MEETING, "Встреча продолжается",
                                    "Окно встречи или микрофон снова активны"))
            elif now >= self.ending_until:
                out.append(self._go(IDLE, "Встреча завершена",
                                    f"Нет ответа {END_CONFIRM_SEC} с, сигналы встречи отсутствуют"))
        return out

    def _enter_meeting(self, event, reason, initiator="auto"):
        # Если встреча началась по сигналу, он уже наблюдался: его исчезновение — повод спросить о конце.
        # После ручного старта без сигналов ждём, пока сигналы появятся, иначе встреча сразу «закончится».
        self.seen_active = self.candidate is not None and self.candidate.kind != "manual"
        self.last_active_at = None
        self.inactive_since = None
        self.countdown_until = self.ask_until = self.ending_until = None
        return self._go(IN_MEETING, event, reason, initiator)

    # ---------- действия пользователя ----------
    def manual_start(self, now=None):
        if self.state in (IN_MEETING, ENDING):
            return None
        if self.candidate is None:
            self.candidate = Candidate("manual")
        return self._enter_meeting("Встреча начата вручную", "Пользователь нажал «Начать встречу»", "user")

    def manual_end(self, now=None):
        if self.state == IDLE:
            return None
        self.suppress_auto = True
        return self._go(IDLE, "Встреча завершена вручную", "Пользователь нажал «Завершить встречу»", "user")

    def cancel_countdown(self, now=None):
        if self.state != COUNTDOWN:
            return None
        self.suppress_auto = True
        return self._go(IDLE, "Отсчёт отменён", "Пользователь нажал «Отмена»", "user")

    def start_now(self, now=None):
        if self.state != COUNTDOWN:
            return None
        return self._enter_meeting("Встреча началась", "Пользователь нажал «Начать сейчас»", "user")

    def answer_ask(self, is_meeting, now=None):
        if self.state != ASKING:
            return None
        if is_meeting:
            return self._enter_meeting("Встреча начата", "Пользователь подтвердил: это встреча", "user")
        return self._go(IDLE, "Не встреча", "Пользователь ответил: это не встреча", "user")

    def keep_meeting(self, now=None):
        if self.state != ENDING:
            return None
        d = self._go(IN_MEETING, "Встреча продолжается", "Пользователь нажал «Нет, идёт»", "user")
        # Не спрашиваем снова, пока сигналы встречи не появятся и не пропадут ещё раз.
        self.seen_active = False
        self.inactive_since = None
        self.ending_until = None
        return d

    def end_now(self, now=None):
        if self.state != ENDING:
            return None
        return self._go(IDLE, "Встреча завершена", "Пользователь подтвердил завершение", "user")

    # ---------- для интерфейса ----------
    @property
    def in_meeting(self):
        return self.state in (IN_MEETING, ENDING)

    @property
    def blocks_analysis(self):
        return self.state in (COUNTDOWN, IN_MEETING, ENDING)

    def seconds_left(self, now):
        target = {COUNTDOWN: self.countdown_until, ENDING: self.ending_until,
                  ASKING: self.ask_until}.get(self.state)
        return None if target is None else max(0, int(target - now + 0.999))
