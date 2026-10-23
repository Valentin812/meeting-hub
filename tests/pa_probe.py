# -*- coding: utf-8 -*-
"""Пробная запись через PyAudioWPatch (PortAudio): WASAPI-loopback и обычный микрофон."""
import sys, traceback, platform
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def note(title, text):
    print(f"::notice title={title}::" + str(text).replace("%", "%25").replace("\n", "%0A"), flush=True)


try:
    import pyaudiowpatch as pa
    import importlib.metadata as md
    note("pa-env", f"python {platform.python_version()} pyaudiowpatch {md.version('PyAudioWPatch')}")
    p = pa.PyAudio()
    apis = [p.get_host_api_info_by_index(i)["name"] for i in range(p.get_host_api_count())]
    wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
    spk = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
    loop = next((d for d in p.get_loopback_device_info_generator() if spk["name"] in d["name"]), None)
    res = {"apis": apis, "speaker": spk["name"], "loopback": loop and loop["name"]}
    if loop:
        s = p.open(format=pa.paFloat32, channels=loop["maxInputChannels"], rate=int(loop["defaultSampleRate"]),
                   input=True, input_device_index=loop["index"], frames_per_buffer=1024)
        import threading, math, struct
        out = p.open(format=pa.paFloat32, channels=1, rate=48000, output=True)
        tone = struct.pack("<4800f", *[0.3 * math.sin(2 * math.pi * 440 * i / 48000) for i in range(4800)])
        stop = threading.Event()
        threading.Thread(target=lambda: [out.write(tone) for _ in iter(lambda: stop.is_set(), True)], daemon=True).start()
        got = {}
        th = threading.Thread(target=lambda: got.setdefault("n", len(s.read(1024, exception_on_overflow=False))), daemon=True)
        th.start(); th.join(10); stop.set()
        res["loopback_read"] = got.get("n", "timeout")
    note("pa", res)
    import os
    sys.stdout.flush()
    os._exit(0)
except Exception:
    note("pa-failed", traceback.format_exc())
