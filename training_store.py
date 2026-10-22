# -*- coding: utf-8 -*-
"""Meeting Hub v5 — хранилище обучения (meeting_hub_training.json, схема 2).

Что хранится:
  * meeting_patterns — шаблоны заголовков окон, которые пользователь подтвердил как встречу;
  * ignore_patterns  — заголовки, которые пользователь отметил как «не встреча»;
  * journal          — журнал решений детектора и действий пользователя (с объяснением);
  * legacy_v49       — история v4.x (события, метки, app_rules) без изменений.

Правила: история не удаляется; перед миграцией и импортом делается резервная копия;
запись атомарная (временный файл + замена), чтобы сбой не испортил данные.
"""
import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from meeting_detector import normalize, ALGORITHM_VERSION

SCHEMA = 2
JOURNAL_LIMIT = 2000


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fresh():
    return {"schema": SCHEMA, "meeting_patterns": [], "ignore_patterns": [], "journal": []}


class TrainingStore:
    def __init__(self, path):
        self.path = Path(path)
        self.notices = []          # сообщения для пользователя о миграции и ошибках
        self.data = self._load()

    # ---------- загрузка и миграция ----------
    def _load(self):
        if not self.path.exists():
            return _fresh()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            broken = self._backup("broken")
            self.notices.append(f"Файл обучения не читается ({e}). Он сохранён как {broken.name}, начат новый.")
            data = _fresh()
            self._write(data)
            return data
        if isinstance(raw, dict) and raw.get("schema") == SCHEMA:
            for key, default in _fresh().items():
                raw.setdefault(key, default)
            return raw
        return self._migrate_v1(raw)

    def _migrate_v1(self, raw):
        backup = self._backup("v4-backup")
        raw = raw if isinstance(raw, dict) else {"events": raw if isinstance(raw, list) else []}
        data = _fresh()
        data["legacy_v49"] = {
            "events": raw.get("events", []),
            "feedback": raw.get("feedback", []),
            "app_rules": raw.get("app_rules", {}),
        }
        extra = {k: v for k, v in raw.items() if k not in ("events", "feedback", "app_rules")}
        if extra:
            data["legacy_v49"]["other"] = extra
        data["migration"] = {
            "from": "v4.x",
            "at": _now(),
            "backup": backup.name,
            "note": ("История v4.x перенесена без изменений в legacy_v49. Правило app_rules "
                     "(например chrome.exe: always) больше не управляет детектором: в v5 встреча "
                     "определяется по окну встречи и занятому микрофону."),
        }
        self.notices.append(
            f"Данные обучения v4.9 перенесены в новый формат. Резервная копия: {backup.name}.")
        self._write(data)
        return data

    def _backup(self, tag):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.path.with_name(f"{self.path.stem}.{tag}-{stamp}{self.path.suffix}")
        n = 1
        while target.exists():
            target = self.path.with_name(f"{self.path.stem}.{tag}-{stamp}-{n}{self.path.suffix}")
            n += 1
        shutil.copy2(self.path, target)
        return target

    # ---------- запись ----------
    def _write(self, data):
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def save(self):
        self._write(self.data)

    # ---------- шаблоны ----------
    def meeting_patterns(self):
        return list(self.data["meeting_patterns"])

    def ignore_patterns(self):
        return list(self.data["ignore_patterns"])

    def add_pattern(self, pattern, label="", kind="meeting", source="user", example=""):
        """Добавить шаблон. Повтор (без учёта регистра и вида тире) не дублируется."""
        key = "meeting_patterns" if kind == "meeting" else "ignore_patterns"
        norm = normalize(pattern)
        if len(norm.strip()) < 3:
            raise ValueError("Шаблон слишком короткий: нужно минимум 3 символа.")
        for item in self.data[key]:
            if normalize(item["pattern"]) == norm:
                return False
        self.data[key].append({"pattern": pattern, "label": label or pattern.strip(), "strict": True,
                               "source": source, "added": _now(), "example": example[:160],
                               "algorithm": ALGORITHM_VERSION})
        self.save()
        return True

    def remove_pattern(self, pattern, kind="meeting"):
        key = "meeting_patterns" if kind == "meeting" else "ignore_patterns"
        norm = normalize(pattern)
        before = len(self.data[key])
        self.data[key] = [p for p in self.data[key] if normalize(p["pattern"]) != norm]
        if len(self.data[key]) != before:
            self.save()
            return True
        return False

    # ---------- журнал ----------
    def log(self, entry):
        item = dict(entry)
        item.setdefault("time", _now())
        item["id"] = f"{int(time.time() * 1000)}-{len(self.data['journal'])}"
        item.setdefault("user_label", "")
        self.data["journal"].append(item)
        if len(self.data["journal"]) > JOURNAL_LIMIT:
            self.data["journal"] = self.data["journal"][-JOURNAL_LIMIT:]
        self.save()
        return item

    def set_label(self, entry_id, label):
        for item in self.data["journal"]:
            if item.get("id") == entry_id:
                item["user_label"] = label
                item["labeled_at"] = _now()
                self.save()
                return item
        return None

    def legacy_events(self):
        return list(self.data.get("legacy_v49", {}).get("events", []))

    # ---------- экспорт / импорт ----------
    def export(self, target):
        Path(target).write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def import_file(self, source):
        """Слить данные из файла: шаблоны и журнал объединяются, ничего не удаляется."""
        raw = json.loads(Path(source).read_text(encoding="utf-8"))
        if not (isinstance(raw, dict) and raw.get("schema") == SCHEMA):
            # старый формат импортируем в память, исходный файл не трогаем
            data = _fresh()
            raw = raw if isinstance(raw, dict) else {}
            data["legacy_v49"] = {"events": raw.get("events", []), "feedback": raw.get("feedback", []),
                                  "app_rules": raw.get("app_rules", {})}
            raw = data
        if self.path.exists():
            self._backup("before-import")
        added = 0
        for key in ("meeting_patterns", "ignore_patterns"):
            known = {normalize(p["pattern"]) for p in self.data[key]}
            for p in raw.get(key, []):
                if normalize(p.get("pattern", "")) not in known:
                    self.data[key].append(p)
                    known.add(normalize(p.get("pattern", "")))
                    added += 1
        ids = {j.get("id") for j in self.data["journal"]}
        for j in raw.get("journal", []):
            if j.get("id") not in ids:
                self.data["journal"].append(j)
                added += 1
        self.data["journal"].sort(key=lambda j: str(j.get("time", "")))
        if "legacy_v49" not in self.data and raw.get("legacy_v49"):
            self.data["legacy_v49"] = raw["legacy_v49"]
            added += 1
        self.save()
        return added
