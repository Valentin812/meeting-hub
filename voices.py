# -*- coding: utf-8 -*-
"""Кто говорит: голоса участников встречи.

Как это работает
* Для реплик «Собеседники» (и «Участники» в загруженных файлах) считаются голосовые отпечатки —
  небольшие векторы, по которым голоса отличаются. Модель локальная (ONNX, sherpa-onnx), звук никуда не уходит.
* Похожие отпечатки собираются в голоса: «Голос 1», «Голос 2»…
* Пользователь слушает короткие отрывки и пишет имя. Имя запоминается вместе с отпечатком, и в следующих
  встречах этот человек подписывается сам. Чем больше подтверждённых отрывков, тем точнее.
* Результат: <запись>.speakers.json (голоса, отрывки, имена) и подписи в <запись>.transcript.json и .txt.
"""
import json
import os
import tempfile
import urllib.request
from pathlib import Path

import numpy as np

import transcriber as tr

RATE = 16000
MODEL_NAME = "wespeaker_en_voxceleb_resnet34.onnx"
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
             + MODEL_NAME)
MODEL_MIN_BYTES = 5_000_000

OTHERS = (tr.SPEAKERS[1], "Участники")      # метки, которые разбираем по голосам
MIN_SEC = 1.0           # короче — отпечаток ненадёжен
WINDOW_SEC = 4.0        # длинную реплику режем на окна
MAX_WINDOWS = 6         # на реплику, чтобы часовая встреча считалась быстро
SAME_VOICE = 0.60       # косинусная близость: один и тот же голос внутри встречи
KNOWN_VOICE = 0.60      # близость к сохранённому голосу: подписываем именем автоматически
MIN_VOICE_SEC = 4.0     # меньше суммарно — голос не заводим, реплики остаются «Собеседники»
MAX_STORED = 12         # отпечатков на человека
CLIPS = 3               # отрывков для прослушивания на голос
CLIP_SEC = 8.0


class VoicesError(Exception):
    pass


# ---------------------------------------------------------------------------
# Модель отпечатков
# ---------------------------------------------------------------------------
def available():
    try:
        import sherpa_onnx  # noqa: F401
        return True, ""
    except Exception as e:
        return False, f"модуль распознавания голосов не установлен ({type(e).__name__}: {e})"


def model_path(data_dir):
    return Path(data_dir) / "models" / "voices" / MODEL_NAME


def model_ready(data_dir):
    p = model_path(data_dir)
    try:
        return p.is_file() and p.stat().st_size >= MODEL_MIN_BYTES
    except OSError:
        return False


def download_model(data_dir, progress=None, url=MODEL_URL, opener=urllib.request.urlopen):
    """Скачать модель один раз (около 27 МБ). Возвращает путь."""
    dest = model_path(data_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
    try:
        with os.fdopen(fd, "wb") as out, opener(url, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            while True:
                block = resp.read(1 << 16)
                if not block:
                    break
                out.write(block)
                got += len(block)
                if progress is not None and total:
                    progress(got / total)
        if os.path.getsize(tmp) < MODEL_MIN_BYTES:
            raise VoicesError("модель скачалась не полностью")
        os.replace(tmp, dest)
    except VoicesError:
        raise
    except OSError as e:
        raise VoicesError(f"не удалось скачать модель голосов: {e}. Модель можно положить вручную в "
                          f"папку {dest.parent}")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return dest


class Embedder:
    """Голосовой отпечаток по куску звука (float32, 16 кГц). Возвращает нормированный вектор или None."""

    def __init__(self, model_file, threads=2):
        ok, why = available()
        if not ok:
            raise VoicesError(why)
        import sherpa_onnx
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model_file), num_threads=threads,
                                                          debug=False, provider="cpu")
        if hasattr(cfg, "validate") and not cfg.validate():
            raise VoicesError("модель голосов не читается: " + str(model_file))
        self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)

    def embed(self, samples):
        if len(samples) < int(MIN_SEC * RATE):
            return None
        stream = self.extractor.create_stream()
        stream.accept_waveform(sample_rate=RATE, waveform=np.asarray(samples, dtype=np.float32))
        stream.input_finished()
        if not self.extractor.is_ready(stream):
            return None
        v = np.asarray(self.extractor.compute(stream), dtype=np.float32)
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else None


# ---------------------------------------------------------------------------
# Сохранённые голоса людей
# ---------------------------------------------------------------------------
class Profiles:
    """people: {имя: [отпечаток, ...]} в voices.json рядом с настройками."""

    def __init__(self, path):
        self.path = Path(path)
        self.people = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for name, embs in (data.get("people") or {}).items():
                self.people[name] = [np.asarray(e, dtype=np.float32) for e in embs]
        except (OSError, ValueError, TypeError):
            pass

    def save(self):
        data = {"version": 1, "people": {n: [[round(float(x), 5) for x in e] for e in embs]
                                         for n, embs in self.people.items()}}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def names(self):
        return sorted(self.people)

    def learn(self, name, embeddings):
        name = name.strip()
        if not name:
            return
        mine = self.people.setdefault(name, [])
        mine.extend(np.asarray(e, dtype=np.float32) for e in embeddings)
        del mine[:-MAX_STORED]

    def forget(self, name):
        self.people.pop(name, None)

    def rename(self, old, new):
        new = new.strip()
        if old in self.people and new and new != old:
            self.people.setdefault(new, []).extend(self.people.pop(old))
            del self.people[new][:-MAX_STORED]

    def best_match(self, vec, threshold=KNOWN_VOICE):
        """(имя, близость) самого похожего человека или ("", лучшая близость)."""
        best_name, best = "", -1.0
        for name, embs in self.people.items():
            if not embs:
                continue
            sims = sorted((float(np.dot(vec, e)) for e in embs), reverse=True)
            score = float(np.mean(sims[:3]))
            if score > best:
                best_name, best = name, score
        return (best_name, best) if best >= threshold else ("", best)


# ---------------------------------------------------------------------------
# Кластеризация
# ---------------------------------------------------------------------------
def _unit(v):
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def cluster(embs, threshold=SAME_VOICE):
    """Метки голосов для векторов (нормированных). Последовательное объединение, слияние, уточнение."""
    n = len(embs)
    if n == 0:
        return []
    X = np.stack(embs)
    cents, members = [], []
    for i in range(n):
        if cents:
            sims = np.stack(cents) @ X[i]
            j = int(np.argmax(sims))
            if sims[j] >= threshold:
                members[j].append(i)
                cents[j] = _unit(X[members[j]].mean(axis=0))
                continue
        cents.append(X[i].copy())
        members.append([i])
    merged = True
    while merged and len(cents) > 1:
        merged = False
        C = np.stack(cents)
        S = C @ C.T
        np.fill_diagonal(S, -1)
        a, b = np.unravel_index(int(np.argmax(S)), S.shape)
        if S[a, b] >= threshold:
            members[a] += members[b]
            cents[a] = _unit(X[members[a]].mean(axis=0))
            del cents[b], members[b]
            merged = True
    for _ in range(2):                                   # уточнение: каждое окно — к ближайшему центру
        C = np.stack(cents)
        labels = np.argmax(X @ C.T, axis=1)
        for k in range(len(cents)):
            idx = np.where(labels == k)[0]
            if len(idx):
                cents[k] = _unit(X[idx].mean(axis=0))
    C = np.stack(cents)
    return [int(k) for k in np.argmax(X @ C.T, axis=1)]


def cluster_k(embs, k):
    """Ровно k голосов (сферический k-means с несколькими запусками). Для случая «я знаю, сколько было людей»."""
    n = len(embs)
    if n == 0:
        return []
    k = max(1, min(int(k), n))
    X = np.stack(embs)
    rng = np.random.default_rng(7)
    best_labels, best_score = None, -1e9
    for _ in range(8):
        cents = [X[int(rng.integers(n))]]
        while len(cents) < k:                                   # k-means++: далёкие от уже выбранных центров
            d = 1.0 - np.max(X @ np.stack(cents).T, axis=1)
            p = np.maximum(d, 1e-9) ** 2
            cents.append(X[int(rng.choice(n, p=p / p.sum()))])
        C = np.stack(cents)
        for _it in range(30):
            labels = np.argmax(X @ C.T, axis=1)
            newC = C.copy()
            for j in range(k):
                idx = np.where(labels == j)[0]
                if len(idx):
                    newC[j] = _unit(X[idx].mean(axis=0))
            if np.allclose(newC, C):
                break
            C = newC
        labels = np.argmax(X @ C.T, axis=1)
        score = float(np.sum(np.max(X @ C.T, axis=1)))
        if score > best_score:
            best_labels, best_score = labels, score
    return [int(x) for x in best_labels]


# ---------------------------------------------------------------------------
# Разбор встречи
# ---------------------------------------------------------------------------
def speakers_path(wav):
    wav = Path(wav)
    return wav.with_name(wav.stem + ".speakers.json")


def read_speakers(wav):
    try:
        return json.loads(speakers_path(wav).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def label_of(cluster):
    return (cluster.get("name") or "").strip() or f"Голос {cluster['id']}"


def _write_json(path, data):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _other_audio(wav):
    mine, theirs = tr.read_channels(wav)
    return theirs if len(theirs) else mine


def _windows(seg):
    start, end = float(seg.get("start", 0)), float(seg.get("end", 0))
    dur = end - start
    if dur < MIN_SEC:
        return []
    n = max(1, min(MAX_WINDOWS, int(dur // WINDOW_SEC)))
    step = dur / n
    return [(start + k * step, start + (k + 1) * step) for k in range(n)]


def analyze(wav, embedder, profiles, progress=None, should_stop=None, speakers=None):
    """Найти голоса в расшифровке встречи, подписать известные. Возвращает данные speakers.json.

    speakers — сколько собеседников было на самом деле (если известно): тогда голосов будет ровно столько.
    """
    wav = Path(wav)
    js = tr.output_paths(wav)[1]
    data = json.loads(js.read_text(encoding="utf-8"))
    segs = data.get("segments") or []
    audio = _other_audio(wav) if wav.exists() else None
    if audio is None:
        raise VoicesError("аудио удалено — голоса определить нельзя")
    items = []                     # (индекс реплики, начало, конец, вектор)
    todo = [(i, s) for i, s in enumerate(segs) if (s.get("speaker_raw") or s.get("speaker")) in OTHERS]
    for n, (i, s) in enumerate(todo):
        if should_stop is not None:
            should_stop()
        for a, b in _windows(s):
            v = embedder.embed(audio[int(a * RATE):int(b * RATE)])
            if v is not None:
                items.append((i, a, b, v))
        if progress is not None and todo:
            progress((n + 1) / len(todo))
    vecs = [it[3] for it in items]
    labels = cluster_k(vecs, speakers) if speakers else cluster(vecs)
    groups = {}
    for it, lab in zip(items, labels):
        groups.setdefault(lab, []).append(it)
    clusters, seg_votes = [], {}
    order = sorted(groups.values(), key=lambda g: -sum(b - a for _i, a, b, _v in g))
    for g in order:
        total = sum(b - a for _i, a, b, _v in g)
        if total < MIN_VOICE_SEC and not speakers:
            continue                                           # случайные короткие обрывки — не голос
        cid = len(clusters) + 1
        cent = _unit(np.mean([v for *_x, v in g], axis=0))
        name, score = profiles.best_match(cent)
        by_seg = {}
        for i, a, b, _v in g:
            by_seg.setdefault(i, []).append((a, b))
        best = sorted(((sum(b - a for a, b in w), i, w) for i, w in by_seg.items()), reverse=True)[:CLIPS]
        clips = []
        for _d, i, w in best:
            a = min(x for x, _y in w)
            b = max(y for _x, y in w)
            clips.append({"start": round(a, 2), "end": round(min(b, a + CLIP_SEC), 2)})
        clusters.append({"id": cid, "name": name, "auto": bool(name), "score": round(score, 3),
                         "seconds": round(total, 1), "clips": clips,
                         "centroid": [round(float(x), 5) for x in cent]})
        for i, a, b, _v in g:
            seg_votes.setdefault(i, {}).setdefault(cid, 0.0)
            seg_votes[i][cid] += b - a
    assign = {str(i): max(v, key=v.get) for i, v in seg_votes.items()}
    result = {"model": MODEL_NAME, "clusters": clusters, "assign": assign, "speakers": speakers or 0}
    _write_json(speakers_path(wav), result)
    apply_names(wav)
    return result


def apply_names(wav):
    """Записать подписи голосов в transcript.json и .txt. Исходная метка хранится в speaker_raw."""
    wav = Path(wav)
    sp = read_speakers(wav)
    txt, js = tr.output_paths(wav)
    data = json.loads(js.read_text(encoding="utf-8"))
    labels = {c["id"]: label_of(c) for c in (sp or {}).get("clusters", [])}
    assign = (sp or {}).get("assign", {})
    for i, s in enumerate(data.get("segments") or []):
        raw = s.get("speaker_raw") or s.get("speaker")
        if raw in OTHERS:
            s["speaker_raw"] = raw
            cid = assign.get(str(i))
            s["speaker"] = labels.get(cid, raw) if cid is not None else raw
    _write_json(js, data)
    try:
        first = txt.read_text(encoding="utf-8-sig").splitlines()[0] if txt.exists() else ""
    except (OSError, IndexError):
        first = ""
    title = first if first and not first.startswith("[") else f"Расшифровка {wav.stem}"
    txt.write_text(tr.format_text(data.get("segments") or [], title), encoding="utf-8")


def name_voice(wav, cluster_id, name, profiles):
    """Пользователь подписал голос: запомнить имя и отпечаток, перенести имя в текст."""
    wav = Path(wav)
    sp = read_speakers(wav)
    if not sp:
        raise VoicesError("голоса этой встречи ещё не определены")
    name = (name or "").strip()
    for c in sp["clusters"]:
        if c["id"] == cluster_id:
            c["name"], c["auto"] = name, False
            if name and c.get("centroid"):
                profiles.learn(name, [np.asarray(c["centroid"], dtype=np.float32)])
                profiles.save()
    _write_json(speakers_path(wav), sp)
    apply_names(wav)
    return sp


def rematch(wav, profiles):
    """Подписать ещё не названные голоса встречи по сохранённым людям (звук не нужен)."""
    wav = Path(wav)
    sp = read_speakers(wav)
    if not sp:
        return None
    changed = False
    for c in sp["clusters"]:
        if not (c.get("name") or "").strip() and c.get("centroid"):
            name, score = profiles.best_match(np.asarray(c["centroid"], dtype=np.float32))
            if name:
                c["name"], c["auto"], c["score"] = name, True, round(score, 3)
                changed = True
    if changed:
        _write_json(speakers_path(wav), sp)
        apply_names(wav)
    return sp
