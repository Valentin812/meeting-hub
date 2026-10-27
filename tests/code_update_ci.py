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


def fail(msg, **ctx):
    text = msg + " | " + json.dumps(ctx, ensure_ascii=False)
    print(f"::error title=code update test::{text}")
    raise SystemExit(text)


def run():
    info = data / "code_info.json"
    info.unlink(missing_ok=True)
    proc = subprocess.run([exe, "--code-info"], env=env, timeout=120, capture_output=True)
    end = time.time() + 40                       # при откате запуск продолжает уже новый процесс
    while not info.exists() and time.time() < end:
        time.sleep(0.5)
    if not info.exists():
        fail("exe не записал code_info.json", code=proc.returncode, out=proc.stdout[-300:].decode("utf-8", "replace"),
             err=proc.stderr[-300:].decode("utf-8", "replace"), data=str(data),
             files=sorted(p.name for p in data.iterdir()))
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


def main():
    base = run()
    print("основа:", base)
    if not (base["base_runtime"] and base["base_version"]):
        fail("launcher не передал сведения об основе", **base)
    if base["version"] != base["base_version"]:
        fail("без обновления должен работать вшитый код", **base)
    if str(data) in base["file"]:
        fail("вшитый код загружен из папки данных", **base)

    install("99.0.0")
    now = run()
    print("с обновлением кода:", now)
    if now["version"] != "99.0.0" or str(data / "code" / "99.0.0") not in now["file"]:
        fail("код из папки обновления не подхватился", **now)

    install("99.0.1", broken=True, pending=True, previous="99.0.0")
    after = run()
    print("после битого обновления:", after)
    if after["version"] != "99.0.0":
        fail("битое обновление не откатилось на предыдущее", **after)

    install("99.0.2", runtime=int(base["base_runtime"]) + 5)
    skipped = run()
    print("обновление требует новой основы:", skipped)
    if skipped["version"] != base["base_version"]:
        fail("обновление с чужим runtime не должно применяться", **skipped)
    print("OK")


try:
    main()
except SystemExit:
    raise
except BaseException as e:                          # любой сбой должен быть виден в аннотациях CI
    import traceback
    text = traceback.format_exc().replace("\n", " | ")[-900:]
    print(f"::error title=code update test::{type(e).__name__}: {text}")
    raise SystemExit(1)
