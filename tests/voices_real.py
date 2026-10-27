# -*- coding: utf-8 -*-
"""Проверка на настоящей модели: два синтезированных голоса должны разойтись по двум разным «голосам».

Запускается в CI (Windows): файлы v1_*.wav и v2_*.wav делает System.Speech (разные голоса).
Печатает близости, чтобы подобрать пороги.
"""
import json
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import transcriber as tr  # noqa: E402
import voices  # noqa: E402

src = Path(sys.argv[1])
work = Path(tempfile.mkdtemp())
order = ["v1_1", "v2_1", "v1_2", "v2_2", "v1_3", "v2_3"]
pcm, segs, pos = [], [], 0.0
gap = np.zeros(int(0.8 * 16000), dtype="<i2")
for name in order:
    with wave.open(str(src / f"{name}.wav"), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1, name
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    segs.append({"start": round(pos, 2), "end": round(pos + len(data) / 16000, 2), "text": name,
                 "speaker": "Участники"})
    pcm += [data, gap]
    pos += len(data) / 16000 + 0.8
wav = work / "20261026_100000.wav"
with wave.open(str(wav), "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(np.concatenate(pcm).tobytes())
(work / "20261026_100000.transcript.json").write_text(json.dumps({"segments": segs}), encoding="utf-8")
(work / "20261026_100000.txt").write_text(tr.format_text(segs, "t"), encoding="utf-8")
print("durations:", [(s["text"], round(s["end"] - s["start"], 1)) for s in segs])

data_dir = work / "data"
voices.download_model(data_dir)
print("model:", voices.model_path(data_dir), voices.model_path(data_dir).stat().st_size)
emb = voices.Embedder(voices.model_path(data_dir))
audio = np.frombuffer(wav.read_bytes()[44:], dtype="<i2").astype(np.float32) / 32768.0
vecs = [emb.embed(audio[int(s["start"] * 16000):int(s["end"] * 16000)][:16000 * 12]) for s in segs]
S = np.array([[float(np.dot(a, b)) for b in vecs] for a in vecs])
np.set_printoptions(precision=2, suppress=True)
print("similarity (order %s):\n%s" % (order, S))
same = [S[i, j] for i in range(6) for j in range(i + 1, 6) if order[i][:2] == order[j][:2]]
diff = [S[i, j] for i in range(6) for j in range(i + 1, 6) if order[i][:2] != order[j][:2]]
print(f"::notice title=voice similarity::same min={min(same):.2f} mean={np.mean(same):.2f}; "
      f"different max={max(diff):.2f} mean={np.mean(diff):.2f}; thresholds same={voices.SAME_VOICE} "
      f"known={voices.KNOWN_VOICE}")
profiles = voices.Profiles(work / "voices.json")
sp = voices.analyze(wav, emb, profiles)
assign = [sp["assign"].get(str(i)) for i in range(6)]
print("clusters:", [(c["id"], c["seconds"]) for c in sp["clusters"]], "assign:", assign)
print(f"::notice title=voice clusters::{len(sp['clusters'])} clusters, assign={assign}")
assert min(same) > max(diff), "голоса одного человека не ближе голосов разных людей"
assert len(sp["clusters"]) == 2, f"ожидалось 2 голоса, найдено {len(sp['clusters'])}"
assert assign[0] == assign[2] == assign[4] and assign[1] == assign[3] == assign[5] and assign[0] != assign[1], assign
# обучение и узнавание в «следующей встрече»
cid = assign[0]
voices.name_voice(wav, cid, "Первый", profiles)
sp2 = voices.analyze(wav, emb, profiles)
names = sorted(c["name"] for c in sp2["clusters"])
print("after learning:", names)
assert "Первый" in names and names.count("") == 1, names
print("OK")
