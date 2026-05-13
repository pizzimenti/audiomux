#!/usr/bin/env python3
"""
audiomux_calibrate — Phase 2 calibrator (GCC-PHAT, sequential per sink)

Invoked as an isolated subprocess by audiomux-syncd's `check-sync` command.
For each active sink, plays one logarithmic sine sweep directly to that sink
(via `paplay --device`, bypassing the combine), captures the mic and the
sink's `.monitor` simultaneously, and computes the acoustic delay by
GCC-PHAT cross-correlating mic vs. monitor.

Why sequential rather than one combined sweep: when the sweep fans out to
every sink simultaneously, the loudest
speaker dominates the mic's capture. Because combine-sink feeds all backend
monitors with a single time-aligned stream, GCC-PHAT of mic vs. *any*
monitor locks onto the loudest-speaker peak — so every sink reports the
same (wrong) delay. Integration test on 2026-04-14 produced analog=19.8 ms
and BT=9.2 ms, which is clearly the analog peak leaking into BT's cross-
correlation. Playing to one sink at a time eliminates the crosstalk and
still costs only ~2 s per sink — well under the 5 s budget.

This also replaces the 0.2.0 approach of per-sink sequential paplay +
Goertzel energy detection, which suffered from ~85 ms jitter caused by
variable PipeWire startup latency. That problem
is solved here because GCC-PHAT measures mic-vs-monitor *relative* timing,
not absolute emit time — whatever PipeWire's per-invocation startup
latency is, both captures see the same sweep at the same sample offsets
within their own streams.

Argv protocol (daemon-compatible):
    audiomux_calibrate.py <mic_source> <sink_1> [<sink_2> ...]
Stdin:
    Ignored.
Stdout (single JSON line):
    {"ok": true, "method": "gcc-phat-sequential",
     "rounds": [{<sink>: {"delay_ms": N, "confidence": 0..1, "freq": 0,
                          "peak_ratio": N}, ...}]}
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time

try:
    import numpy as np
except ImportError:
    print(json.dumps({
        "ok": False,
        "error": ("numpy is required for Phase 2 calibration — install with "
                  "`python3 -m pip install --user numpy`"),
    }))
    sys.exit(0)

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from audiomux_sync_lib import (
    gcc_phat,
    gen_log_sweep,
    float_mono_to_s16_stereo_bytes,
    raw_s16le_stereo_to_mono,
)

# ── parameters ───────────────────────────────────────────────────────────────

RATE         = 48000
SWEEP_SEC    = 1.0
SWEEP_F0     = 80.0
SWEEP_F1     = 8000.0
SWEEP_AMP    = 0.55
WARMUP_SEC   = 1.5    # silent pre-roll — keeps BT speakers awake, primes codec
PRE_SILENCE  = 0.15   # additional silence directly before the sweep
POST_SILENCE = 0.80   # generous tail so BT codec+radio delay lands in capture
MAX_LAG_MS   = 1500   # large enough for pathological BT paths
PAPLAY_TIMEOUT_MARGIN_S = 4.0
REC_TERM_WAIT_S = 2.0


# ── process helpers ──────────────────────────────────────────────────────────

def _start_parecord(device: str, out_path: str):
    return subprocess.Popen(
        ["parecord",
         "--device", device,
         "--rate", str(RATE),
         "--channels", "2",
         "--format", "s16le",
         "--latency-msec", "20",
         "--raw",
         out_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _stop(proc: subprocess.Popen):
    if proc.poll() is None:
        proc.terminate()
    try:
        _, err = proc.communicate(timeout=REC_TERM_WAIT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err = proc.communicate()
    return err or b""


def _play_sweep_to(sink: str, payload: bytes, budget_s: float):
    """Play a stereo S16LE PCM blob directly to one hardware sink.

    Using `paplay --raw` because `pw-cat` reads stdin via libsndfile, which
    cannot parse unframed raw PCM.
    """
    proc = subprocess.Popen(
        ["paplay",
         "--device", sink,
         "--rate", str(RATE),
         "--channels", "2",
         "--format", "s16le",
         "--raw"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _, err = proc.communicate(input=payload, timeout=budget_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err = proc.communicate()
        return False, f"paplay timed out after {budget_s:.1f}s"
    if proc.returncode != 0:
        return False, (err.decode(errors="replace").strip()[:200]
                       or f"paplay exited {proc.returncode}")
    return True, ""


# ── measurement ──────────────────────────────────────────────────────────────

def _measure_sink(sink: str, mic: str, payload: bytes,
                  warmup_payload: bytes, tmpdir: str,
                  max_lag_samples: int):
    """Run one sweep into `sink`, capture mic + sink.monitor, return delay.

    Plays a silent warmup first so BT sinks don't go idle between the
    pre-roll and the sweep — a cold BT codec adds 60–200 ms of wake-up
    latency that shifts GCC-PHAT's peak.
    """
    safe = sink.replace(":", "_").replace(".", "_")
    mic_path = os.path.join(tmpdir, f"mic_{safe}.raw")
    mon_path = os.path.join(tmpdir, f"mon_{safe}.raw")

    # 1. Warm the sink — play silent audio (no recording yet). This wakes
    #    suspended BT links and primes codec buffers.
    _play_sweep_to(sink, warmup_payload,
                   WARMUP_SEC + PAPLAY_TIMEOUT_MARGIN_S)

    # 2. Start captures.
    mic_proc = _start_parecord(mic, mic_path)
    mon_proc = _start_parecord(f"{sink}.monitor", mon_path)
    time.sleep(0.30)  # let parecord settle

    # 3. Play the sweep.
    total_sec = PRE_SILENCE + SWEEP_SEC + POST_SILENCE
    ok, err = _play_sweep_to(
        sink, payload, total_sec + PAPLAY_TIMEOUT_MARGIN_S)

    # 4. Post-roll so BT codec + radio delay lands in the capture.
    time.sleep(0.30)

    stop_errs = []
    for p in (mic_proc, mon_proc):
        e = _stop(p)
        if e:
            text = e.decode(errors="replace").strip()
            if text:
                stop_errs.append(text[:200])

    if not ok:
        return {"delay_ms": 0, "confidence": 0.0, "freq": 0,
                "peak_ratio": 0.0, "error": err}

    mic_sig = raw_s16le_stereo_to_mono(mic_path)
    mon_sig = raw_s16le_stereo_to_mono(mon_path)
    if mic_sig.size < RATE // 2:
        return {"delay_ms": 0, "confidence": 0.0, "freq": 0,
                "peak_ratio": 0.0,
                "error": f"mic capture too short ({mic_sig.size} samples)"}
    if mon_sig.size < RATE // 2:
        return {"delay_ms": 0, "confidence": 0.0, "freq": 0,
                "peak_ratio": 0.0,
                "error": (f"monitor capture too short ({mon_sig.size} "
                          "samples) — sink likely suspended or muted")}

    n = min(mic_sig.size, mon_sig.size)
    lag_samples, peak_ratio = gcc_phat(
        mic_sig[:n], mon_sig[:n], max_lag_samples=max_lag_samples)
    delay_ms = lag_samples / RATE * 1000.0
    # Map (3, 53) → (0, 1) linearly; clip.
    confidence = max(0.0, min(1.0, (peak_ratio - 3.0) / 50.0))

    return {
        "delay_ms": round(delay_ms, 1),
        "confidence": round(confidence, 2),
        "peak_ratio": round(peak_ratio, 1),
        "freq": 0,
    }


# ── main ─────────────────────────────────────────────────────────────────────

def _emit(obj):
    print(json.dumps(obj))
    sys.exit(0)


def main():
    if len(sys.argv) < 3:
        _emit({"ok": False,
               "error": "usage: audiomux_calibrate.py <mic> <sink1> [sink2 ...]"})

    mic = sys.argv[1]
    sinks = list(dict.fromkeys(sys.argv[2:]))

    with contextlib.suppress(OSError, EOFError):
        sys.stdin.read()

    sweep = gen_log_sweep(duration_s=SWEEP_SEC, f0=SWEEP_F0, f1=SWEEP_F1,
                          rate=RATE, amplitude=SWEEP_AMP)
    silence_pre  = np.zeros(int(PRE_SILENCE  * RATE), dtype=np.float32)
    silence_post = np.zeros(int(POST_SILENCE * RATE), dtype=np.float32)
    wave = np.concatenate([silence_pre, sweep, silence_post])
    payload = float_mono_to_s16_stereo_bytes(wave)

    # Warmup: a very quiet dithered signal. Pure zeros would look like
    # silence to PipeWire's "was-corked" detection on some paths.
    warmup = (np.random.RandomState(0)
              .uniform(-1.0, 1.0, int(WARMUP_SEC * RATE))
              .astype(np.float32) * 0.001)
    warmup_payload = float_mono_to_s16_stereo_bytes(warmup)

    max_lag_samples = int(RATE * MAX_LAG_MS / 1000)
    debug_dir = os.environ.get("AUDIOMUX_CAL_DEBUG")

    results = {}
    tmp_ctx = (tempfile.TemporaryDirectory(prefix="audiomux-cal-")
               if not debug_dir else None)
    if tmp_ctx is not None:
        tmp = tmp_ctx.name
    else:
        os.makedirs(debug_dir, exist_ok=True)
        tmp = debug_dir
    try:
        for sink in sinks:
            results[sink] = _measure_sink(
                sink, mic, payload, warmup_payload, tmp, max_lag_samples)
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()

    _emit({
        "ok": True,
        "method": "gcc-phat-sequential",
        "rounds": [results],
        "debug_dir": tmp if debug_dir else None,
    })


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        sys.exit(0)
