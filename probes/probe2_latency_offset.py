#!/usr/bin/env python3
"""probe2 — does Props.latencyOffsetNsec work as a live-editable, additive
static delay on a sink node, and does it compose sensibly with
combine.latency-compensate?

What it does:
  1. Inventories current nodes.
  2. For the first real sink, reads its current Props.latencyOffsetNsec.
  3. Sets latencyOffsetNsec to several values via pw-cli set-param, with a
     short sleep between each, and re-reads pw-dump each time.
  4. Restores the original value.

We are not (yet) doing an acoustic measurement here — we only want to verify:
  (a) pw-cli accepts the prop write,
  (b) pw-dump echoes it back as the effective value,
  (c) no restart / relink is required.

If (a)+(b)+(c) hold, offsets are live-editable and the DESIGN.md §3.1 plan
works. If they don't, Phase 1 must fall back to driving the offset via a
per-sink pw-loopback --delay (but editable without process restart — probably
via a running Python process that holds the loopback and uses SPA_PARAM to
update at runtime).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time


def run(*args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def pw_dump():
    r = run("pw-dump")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return []


def find_sink_node(dump, sink_name):
    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("node.name") == sink_name and props.get("media.class", "").startswith("Audio/Sink"):
            return obj
    return None


def read_latency_offset(dump, sink_name):
    """Return current Props.latencyOffsetNsec for a sink, or None."""
    node = find_sink_node(dump, sink_name)
    if not node:
        return None, None
    info = node.get("info") or {}
    props = info.get("props") or {}
    # latencyOffsetNsec is usually exposed as "node.latency-offset" OR
    # inside Params.Props. Check both.
    direct = props.get("node.latency-offset")
    if direct is not None:
        return node["id"], int(direct)
    # Fall back to Params.Props[0].latencyOffsetNsec
    for param in (info.get("params") or {}).get("Props", []) or []:
        if "latencyOffsetNsec" in param:
            return node["id"], int(param["latencyOffsetNsec"])
    return node["id"], 0


def set_latency_offset(node_id, nsec):
    """Set latencyOffsetNsec on a node via pw-cli."""
    # The pw-cli set-param syntax: set-param <id> Props '{ latencyOffsetNsec = N }'
    cmd = ["pw-cli", "set-param", str(node_id), "Props",
           '{ latencyOffsetNsec = %d }' % nsec]
    r = run(*cmd)
    return r.returncode, r.stderr.strip()


def main():
    dump = pw_dump()
    sinks = []
    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        name = props.get("node.name", "")
        if not props.get("media.class", "").startswith("Audio/Sink"):
            continue
        if name.startswith("audiomux") or "monitor" in name:
            continue
        sinks.append(name)

    if not sinks:
        print("[probe2] No real sinks found", file=sys.stderr)
        return 2

    sink = sinks[0]
    print(f"[probe2] testing sink: {sink}")

    node_id, original = read_latency_offset(dump, sink)
    if node_id is None:
        print(f"[probe2] could not find node for {sink}", file=sys.stderr)
        return 3
    print(f"[probe2] node id {node_id}, original latencyOffsetNsec = {original}")

    try:
        for want in (10_000_000, 50_000_000, 100_000_000, 0):
            rc, err = set_latency_offset(node_id, want)
            if rc != 0:
                print(f"[probe2] set {want}ns → FAILED rc={rc} err={err!r}")
            else:
                print(f"[probe2] set {want}ns → rc={rc}")
            time.sleep(0.4)
            dump2 = pw_dump()
            _, got = read_latency_offset(dump2, sink)
            delta_ms = (got - want) / 1_000_000 if got is not None else None
            print(f"           pw-dump now reports latencyOffsetNsec = {got}  "
                  f"(delta vs. requested: {delta_ms} ms)")
    finally:
        # Restore
        if original is not None:
            set_latency_offset(node_id, original)
            print(f"[probe2] restored latencyOffsetNsec = {original}")

    print()
    print("[probe2] INTERPRETATION:")
    print("  If all three set calls succeeded and pw-dump echoed them back")
    print("  with zero delta, we have a live-editable static-offset knob.")
    print("  Any nonzero delta or error means we fall back to a pw-loopback")
    print("  layer whose delay is updated by a daemon-held Python process.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
