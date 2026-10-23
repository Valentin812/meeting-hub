# -*- coding: utf-8 -*-
"""Сценарии 1–10 из ТЗ на логике детектора (без Windows и реального звука).

Наблюдения подаются с искусственным временем: это проверка логики переходов,
а не доказательство работы на компьютере пользователя.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting_detector import (MeetingDetector, Observation, IDLE, ASKING, COUNTDOWN, IN_MEETING, ENDING,
                              suggest_pattern, app_matches, COUNTDOWN_SEC, END_CONFIRM_SEC, END_GRACE_SEC)

MEET = ("Meet – abc-defg-hij - Google Chrome", "chrome.exe")
YOUTUBE = ("Музыка — YouTube - Google Chrome", "chrome.exe")
EXPLORER = ("Загрузки", "explorer.exe")


class Sim:
    """Подаёт наблюдения раз в секунду и собирает переходы."""

    def __init__(self, detector=None):
        self.d = detector or MeetingDetector(clock_label=lambda: "t")
        self.t = 0.0
        self.decisions = []

    def run(self, seconds, windows=(), mic=frozenset(), mic_error=""):
        for _ in range(int(seconds)):
            self.t += 1
            obs = Observation(now=self.t, windows=list(windows),
                              mic=None if mic is None else set(mic), mic_error=mic_error)
            self.decisions += self.d.update(obs)
        return self.d.state

    def transitions(self):
        return [(x.prev, x.new) for x in self.decisions if x.prev != x.new]

    def act(self, method, *args):
        d = getattr(self.d, method)(*args)
        if d:
            self.decisions.append(d)
        return d


class Scenario1Startup(unittest.TestCase):
    def test_start_without_meeting_stays_idle(self):
        s = Sim()
        self.assertEqual(s.run(30, windows=[YOUTUBE, EXPLORER], mic=set()), IDLE)
        self.assertEqual(s.transitions(), [])

    def test_unknown_mic_never_starts_meeting(self):
        s = Sim()
        s.run(30, windows=[MEET], mic=None, mic_error="нет доступа")
        # без данных о микрофоне допустим только вопрос пользователю, но не автозапуск
        self.assertNotIn(COUNTDOWN, [n for _, n in s.transitions()])
        self.assertNotIn(IN_MEETING, [n for _, n in s.transitions()])
        self.assertEqual(sum(1 for x in s.decisions if x.event == "Ошибка источника"), 1)

    def test_unknown_mic_without_meeting_window_stays_idle(self):
        s = Sim()
        self.assertEqual(s.run(30, windows=[YOUTUBE], mic=None, mic_error="нет доступа"), IDLE)


class Scenario2ChromeWithoutMeeting(unittest.TestCase):
    def test_open_chrome_is_not_a_meeting(self):
        s = Sim()
        self.assertEqual(s.run(60, windows=[YOUTUBE], mic=set()), IDLE)

    def test_chrome_mic_on_unknown_site_only_asks_once(self):
        s = Sim()
        s.run(10, windows=[YOUTUBE], mic={"chrome.exe"})
        self.assertEqual(s.d.state, ASKING)
        self.assertNotIn(COUNTDOWN, [n for _, n in s.transitions()])
        s.act("answer_ask", False)
        self.assertEqual(s.run(120, windows=[YOUTUBE], mic={"chrome.exe"}), IDLE)
        self.assertEqual(sum(1 for _, n in s.transitions() if n == ASKING), 1)
        # микрофон отпущен и взят снова — новый вопрос допустим
        s.run(3, windows=[YOUTUBE], mic=set())
        self.assertEqual(s.run(10, windows=[YOUTUBE], mic={"chrome.exe"}), ASKING)

    def test_question_without_answer_expires(self):
        s = Sim()
        s.run(10, windows=[YOUTUBE], mic={"chrome.exe"})
        self.assertEqual(s.run(70, windows=[YOUTUBE], mic={"chrome.exe"}), IDLE)


class Scenario3MeetingStart(unittest.TestCase):
    def test_window_and_mic_start_countdown_then_meeting(self):
        s = Sim()
        s.run(3, windows=[MEET], mic={"chrome.exe"})
        self.assertEqual(s.d.state, COUNTDOWN)
        s.run(COUNTDOWN_SEC, windows=[MEET], mic={"chrome.exe"})
        self.assertEqual(s.d.state, IN_MEETING)
        self.assertEqual(s.transitions(), [(IDLE, COUNTDOWN), (COUNTDOWN, IN_MEETING)])
        self.assertIn("Google Meet", s.decisions[-1].label)

    def test_cancel_prevents_meeting_until_signals_go_away(self):
        s = Sim()
        s.run(3, windows=[MEET], mic={"chrome.exe"})
        s.act("cancel_countdown")
        self.assertEqual(s.run(60, windows=[MEET], mic={"chrome.exe"}), IDLE)
        s.run(3, windows=[YOUTUBE], mic=set())
        self.assertEqual(s.run(3, windows=[MEET], mic={"chrome.exe"}), COUNTDOWN)

    def test_window_without_mic_asks_instead_of_starting(self):
        s = Sim()
        self.assertEqual(s.run(25, windows=[MEET], mic=set()), ASKING)
        s.act("answer_ask", True)
        self.assertEqual(s.d.state, IN_MEETING)

    def test_desktop_teams_from_store_counts_as_meeting(self):
        s = Sim()
        s.run(3, windows=[("Чат | Microsoft Teams", "ms-teams.exe")], mic={"msteams_8wekyb3d8bbwe"})
        self.assertEqual(s.d.state, COUNTDOWN)

    def test_own_process_using_mic_is_ignored(self):
        s = Sim()
        self.assertEqual(s.run(30, windows=[MEET], mic={"python.exe", "pythonw.exe"}), ASKING)
        self.assertNotIn(COUNTDOWN, [n for _, n in s.transitions()])


def in_meeting():
    s = Sim()
    s.run(3, windows=[MEET], mic={"chrome.exe"})
    s.run(COUNTDOWN_SEC, windows=[MEET], mic={"chrome.exe"})
    assert s.d.state == IN_MEETING
    return s


class Scenario4MicOff(unittest.TestCase):
    def test_muted_mic_keeps_meeting(self):
        s = in_meeting()
        self.assertEqual(s.run(600, windows=[MEET], mic=set()), IN_MEETING)


class Scenario5Silence(unittest.TestCase):
    def test_long_silence_with_open_mic_keeps_meeting(self):
        s = in_meeting()
        self.assertEqual(s.run(1800, windows=[MEET], mic={"chrome.exe"}), IN_MEETING)


class Scenario6End(unittest.TestCase):
    def test_leaving_meeting_asks_then_ends_after_30s(self):
        s = in_meeting()
        s.run(END_GRACE_SEC + 1, windows=[YOUTUBE], mic=set())
        self.assertEqual(s.d.state, ENDING)
        self.assertEqual(s.d.seconds_left(s.t), END_CONFIRM_SEC)
        s.run(END_CONFIRM_SEC, windows=[YOUTUBE], mic=set())
        self.assertEqual(s.d.state, IDLE)

    def test_short_gap_does_not_end(self):
        s = in_meeting()
        s.run(END_GRACE_SEC - 2, windows=[YOUTUBE], mic=set())
        self.assertEqual(s.run(30, windows=[MEET], mic=set()), IN_MEETING)

    def test_signals_back_during_question_continue_meeting(self):
        s = in_meeting()
        s.run(END_GRACE_SEC + 1, windows=[], mic=set())
        self.assertEqual(s.run(5, windows=[MEET], mic=set()), IN_MEETING)

    def test_keep_meeting_answer_is_respected(self):
        s = in_meeting()
        s.run(END_GRACE_SEC + 1, windows=[], mic=set())
        s.act("keep_meeting")
        self.assertEqual(s.run(300, windows=[], mic=set()), IN_MEETING)
        s.run(5, windows=[MEET], mic={"chrome.exe"})
        self.assertEqual(s.run(END_GRACE_SEC + 1, windows=[], mic=set()), ENDING)

    def test_manual_end_wins_and_does_not_restart(self):
        s = in_meeting()
        s.act("manual_end")
        self.assertEqual(s.run(60, windows=[MEET], mic={"chrome.exe"}), IDLE)


class Scenario7FalseEvent(unittest.TestCase):
    def test_ignore_pattern_stops_false_start(self):
        title = ("Google Meet - проверка микрофона - Google Chrome", "chrome.exe")
        s = Sim()
        self.assertEqual(s.run(3, windows=[title], mic={"chrome.exe"}), COUNTDOWN)
        s.act("cancel_countdown")
        s.d.set_patterns([], ["Google Meet - проверка микрофона"])
        s.run(3, windows=[], mic=set())
        s.run(30, windows=[title], mic={"chrome.exe"})
        self.assertNotIn(COUNTDOWN, [n for _, n in s.transitions()[1:]])


class Scenario8Learning(unittest.TestCase):
    def test_learned_pattern_turns_question_into_countdown(self):
        room = ("Видеовстреча - Комната 42 - Google Chrome", "chrome.exe")
        s = Sim()
        self.assertEqual(s.run(6, windows=[room], mic={"chrome.exe"}), ASKING)
        pattern = suggest_pattern(room[0])
        self.assertEqual(pattern, "Видеовстреча - ")
        s.act("answer_ask", True)
        s.act("manual_end")
        learned = MeetingDetector([{"pattern": pattern, "label": "Видеовстреча"}], clock_label=lambda: "t")
        s2 = Sim(learned)
        self.assertEqual(s2.run(3, windows=[room], mic={"chrome.exe"}), COUNTDOWN)


class Scenario9Duplicates(unittest.TestCase):
    def test_repeated_and_double_signals_give_one_transition(self):
        s = Sim()
        two_windows = [MEET, ("Meet – abc-defg-hij - Google Chrome", "chrome.exe")]
        s.run(120, windows=two_windows, mic={"chrome.exe"})
        self.assertEqual(s.transitions(), [(IDLE, COUNTDOWN), (COUNTDOWN, IN_MEETING)])


class Scenario10SourceFailure(unittest.TestCase):
    def test_mic_failure_is_logged_once_and_does_not_end_meeting(self):
        s = in_meeting()
        s.run(120, windows=[], mic=None, mic_error="реестр недоступен")
        self.assertEqual(s.d.state, IN_MEETING)
        errors = [x for x in s.decisions if x.event == "Ошибка источника"]
        self.assertEqual(len(errors), 1)
        s.run(3, windows=[MEET], mic={"chrome.exe"})
        self.assertEqual(s.d.state, IN_MEETING)


class ManualControl(unittest.TestCase):
    def test_manual_start_without_signals_is_not_auto_ended(self):
        s = Sim()
        s.act("manual_start")
        self.assertEqual(s.run(600, windows=[], mic=set()), IN_MEETING)

    def test_manual_start_then_signals_vanish_asks(self):
        s = Sim()
        s.act("manual_start")
        s.run(5, windows=[MEET], mic={"chrome.exe"})
        self.assertEqual(s.run(END_GRACE_SEC + 1, windows=[], mic=set()), ENDING)


class Helpers(unittest.TestCase):
    def test_app_matches(self):
        self.assertTrue(app_matches("chrome.exe", "chrome.exe"))
        self.assertTrue(app_matches("ms-teams.exe", "msteams_8wekyb3d8bbwe"))
        self.assertFalse(app_matches("chrome.exe", "msedge.exe"))

    def test_suggest_pattern(self):
        self.assertEqual(suggest_pattern("Meet – abc-defg-hij - Google Chrome"), "Meet - ")
        self.assertEqual(suggest_pattern("Телемост - Google Chrome"), "Телемост")


if __name__ == "__main__":
    unittest.main()
