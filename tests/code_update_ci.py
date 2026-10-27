# -*- coding: utf-8 -*-
"""Проверка на собранном exe: новый код подхватывается без замены exe, битый откатывается.

python tests/code_update_ci.py <путь к MeetingHub.exe>
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
exe = sys.argv[1]
data = Path(tempfile.mkdtemp(prefix="mh_code_"))
env = dict(os.environ, MEETINGHUB_DATA=str(data))


def run():
    info = data / "code_info.json"
    info.unlink(missing_ok=True)
    subprocess.run([exe, "--code-info"], env=env, timeout=120)
    end = time.time() + 40                       # при откате запуск продолжает уже новый процесс
    while not info.exists() and time.time() < end:
        time.sleep(0.5)
    if not info.exists():
        raise SystemExit("exe не записал code_info.json")
    return json.loads(info.read_text(encoding="utf-8"))


def install(version, runtime=1, broken=False, pending=False, previous="", attempts=0):
    folder = data / "code" / version
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    for p in ROOT.glob("*.py"):
        if p.stem != "launcher":
            shutil.copy(p, folder / p.name)
    text = (folder / "app.py").read_text(encoding="utf-8")
    text = re.sub(r'^APP_VERSION = ".*"', f'APP_VERSION = "{version}"', text, count=1, flags=re.M)
    if broken:
        text = "raise RuntimeError('битое обновление')\n" + text
    (folder / "app.py").write_text(text, encoding="utf-8")
    (folder / "code.json").write_text(json.dumps({"version": version, "runtime": runtime}))
    (data / "code" / "state.json").write_text(json.dumps(
        {"active": version, "previous": previous, "pending": pending, "attempts": attempts}))


base = run()
print("основа:", base)
assert base["base_runtime"] and base["base_version"], base
assert base["version"] == base["base_version"], "без обновления должен работать вшитый код"
assert str(data) not in base["file"]

install("99.0.0")
now = run()
print("с обновлением кода:", now)
assert now["version"] == "99.0.0", "код из папки обновления не подхватился"
assert str(data / "code" / "99.0.0") in now["file"], now["file"]

install("99.0.1", broken=True, pending=True, previous="99.0.0")
assert (data / "code" / "99.0.0").exists()          # прежняя версия на месте
after = run()
print("после битого обновления:", after)
assert after["version"] == "99.0.0", "битое обновление не откатилось на предыдущее"

install("99.0.2", runtime=int(base["base_runtime"]) + 5)
skipped = run()
print("обновление требует новой основы:", skipped)
assert skipped["version"] == base["base_version"], "обновление с чужим runtime не должно применяться"
print("OK")
