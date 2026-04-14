#!/usr/bin/env python3
"""probe1 — does module-combine-stream share ONE driver, or does each backend
sink remain its own driver with combine just mixing?

What it does:
  1. Inventories the current PipeWire node list.
  2. Loads module-combine-stream with combine.mode=sink, pointing at ALL
     non-virtual, non-monitor hardware sinks currently present.
  3. Starts a short silent stream into the combine sink to force the graph
     to actually link up and pick drivers.
  4. Samples `pw-top -b` three times and records which node IDs appear in
     the Driver column.
  5. Unloads the module and prints a verdict.

If one driver row owns all three sinks' processing during playback, the
combine-stream architecture proposed in DESIGN.md §3.1 works as intended.
If each sink retains its own driver row, we'll need node.group / node.link-group
gymnastics (or the zita-resampler escape hatch).

Safe to run on a live system: module is unloaded in a finally block.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time


VIRTUAL_NAME = "probe1_combine"


def run(*args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def list_sinks():
    r = run("pactl", "list", "sinks", "short")
    out = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[1]
        if name.startswith("audiomux") or name.startswith("probe"):
            continue
        if "monitor" in name:
            continue
        out.append(name)
    return out


def pw_top_snapshot():
    """Run pw-top in batch mode for 3 samples, return raw text."""
    r = run("pw-top", "-b", "-n", "3")
    return r.stdout


def unload_via_pw_cli(module_id):
    if module_id:
        run("pw-cli", "destroy", str(module_id))


def main():
    sinks = list_sinks()
    if len(sinks) < 2:
        print(f"[probe1] Need at least 2 real sinks, found {sinks}", file=sys.stderr)
        return 2

    print(f"[probe1] real sinks available: {sinks}")

    # Load combine-stream via pw-cli (native PipeWire module interface).
    # pactl load-module is PulseAudio-compat only and doesn't see combine-stream.
    config = (
        '{ '
        'combine.mode = sink '
        f'node.name = "{VIRTUAL_NAME}" '
        'node.description = "AudioMuxProbe1" '
        'combine.latency-compensate = true '
        'audio.position = [ FL FR ] '
        '}'
    )
    load = run("pw-cli", "load-module", "libpipewire-module-combine-stream", config)
    if load.returncode != 0:
        print(f"[probe1] pw-cli load-module failed rc={load.returncode}",
              file=sys.stderr)
        print(f"         stdout: {load.stdout!r}", file=sys.stderr)
        print(f"         stderr: {load.stderr!r}", file=sys.stderr)
        return 3
    # pw-cli prints something like: "Loaded module ... (id=42)"
    module_id = None
    for tok in load.stdout.split():
        if tok.isdigit():
            module_id = int(tok)
            break
    print(f"[probe1] loaded combine-stream id={module_id}  (raw stdout: {load.stdout.strip()!r})")

    try:
        time.sleep(0.5)

        # Force the graph to link by playing very quiet noise into the combine
        # sink. pw-cat with a 2s null source → the combine sink.
        print(f"[probe1] starting silent stream into {VIRTUAL_NAME}")
        play = subprocess.Popen(
            ["pw-cat", "--playback", "--target", VIRTUAL_NAME,
             "--rate", "48000", "--channels", "2", "--format", "s16",
             "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Feed ~2s of near-silence: low-level random so PipeWire actually runs
        # mixing code rather than short-circuiting.
        try:
            chunk = bytes([0x01, 0x00] * 4800)   # 4800 stereo samples @ 48k = 50ms
            for _ in range(40):                  # 40 * 50ms = 2s
                play.stdin.write(chunk)
                play.stdin.flush()
                time.sleep(0.04)
        except BrokenPipeError:
            pass

        # While playback is still running, snapshot pw-top.
        print("[probe1] sampling pw-top ...")
        top = pw_top_snapshot()
        print("----- pw-top snapshot -----")
        print(top)
        print("---------------------------")

        # pw-dump the combine node to see its links
        print("[probe1] pw-dump (nodes) filtered to combine/sinks ...")
        dump = run("pw-dump")
        try:
            parsed = json.loads(dump.stdout)
        except json.JSONDecodeError:
            parsed = []

        for obj in parsed:
            if obj.get("type") != "PipeWire:Interface:Node":
                continue
            info = obj.get("info") or {}
            props = info.get("props") or {}
            name = props.get("node.name", "")
            if (name.startswith("probe1") or name in sinks
                    or "combine" in name.lower()):
                driver_id = props.get("node.driver-id", "?")
                own_driver = props.get("node.driver", "?")
                link_group = props.get("node.link-group", "")
                print(f"  node {obj.get('id'):>4}  "
                      f"name={name:<55}  driver={own_driver}  "
                      f"driver-id={driver_id}  link-group={link_group}")

        try:
            play.stdin.close()
        except Exception:
            pass
        try:
            play.wait(timeout=2)
        except subprocess.TimeoutExpired:
            play.terminate()
            play.wait(timeout=1)

    finally:
        unload_via_pw_cli(module_id)
        print(f"[probe1] unloaded module {module_id}")

    print()
    print("[probe1] INTERPRETATION:")
    print("  Look in the pw-top snapshot at rows whose NAME matches the three")
    print("  hardware sinks. If they all share the same DRIVER column entry")
    print("  (i.e. one row is the driver and the others are followers of it),")
    print("  combine-stream is giving us the unified-graph behavior DESIGN.md")
    print("  §3.1 assumes. If each sink is its own driver, we need a different")
    print("  static-offset story.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
