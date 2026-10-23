# -*- coding: utf-8 -*-
"""Играть тон 440 Гц на динамиках по умолчанию N секунд (для проверки записи звука компьютера в CI)."""
import sys
import numpy as np
import soundcard as sc

seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 10
tone = (0.3 * np.sin(2 * np.pi * 440 * np.arange(48000) / 48000)).astype("float32")
with sc.default_speaker().player(samplerate=48000, channels=1) as p:
    for _ in range(seconds):
        p.play(tone)
