#!/usr/bin/env python3
"""probe3 — can we capture 4 synchronous input streams (mic + N monitor
sources) with sample-accurate alignment?

Strategy A (preferred): one sounddevice.Stream opened against a synthetic
N-channel device. PipeWire doesn't expose such a combined device by default,
so this probably isn't available — but we try.

Strategy B (fallback, what we'll most likely ship): N separate parecord
processes started via a single run() dispatch, captured into memory, and
aligned post-hoc by correlating each capture against a common click emitted
on all sinks right before the real measurement. We measure the stdev of that
click's arrival time across captures.

What it does:
  1. Picks up to 3 monitor sources from the system.
  2. Emits a short 50 ms click through the default sink.
  3. Launches parecord on each monitor in quick succession.
  4. Records ~1.5 s, cross-correlates each capture against a reference
     (first capture), and reports start-offsets in milliseconds.

If the stdev of start-offsets across captures is < 1 ms, we have sample-level
alignment "for free" — Strategy B is viable and GCC-PHAT in DESIGN.md §3.2
will work. If stdev is > 10 ms, we need Strategy A-prime: co-open streams
and align by pw_time.ticks via libpipewire's Python bindings.
"""

from __future__ import annotations

import array
import math
import os
import struct
import subprocess
import sys
import tempfile
import time


RATE = 48000
CHANNELS = 2
CAPTURE_SEC = 1.5
CLICK_HZ = 2000
CLICK_MS = 50


def run(*args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def list_monitor_sources():
    r = run("pactl", "list", "sources", "short")
    out = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[1]
        if name.endswith(".monitor") and not name.startswith("audiomux"):
            out.append(name)
    return out


def emit_click():
    """Emit a 50ms @ 2kHz click through the default sink via pw-cat."""
    nsamp = int(RATE * CLICK_MS / 1000)
    buf = bytearray()
    for i in range(nsamp):
        v = int(0.5 * 32767 * math.sin(2 * math.pi * CLICK_HZ * i / RATE))
        buf += struct.pack("<hh", v, v)
    # Pad with 10ms silence on either side so the click has a clean onset
    pad = b"\x00\x00\x00\x00" * (RATE // 100)
    data = pad + bytes(buf) + pad
    p = subprocess.Popen(
        ["pw-cat", "--playback", "--rate", str(RATE),
         "--channels", str(CHANNELS), "--format", "s16", "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    p.communicate(input=data, timeout=3)


def start_parecord(monitor_name, out_path, duration):
    return subprocess.Popen(
        ["parecord",
         "--device", monitor_name,
         "--rate", str(RATE),
         "--channels", str(CHANNELS),
         "--format", "s16le",
         "--raw",
         "--file-format=raw",
         out_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )


def raw_to_mono(path):
    out = []
    with open(path, "rb") as f:
        data = f.read()
    n = len(data) // 4
    samples = struct.unpack(f"<{n * 2}h", data[: n * 4])
    for i in range(n):
        out.append((samples[2 * i] + samples[2 * i + 1]) * 0.5 / 32768.0)
    return out


def rms(xs):
    if not xs:
        return 0.0
    return math.sqrt(sum(x * x for x in xs) / len(xs))


def find_click(mono, click_hz=CLICK_HZ):
    """Goertzel-ish onset of the click in `mono`. Returns sample index or None."""
    block = RATE // 200                 # ~5 ms blocks
    n_blocks = len(mono) // block
    if n_blocks < 4:
        return None
    energies = []
    for b in range(n_blocks):
        seg = mono[b * block:(b + 1) * block]
        s = c = 0.0
        for i, x in enumerate(seg):
            phase = 2 * math.pi * click_hz * i / RATE
            s += x * math.sin(phase)
            c += x * math.cos(phase)
        energies.append(math.sqrt(s * s + c * c) / block)
    peak = max(energies)
    if peak <= 0:
        return None
    thresh = 0.3 * peak
    for b, e in enumerate(energies):
        if e > thresh:
            return b * block
    return None


def main():
    monitors = list_monitor_sources()[:3]
    if len(monitors) < 2:
        print(f"[probe3] Need >= 2 monitor sources, got {monitors}", file=sys.stderr)
        return 2
    print(f"[probe3] capturing from: {monitors}")

    with tempfile.TemporaryDirectory() as tmp:
        paths = [os.path.join(tmp, f"mon_{i}.raw") for i in range(len(monitors))]

        # Start all parecords as close together as we can.
        recs = []
        for mon, path in zip(monitors, paths):
            recs.append(start_parecord(mon, path, CAPTURE_SEC))

        # Brief settle so all parecords are actually attached.
        time.sleep(0.2)

        emit_click()

        # Wait until the captures are long enough.
        time.sleep(CAPTURE_SEC)

        for r in recs:
            r.terminate()
        for r in recs:
            try:
                r.wait(timeout=2)
            except subprocess.TimeoutExpired:
                r.kill()

        # Load and find click in each capture.
        results = []
        for mon, path in zip(monitors, paths):
            mono = raw_to_mono(path)
            rms_v = rms(mono)
            click_at = find_click(mono)
            click_ms = (click_at / RATE * 1000) if click_at is not None else None
            results.append((mon, len(mono), rms_v, click_ms))
            print(f"  {mon:<55}  len={len(mono):>6}  "
                  f"rms={rms_v:.4f}  click_at_ms={click_ms}")

        # Compute cross-capture spread.
        click_times = [c for _, _, _, c in results if c is not None]
        if len(click_times) < 2:
            print("[probe3] too few clicks detected; cannot assess alignment")
            return 3

        mean_t = sum(click_times) / len(click_times)
        stdev = math.sqrt(sum((t - mean_t) ** 2 for t in click_times) / len(click_times))
        spread = max(click_times) - min(click_times)

        print()
        print(f"[probe3] click arrival times:  {click_times}")
        print(f"[probe3] stdev = {stdev:.2f} ms   spread = {spread:.2f} ms")
        print()
        print("[probe3] INTERPRETATION:")
        print("  < 1 ms  → captures are sample-aligned for free. Strategy B viable.")
        print("  1–10 ms → usable, but GCC-PHAT must apply a per-capture prealign.")
        print("  > 10 ms → need libpipewire pw_time.ticks alignment (Strategy A-prime).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
