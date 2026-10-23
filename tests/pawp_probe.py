# -*- coding: utf-8 -*-
"""Пробный запуск PyAudioWPatch (PortAudio + WASAPI loopback) на CI."""
import sys, traceback, platform
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def note(title, text):
    print(f"::notice title={title}::" + str(text).replace("%", "%25").replace("\n", "%0A"))


try:
    import pyaudiowpatch as pyaudio
    import importlib.metadata as md
    p = pyaudio.PyAudio()
    wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    spk = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
    loop = None
    for d in p.get_loopback_device_info_generator():
        if spk["name"] in d["name"]:
            loop = d
    res = {"ver": md.version("PyAudioWPatch"), "py": platform.python_version(), "speaker": spk["name"],
           "loopback": loop and loop["name"]}
    if loop:
        rate = int(loop["defaultSampleRate"]); ch = int(loop["maxInputChannels"])
        st = p.open(format=pyaudio.paFloat32, channels=ch, rate=rate, input=True,
                    input_device_index=loop["index"], frames_per_buffer=rate // 10)
        data = st.read(rate // 10, exception_on_overflow=False)
        st.close()
        res["loop_read"] = f"{len(data)} bytes {rate} Hz {ch} ch"
    p.terminate()
    note("pyaudiowpatch", res)
except Exception:
    note("pyaudiowpatch", traceback.format_exc())
