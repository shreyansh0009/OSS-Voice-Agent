"""
ring_tone.py — Generates a phone ring tone as raw 16-bit PCM.

Produces a dual-tone ring (400 Hz + 425 Hz — Indian PSTN standard) with a
standard ring cadence (0.4s on / 0.2s off repeating) using pure Python stdlib.
No external dependencies.
"""
from __future__ import annotations

import math
import struct


def generate_ring_pcm(duration_secs: float = 3.0, sample_rate: int = 8000) -> bytes:
    """
    Generate a dual-tone phone ring tone as signed 16-bit little-endian PCM.

    Parameters
    ----------
    duration_secs : float
        Total duration of ring audio to generate.
    sample_rate : int
        Sample rate in Hz (must match AudioSocket — default 8000).

    Returns
    -------
    bytes
        Raw PCM bytes ready to split into 320-byte AudioSocket frames.
    """
    # Ring cadence: 0.4s tone on, 0.2s silence, repeat
    ON_SECS  = 0.4
    OFF_SECS = 0.2
    PERIOD   = ON_SECS + OFF_SECS  # 0.6s per cycle

    n_samples = int(sample_rate * duration_secs)
    buf = bytearray(n_samples * 2)  # 2 bytes per int16 sample

    for i in range(n_samples):
        t = i / sample_rate
        phase = t % PERIOD
        if phase < ON_SECS:
            # Dual-tone: 400 Hz + 425 Hz, 0.35 each → 0.70 total (safe headroom)
            val = (0.35 * math.sin(2 * math.pi * 400 * t)
                 + 0.35 * math.sin(2 * math.pi * 425 * t))
            sample = int(val * 32767)
        else:
            sample = 0  # silence between rings

        struct.pack_into('<h', buf, i * 2, sample)

    return bytes(buf)
