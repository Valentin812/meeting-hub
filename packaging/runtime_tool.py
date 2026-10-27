# -*- coding: utf-8 -*-
"""Контроль совместимости «основы» (exe) и обновляемого кода.

Код программы обновляется отдельным архивом, но exe содержит только те библиотеки, которые были нужны
при его сборке. Если код начинает импортировать что-то новое, старый exe его не потянет — тогда нужно поднять
номер runtime, и такое обновление придёт обычным установщиком.

  python packaging/runtime_tool.py          — проверить (код возврата 1, если пора поднять runtime)
  python packaging/runtime_tool.py --write  — записать новый список и поднять runtime
"""
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "runtime.json"
EXCLUDE = {"launcher"}


def app_modules(root=ROOT):
    return sorted(p for p in Path(root).glob("*.py") if p.stem not in EXCLUDE)


def scan_imports(root=ROOT):
    ours = {p.stem for p in Path(root).glob("*.py")}
    found = set()
    for path in app_modules(root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return sorted(found - ours - {"__future__"})


def load(path=FILE):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"runtime": 0, "imports": []}


def missing(root=ROOT, path=FILE):
    """Что код импортирует, а в записанной основе нет."""
    return sorted(set(scan_imports(root)) - set(load(path).get("imports", [])))


def write(root=ROOT, path=FILE):
    cur = load(path)
    imports = sorted(set(scan_imports(root)) | set(cur.get("imports", [])))
    runtime = int(cur.get("runtime") or 0)
    if imports != cur.get("imports") or not runtime:
        runtime += 1
    Path(path).write_text(json.dumps({"runtime": runtime, "imports": imports}, ensure_ascii=False, indent=1) + "\n",
                          encoding="utf-8")
    return runtime


if __name__ == "__main__":
    if "--write" in sys.argv:
        print("runtime =", write())
    else:
        extra = missing()
        if extra:
            print("Новые импорты, которых нет в основе:", ", ".join(extra))
            print("Выполните: python packaging/runtime_tool.py --write  (и обновление придёт установщиком)")
            sys.exit(1)
        print("ok, runtime =", load().get("runtime"))
