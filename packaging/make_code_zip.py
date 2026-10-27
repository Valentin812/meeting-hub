# -*- coding: utf-8 -*-
"""Собрать архив обновления кода: python packaging/make_code_zip.py <версия> <папка-вывода>

Имя файла: MeetingHubCode-v<версия>-r<runtime>.zip. Внутри — все модули программы (*.py), runtime.json и code.json.
"""
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_tool  # noqa: E402


def build(version, out_dir, root=runtime_tool.ROOT):
    rt = runtime_tool.load(Path(root) / "runtime.json")
    runtime = int(rt.get("runtime") or 0)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"MeetingHubCode-v{version}-r{runtime}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for path in runtime_tool.app_modules(root):
            z.write(path, path.name)
        z.write(Path(root) / "runtime.json", "runtime.json")
        z.writestr("code.json", json.dumps({"version": version, "runtime": runtime}))
    return target


if __name__ == "__main__":
    print(build(sys.argv[1], sys.argv[2]))
