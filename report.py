# -*- coding: utf-8 -*-
"""Протокол встречи: один HTML-файл (открывается в браузере, печатается, пересылается) и Markdown.

Собирается из метаданных записи, итогов (analyzer) и расшифровки (transcriber). Если итогов нет,
протокол всё равно содержит расшифровку.
"""
import html
import json
from pathlib import Path

import transcriber as tr


def protocol_paths(wav):
    wav = Path(wav)
    return wav.with_name(wav.stem + ".protocol.html"), wav.with_name(wav.stem + ".protocol.md")


def load_parts(wav):
    """(meta записи, итоги или None, сегменты расшифровки или None)."""
    wav = Path(wav)

    def read(p):
        try:
            return json.loads(Path(p).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    meta = read(wav.with_suffix(".json")) or {}
    result = read(wav.with_name(wav.stem + ".summary.json"))
    tjs = read(tr.output_paths(wav)[1]) or {}
    return meta, result, tjs.get("segments")


def _dur(sec):
    sec = int(sec or 0)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _title(wav, meta):
    return meta.get("meeting") or meta.get("window") or Path(wav).stem


def _when(wav):
    s = Path(wav).stem
    if len(s) >= 13 and s[:8].isdigit():
        return f"{s[6:8]}.{s[4:6]}.{s[0:4]} {s[9:11]}:{s[11:13]}"
    return s


def build_markdown(wav, meta, result, segments):
    lines = [f"# {_title(wav, meta)}", "", f"Дата: {_when(wav)} · Длительность: {_dur(meta.get('duration_sec'))}", ""]
    if result:
        bullets = [f"- {x}" for x in result.get("summary") or []] or ["- не сформировано"]
        lines += ["## Резюме", *bullets, ""]
        if result.get("decisions"):
            lines += ["## Решения", *[f"- {d.get('text', '')}" for d in result["decisions"]], ""]
        if result.get("tasks"):
            lines += ["## Задачи", "| Задача | Ответственный | Срок |", "|---|---|---|"]
            lines += [f"| {t.get('task', '')} | {t.get('owner', '') or '—'} | {t.get('deadline', '') or '—'} |"
                      for t in result["tasks"]] + [""]
        if result.get("open_questions"):
            lines += ["## Открытые вопросы", *[f"- {q.get('question', '')}" for q in result["open_questions"]], ""]
        lines += ["> Итоги подготовлены ИИ по расшифровке речи — проверьте перед использованием.", ""]
    if segments:
        lines += ["## Расшифровка", ""]
        lines += [f"[{tr.stamp(s['start'])}] **{s['speaker']}:** {s['text']}" for s in segments]
    return "\n".join(lines).strip() + "\n"


CSS = """
:root{--bg:#fff;--fg:#1c1f23;--mut:#5f6b76;--line:#d9dee3;--acc:#1a5fb4;--card:#f5f7f9}
@media (prefers-color-scheme:dark){:root{--bg:#16191d;--fg:#e6e9ec;--mut:#9aa6b2;--line:#2c333a;--acc:#6aa7ff;--card:#1e2328}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,"Segoe UI",sans-serif}
main{max-width:860px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
.meta{color:var(--mut);margin-bottom:8px}
ul{padding-left:22px}li{margin:3px 0}
table{border-collapse:collapse;width:100%}th,td{border:1px solid var(--line);padding:6px 10px;text-align:left;vertical-align:top}
th{background:var(--card)}
.note{background:var(--card);border-left:4px solid var(--acc);padding:8px 12px;margin:20px 0;color:var(--mut)}
.tr p{margin:6px 0}.t{color:var(--mut);font-variant-numeric:tabular-nums;margin-right:6px}
.me{color:var(--acc);font-weight:600}.they{font-weight:600}
@media print{.note{border:1px solid #999}body{font-size:12px}}
"""


def build_html(wav, meta, result, segments):
    e = html.escape
    out = [f"<h1>{e(_title(wav, meta))}</h1>",
           f"<div class=meta>{e(_when(wav))} · длительность {e(_dur(meta.get('duration_sec')))}</div>"]
    if result:
        out.append("<h2>Резюме</h2><ul>" + "".join(f"<li>{e(str(x))}</li>" for x in result.get("summary") or [])
                   + "</ul>" if result.get("summary") else "<h2>Резюме</h2><p>Не сформировано.</p>")
        if result.get("decisions"):
            out.append("<h2>Решения</h2><ul>" + "".join(f"<li>{e(str(d.get('text', '')))}</li>"
                                                         for d in result["decisions"]) + "</ul>")
        if result.get("tasks"):
            rows = "".join(f"<tr><td>{e(str(t.get('task', '')))}</td><td>{e(str(t.get('owner') or '—'))}</td>"
                           f"<td>{e(str(t.get('deadline') or '—'))}</td></tr>" for t in result["tasks"])
            out.append("<h2>Задачи</h2><table><tr><th>Задача</th><th>Ответственный</th><th>Срок</th></tr>"
                       + rows + "</table>")
        if result.get("open_questions"):
            out.append("<h2>Открытые вопросы</h2><ul>" + "".join(
                f"<li>{e(str(q.get('question', '')))}</li>" for q in result["open_questions"]) + "</ul>")
        out.append("<div class=note>Итоги подготовлены ИИ по расшифровке речи, возможны ошибки — "
                   "проверьте перед использованием.</div>")
    else:
        out.append("<div class=note>Итоги встречи ещё не подготовлены — ниже полная расшифровка.</div>")
    if segments:
        out.append("<h2>Расшифровка</h2><div class=tr>")
        for s in segments:
            cls = "me" if s["speaker"] == tr.SPEAKERS[0] else "they"
            out.append(f"<p><span class=t>{tr.stamp(s['start'])}</span><span class={cls}>{e(s['speaker'])}:</span> "
                       f"{e(s['text'])}</p>")
        out.append("</div>")
    return ("<!doctype html><html lang=ru><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{e(_title(wav, meta))} — протокол</title><style>{CSS}</style></head><body><main>"
            + "".join(out) + "</main></body></html>")


def write_protocol(wav):
    """Собрать протокол записи. Вернуть путь к HTML или None, если нет ни итогов, ни расшифровки."""
    meta, result, segments = load_parts(wav)
    if not result and not segments:
        return None
    html_path, md_path = protocol_paths(wav)
    html_path.write_text(build_html(wav, meta, result, segments), encoding="utf-8")
    md_path.write_text(build_markdown(wav, meta, result, segments), encoding="utf-8")
    return html_path
