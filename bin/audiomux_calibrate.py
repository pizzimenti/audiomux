#!/usr/bin/env python3
"""
audiomux_calibrate — acoustic delay measurement via sink-monitor reference

Single mic session. For each sink sequentially:
  - Record sink.monitor + mic simultaneously (already running)
  - Play chirps through that sink
  - Cross-correlate monitor vs mic → acoustic delay

Usage: audiomux_calibrate.py <mic_device> <sink1> [sink2] ...
"""

import json
import math
import os
import signal
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RATE = 48000
CHANNELS = 2
S16_FRAME = CHANNELS * 2

WARMUP = 0.5       # dither warmup per sink
CHIRP_DURATION = 0.2
CHIRP_F0 = 800
CHIRP_F1 = 6000
CHIRP_AMPLITUDE = 0.7
CHIRP_GAP = 0.3    # gap between chirps
TAIL = 0.8
DITHER_AMPLITUDE = 0.02
N_CHIRPS = 3


def _generate_chirp():
    n = int(CHIRP_DURATION * RATE)
    samples = []
    for i in range(n):
        t = i / RATE
        freq = CHIRP_F0 + (CHIRP_F1 - CHIRP_F0) * t / CHIRP_DURATION
        phase = 2 * math.pi * (CHIRP_F0 * t + 0.5 * (CHIRP_F1 - CHIRP_F0) * t * t / CHIRP_DURATION)
        samples.append(CHIRP_AMPLITUDE * math.sin(phase))
    return samples


def _mono_to_raw(samples):
    raw = bytearray()
    for s in samples:
        val = int(max(-1.0, min(1.0, s)) * 32767)
        raw += struct.pack("<hh", val, val)
    return bytes(raw)


def _raw_to_mono(raw_bytes):
    samples = []
    for i in range(0, len(raw_bytes) - S16_FRAME + 1, S16_FRAME):
        l, r = struct.unpack_from("<hh", raw_bytes, i)
        samples.append((l + r) / 2.0)
    return samples


def _downsample(samples, factor):
    out = []
    for i in range(0, len(samples) - factor + 1, factor):
        out.append(sum(samples[i:i + factor]) / factor)
    return out


def _xcorr_lag(ref, mic):
    """Cross-correlate at 4kHz, return (lag_ms, confidence)."""
    DS = 12  # 48000 → 4000Hz, Nyquist 2000Hz, keeps most of the chirp
    ref_ds = _downsample(ref, DS)
    mic_ds = _downsample(mic, DS)
    ds_rate = RATE // DS
    max_lag = int(1.0 * ds_rate)  # 1 second

    n = min(len(ref_ds), len(mic_ds))
    if n < 20:
        return 0.0, 0.0

    mean_r = sum(ref_ds[:n]) / n
    mean_m = sum(mic_ds[:n]) / n
    r = [x - mean_r for x in ref_ds[:n]]
    m = [x - mean_m for x in mic_ds[:n]]
    er = math.sqrt(sum(x * x for x in r))
    em = math.sqrt(sum(x * x for x in m))
    if er == 0 or em == 0:
        return 0.0, 0.0

    best_lag, best_corr = 0, -1.0
    for lag in range(0, min(max_lag, n - 10)):
        s = sum(a * b for a, b in zip(r[:n - lag], m[lag:n]))
        c = s / (er * em)
        if c > best_corr:
            best_corr = c
            best_lag = lag

    return best_lag / ds_rate * 1000, best_corr


class Recorder:
    """Long-lived parecord process that accumulates raw audio."""
    def __init__(self, device):
        self.device = device
        self._proc = None
        self._raw = bytearray()
        self._thread = None
        self._stop = False

    def start(self):
        cmd = [
            "parecord", "--device", self.device,
            "--rate", str(RATE), "--channels", str(CHANNELS),
            "--format", "s16le", "--raw", "--latency-msec", "10",
        ]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL)
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self):
        while not self._stop:
            chunk = self._proc.stdout.read(4096)
            if not chunk:
                break
            self._raw.extend(chunk)

    def stop(self):
        self._stop = True
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        if self._thread:
            self._thread.join(timeout=2)

    def snapshot(self):
        """Return current sample count (for marking positions)."""
        return len(self._raw) // S16_FRAME

    def get_mono(self, start_sample=0, end_sample=None):
        """Extract a slice as mono float samples."""
        start_byte = start_sample * S16_FRAME
        end_byte = end_sample * S16_FRAME if end_sample else len(self._raw)
        return _raw_to_mono(bytes(self._raw[start_byte:end_byte]))


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"error": "usage: calibrate <mic> <sink1> [sink2] ..."}))
        sys.exit(1)

    mic_device = sys.argv[1]
    sink_names = sys.argv[2:]

    chirp_raw = _mono_to_raw(_generate_chirp())

    # Dither for warmup
    import random
    dither_n = int(WARMUP * RATE)
    dither_raw = _mono_to_raw([DITHER_AMPLITUDE * (random.random() * 2 - 1)
                               for _ in range(dither_n)])

    # Start ONE mic recorder for the entire session
    mic_rec = Recorder(mic_device)
    mic_rec.start()
    time.sleep(0.1)  # let it settle

    results = {}

    for sink in sink_names:
        # Start monitor recorder for this sink
        mon_rec = Recorder(f"{sink}.monitor")
        mon_rec.start()
        time.sleep(0.05)

        # Warm up sink with dither
        _play_raw(sink, dither_raw)
        time.sleep(0.05)

        # Play chirps and record positions
        measurements = []
        for chirp_num in range(N_CHIRPS):
            # Mark positions in both recordings
            mon_start = mon_rec.snapshot()
            mic_start = mic_rec.snapshot()

            # Play chirp
            _play_raw(sink, chirp_raw)

            # Wait for acoustic propagation
            time.sleep(TAIL)

            # Grab the recorded segments
            mon_seg = mon_rec.get_mono(mon_start)
            mic_seg = mic_rec.get_mono(mic_start)

            if len(mon_seg) > 100 and len(mic_seg) > 100:
                lag_ms, confidence = _xcorr_lag(mon_seg, mic_seg)
                measurements.append((lag_ms, confidence))
            else:
                measurements.append((0, 0))

            time.sleep(CHIRP_GAP)

        # Stop this sink's monitor
        mon_rec.stop()

        # Average good measurements
        good = [(l, c) for l, c in measurements if c > 0.2 and l >= 0]
        if good:
            avg_lag = sum(l for l, _ in good) / len(good)
            avg_conf = sum(c for _, c in good) / len(good)
            results[sink] = {
                "delay_ms": round(avg_lag, 1),
                "confidence": round(avg_conf, 2),
                "probes": len(good),
                "raw": [{"delay_ms": round(l, 1), "confidence": round(c, 2)}
                        for l, c in measurements],
            }
        else:
            results[sink] = {
                "delay_ms": 0, "confidence": 0, "probes": 0,
                "error": "no signal detected",
                "raw": [{"delay_ms": round(l, 1), "confidence": round(c, 2)}
                        for l, c in measurements],
            }

    # Stop mic
    mic_rec.stop()

    rounds = [{sink: data} for sink, data in results.items()]
    print(json.dumps({"ok": True, "rounds": rounds}))


def _play_raw(device, raw_data):
    cmd = [
        "paplay", "--device", device,
        "--rate", str(RATE), "--channels", str(CHANNELS),
        "--format", "s16le", "--raw",
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    proc.stdin.write(raw_data)
    proc.stdin.close()
    proc.wait()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    signal.signal(signal.SIGALRM, lambda *_: sys.exit(1))
    signal.alarm(45)
    main()
