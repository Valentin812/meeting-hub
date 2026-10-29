import json
import shutil
import sys
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import transcriber as tr  # noqa: E402
import voices  # noqa: E402

RATE = 16000
FREQS = {"A": 300.0, "B": 700.0, "C": 1300.0}


class FakeEmbedder:
    """Отпечаток по основной частоте тона: разные «голоса» — разные направления."""

    def embed(self, samples):
        if len(samples) < RATE:
            return None
        spec = np.abs(np.fft.rfft(samples))
        f = np.argmax(spec) * RATE / len(samples)
        v = np.zeros(16, dtype=np.float32)
        v[int(np.argmin([abs(f - x) for x in FREQS.values()]))] = 1.0
        rng = np.random.default_rng(int(abs(samples[:50].sum()) * 1000) % 1000)
        v += rng.normal(0, 0.08, 16).astype(np.float32)
        return v / np.linalg.norm(v)


def tone(freq, sec):
    t = np.arange(int(RATE * sec)) / RATE
    return (np.sin(2 * np.pi * freq * t) * 6000).astype("<i2")


def make_meeting(folder, plan, label="Участники"):
    """plan: [(голос, секунды)] → mono wav + transcript.json."""
    folder.mkdir(parents=True, exist_ok=True)
    wav = folder / "20261026_100000.wav"
    pcm, segs, pos = [], [], 0.0
    for who, sec in plan:
        pcm.append(tone(FREQS[who], sec))
        segs.append({"start": round(pos, 2), "end": round(pos + sec, 2), "text": f"реплика {who}", "speaker": label})
        pos += sec
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(np.concatenate(pcm).tobytes())
    js = folder / "20261026_100000.transcript.json"
    js.write_text(json.dumps({"segments": segs}, ensure_ascii=False), encoding="utf-8")
    (folder / "20261026_100000.txt").write_text(tr.format_text(segs, "Тест"), encoding="utf-8")
    return wav


class VoicesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.profiles = voices.Profiles(self.tmp / "voices.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cluster_separates_voices(self):
        emb = FakeEmbedder()
        X = [emb.embed(tone(FREQS[w], 2)) for w in "ABABABCCA"]
        labels = voices.cluster(X)
        self.assertEqual(len(set(labels)), 3)
        self.assertEqual(labels[0], labels[2])
        self.assertNotEqual(labels[0], labels[1])

    def test_analyze_names_and_learning(self):
        wav = make_meeting(self.tmp / "rec", [("A", 6), ("B", 7), ("A", 5), ("B", 6)])
        sp = voices.analyze(wav, FakeEmbedder(), self.profiles)
        self.assertEqual(len(sp["clusters"]), 2)
        self.assertTrue(all(len(c["clips"]) >= 1 for c in sp["clusters"]))
        segs = json.loads(tr.output_paths(wav)[1].read_text(encoding="utf-8"))["segments"]
        self.assertEqual({s["speaker"] for s in segs}, {"Собеседник"})      # не узнаны — просто «Собеседник»
        self.assertEqual(segs[0]["speaker"], segs[2]["speaker"])
        # пользователь называет первый голос
        cid = int(sp["assign"]["0"])
        voices.name_voice(wav, cid, "Иван", self.profiles)
        txt = tr.output_paths(wav)[0].read_text(encoding="utf-8")
        self.assertIn("Иван:", txt)
        self.assertIn("Собеседник", txt)
        segs = json.loads(tr.output_paths(wav)[1].read_text(encoding="utf-8"))["segments"]
        self.assertEqual(segs[0]["speaker"], "Иван")
        self.assertEqual(segs[0]["speaker_raw"], "Участники")
        self.assertEqual(self.profiles.names(), ["Иван"])
        # профиль сохраняется на диск
        again = voices.Profiles(self.tmp / "voices.json")
        self.assertEqual(again.names(), ["Иван"])

    def test_known_voice_recognized_in_next_meeting(self):
        w1 = make_meeting(self.tmp / "r1", [("A", 8), ("B", 8)])
        sp = voices.analyze(w1, FakeEmbedder(), self.profiles)
        voices.name_voice(w1, int(sp["assign"]["0"]), "Иван", self.profiles)
        w2 = make_meeting(self.tmp / "r2", [("B", 6), ("A", 9), ("C", 7)], label="Собеседники")
        sp2 = voices.analyze(w2, FakeEmbedder(), self.profiles)
        names = sorted(c["name"] for c in sp2["clusters"])
        self.assertEqual(names, ["", "", "Иван"])
        segs = json.loads(tr.output_paths(w2)[1].read_text(encoding="utf-8"))["segments"]
        self.assertEqual(segs[1]["speaker"], "Иван")
        self.assertEqual(segs[0]["speaker"], "Собеседник")

    def test_rematch_after_new_person_learned(self):
        w1 = make_meeting(self.tmp / "r1", [("A", 8), ("B", 8)])
        sp = voices.analyze(w1, FakeEmbedder(), self.profiles)
        voices.name_voice(w1, int(sp["assign"]["0"]), "Иван", self.profiles)
        w2 = make_meeting(self.tmp / "r2", [("A", 8), ("C", 8)], label="Собеседники")
        voices.analyze(w2, FakeEmbedder(), self.profiles)
        voices.name_voice(w1, int(sp["assign"]["1"]), "Ольга", self.profiles)
        w3 = make_meeting(self.tmp / "r3", [("B", 8), ("C", 8)], label="Собеседники")
        voices.analyze(w3, FakeEmbedder(), self.profiles)
        self.assertEqual(sorted(c["name"] for c in voices.read_speakers(w3)["clusters"]), ["", "Ольга"])
        # старая встреча w2: Иван уже подписан, голос C — нет; после обучения C станет Петром и «подтянется»
        voices.name_voice(w3, [c["id"] for c in voices.read_speakers(w3)["clusters"] if not c["name"]][0],
                          "Пётр", self.profiles)
        voices.rematch(w2, self.profiles)
        self.assertEqual(sorted(c["name"] for c in voices.read_speakers(w2)["clusters"]), ["Иван", "Пётр"])

    def test_reassign_selected_segments_and_learn(self):
        wav = make_meeting(self.tmp / "ra", [("A", 6), ("B", 7), ("A", 5), ("B", 6)])
        voices.analyze(wav, FakeEmbedder(), self.profiles)
        voices.reassign_segments(wav, [1], "Ольга", FakeEmbedder(), self.profiles)
        segs = json.loads(tr.output_paths(wav)[1].read_text(encoding="utf-8"))["segments"]
        self.assertEqual(segs[1]["speaker"], "Ольга")
        self.assertNotEqual(segs[3]["speaker"], "Ольга")                   # остальные реплики не тронуты
        self.assertIn("Ольга", self.profiles.people)
        voices.reassign_segments(wav, [3], "Ольга", FakeEmbedder(), self.profiles)   # тот же человек — тот же голос
        sp = voices.read_speakers(wav)
        self.assertEqual(sum(1 for c in sp["clusters"] if c["name"] == "Ольга"), 1)
        with self.assertRaises(voices.VoicesError):
            voices.reassign_segments(wav, [], "Ольга", FakeEmbedder(), self.profiles)

    def test_clip_segments_and_game_rounds(self):
        wav = make_meeting(self.tmp / "g", [("A", 6), ("B", 7), ("A", 5), ("B", 6)])
        sp = voices.analyze(wav, FakeEmbedder(), self.profiles)
        a_id = int(sp["assign"]["0"])
        voices.name_voice(wav, a_id, "Иван", self.profiles)
        clip = next(c for c in voices.read_speakers(wav)["clusters"] if c["id"] == a_id)["clips"][0]
        self.assertIn(voices.segments_for_clip(wav, a_id, clip)[0], (0, 2))
        rounds = voices.game_rounds(wav)
        self.assertTrue(rounds)
        self.assertTrue(all(r[4] == "Иван" for r in rounds))        # только подписанные голоса
        # отрывок перенесли другому человеку — у Ивана он пропадает
        seg = voices.segments_for_clip(wav, a_id, clip)
        voices.reassign_segments(wav, seg, "Ольга", FakeEmbedder(), self.profiles)
        ivan = next(c for c in voices.read_speakers(wav)["clusters"] if c["id"] == a_id)
        self.assertNotIn(clip["start"], [c["start"] for c in ivan["clips"]])

    def test_unsure_match_is_only_a_guess(self):
        p = voices.Profiles(self.tmp / "v2.json")
        a = np.zeros(16, dtype=np.float32); a[0] = 1.0
        b = np.zeros(16, dtype=np.float32); b[0], b[1] = 0.65, 0.76         # похож на «Глеба», но не очень (0.65)
        p.learn("Глеб", [a])
        self.assertEqual(p.judge(a), ("Глеб", "", 1.0))
        name, guess, score = p.judge(b)
        self.assertEqual((name, guess), ("", "Глеб"))                      # только предположение
        p.learn("Ольга", [b])
        c = np.zeros(16, dtype=np.float32); c[0], c[1] = 0.95, 0.31        # близок к обоим — отрыв мал
        c /= np.linalg.norm(c)
        name, guess, _ = p.judge(c)
        self.assertEqual(name, "")
        self.assertTrue(guess)

    def test_demote_old_unsure_names(self):
        wav = make_meeting(self.tmp / "dm", [("A", 6), ("B", 7)])
        voices.analyze(wav, FakeEmbedder(), self.profiles)
        sp = voices.read_speakers(wav)
        sp["clusters"][0].update(name="Глеб", auto=True, score=0.62)       # старая версия подставила наугад
        sp["clusters"][1].update(name="Анна", auto=False, score=0.5)       # подтверждено вами — не трогаем
        voices._write_json(voices.speakers_path(wav), sp)
        self.assertTrue(voices.demote_unsure(wav))
        sp = voices.read_speakers(wav)
        self.assertEqual((sp["clusters"][0]["name"], sp["clusters"][0]["guess"]), ("", "Глеб"))
        self.assertEqual(sp["clusters"][1]["name"], "Анна")
        self.assertFalse(voices.demote_unsure(wav))

    def test_confirm_auto_voice_adds_training(self):
        w1 = make_meeting(self.tmp / "c1", [("A", 8), ("B", 8)])
        sp = voices.analyze(w1, FakeEmbedder(), self.profiles)
        voices.name_voice(w1, int(sp["assign"]["0"]), "Иван", self.profiles)
        w2 = make_meeting(self.tmp / "c2", [("A", 8), ("B", 8), ("C", 8)], label="Собеседники")
        sp2 = voices.analyze(w2, FakeEmbedder(), self.profiles)
        auto = [c for c in sp2["clusters"] if c["auto"]][0]
        before = len(self.profiles.people["Иван"])
        self.assertTrue(voices.confirm_voice(w2, auto["id"], self.profiles))
        self.assertEqual(len(self.profiles.people["Иван"]), before + 1)
        self.assertFalse(voices.confirm_voice(w2, auto["id"], self.profiles))     # повторно — нет
        self.assertEqual(len(sp2["clusters"]), 3)

    def test_short_noise_is_not_a_voice_and_you_stays(self):
        wav = make_meeting(self.tmp / "rec", [("A", 8), ("B", 1.2)])
        sp = voices.analyze(wav, FakeEmbedder(), self.profiles)
        self.assertEqual(len(sp["clusters"]), 1)            # реплика на 1,2 с — меньше MIN_VOICE_SEC
        data = json.loads(tr.output_paths(wav)[1].read_text(encoding="utf-8"))
        data["segments"][0]["speaker"] = "Вы"
        data["segments"][0].pop("speaker_raw", None)
        tr.output_paths(wav)[1].write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        voices.apply_names(wav)
        segs = json.loads(tr.output_paths(wav)[1].read_text(encoding="utf-8"))["segments"]
        self.assertEqual(segs[0]["speaker"], "Вы")
        self.assertEqual(segs[1]["speaker"], "Участники")

    def test_profiles_manage(self):
        self.profiles.learn("Иван", [np.ones(4, dtype=np.float32)] * 20)
        self.assertEqual(len(self.profiles.people["Иван"]), voices.MAX_STORED)
        self.profiles.rename("Иван", "Иван П.")
        self.assertEqual(self.profiles.names(), ["Иван П."])
        self.profiles.forget("Иван П.")
        self.assertEqual(self.profiles.names(), [])

    def test_download_model_checks_size(self):
        class Resp:
            headers = {"Content-Length": "10"}

            def __init__(self): self.data = b"x" * 10
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n):
                d, self.data = self.data, b""
                return d
        with self.assertRaises(voices.VoicesError):
            voices.download_model(self.tmp, opener=lambda url, timeout=0: Resp())
        self.assertFalse(voices.model_ready(self.tmp))


if __name__ == "__main__":
    unittest.main()


class ForcedCountTests(unittest.TestCase):
    def test_cluster_k_splits_similar_voices(self):
        rng = np.random.default_rng(1)
        base = np.eye(8, dtype=np.float32)
        a, b, c = base[0], _mix(base[0], base[1], 0.75), base[4]       # a и b похожи (≈0,8)
        X = []
        for v in (a, b, c) * 6:
            w = v + rng.normal(0, 0.03, 8).astype(np.float32)
            X.append(w / np.linalg.norm(w))
        auto = voices.cluster(X)
        self.assertEqual(len(set(auto)), 2)                               # автоматика склеила a и b
        forced = voices.cluster_k(X, 3)
        self.assertEqual(len(set(forced)), 3)
        self.assertEqual(forced[0::3], [forced[0]] * 6)                   # a всегда в одном голосе
        self.assertNotEqual(forced[0], forced[1])
        self.assertEqual(len(set(voices.cluster_k(X, 1))), 1)
        self.assertEqual(len(set(voices.cluster_k(X[:2], 5))), 2)         # не больше, чем векторов

    def test_analyze_with_forced_count(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            wav = make_meeting(tmp / "r", [("A", 6), ("B", 6), ("C", 6), ("A", 5)])
            sp = voices.analyze(wav, FakeEmbedder(), voices.Profiles(tmp / "v.json"), speakers=3)
            self.assertEqual(len(sp["clusters"]), 3)
            self.assertEqual(sp["speakers"], 3)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _mix(u, v, w):
    r = w * u + (1 - w * w) ** 0.5 * v
    return r / np.linalg.norm(r)
