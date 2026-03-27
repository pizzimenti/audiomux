#!/usr/bin/env python3
"""
audiomux-source.py — AudioMux state helper for the KDE plasmoid

Usage:
  audiomux-source.py                              print JSON state
  audiomux-source.py set-active-sinks   n1[,n2...]  set active sinks
  audiomux-source.py set-active-sources n1[,n2...]  set active sources
  audiomux-source.py unbond-all                   reset to primary devices
  audiomux-source.py set-sink-vol    name pct     set sink volume
  audiomux-source.py set-source-vol  name pct     set source volume
  audiomux-source.py set-sink-offset   name ms    set per-sink latency offset
  audiomux-source.py set-source-offset name ms    set per-source latency offset

Sink routing:
  Virtual null sink (audiomux_virtual) is the default; one module-loopback
  per active real sink reads from audiomux_virtual.monitor.
  Per-device latency offsets are stored in ~/.config/audiomux-offsets.json
  and applied when the loopback is (re)created.
"""

import sys, os, json, subprocess
import pulsectl

PRIMARY_SINK        = "alsa_output.pci-0000_03_00.6.analog-stereo"
PRIMARY_SOURCE      = "alsa_input.pci-0000_03_00.6.analog-stereo"
VIRTUAL_SINK_NAME   = "audiomux_virtual"
COMBINE_SOURCE_NAME = "audiomux_combined_source"
STATE_FILE          = f"/run/user/{os.getuid()}/audiomux.json"
OFFSETS_FILE        = os.path.expanduser("~/.config/audiomux-offsets.json")

BASE_LATENCY_MS = 200   # loopback buffer before per-device offset


# ── offset config ─────────────────────────────────────────────────────────────

def load_offsets():
    try:
        with open(OFFSETS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_offsets(offsets):
    with open(OFFSETS_FILE, "w") as f:
        json.dump(offsets, f, indent=2)


# ── state file ────────────────────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        pass
    return {
        "virtual_sink_module_id": None,
        "loopback_modules": {},
        "active_sinks": None,
        "source_module_id": None,
        "active_sources": None,
    }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ── pactl wrapper ─────────────────────────────────────────────────────────────

def pactl(*args):
    result = subprocess.run(["pactl"] + [str(a) for a in args],
                            capture_output=True, text=True)
    return result.stdout.strip()


# ── queries ───────────────────────────────────────────────────────────────────

def _live_loopback_sinks():
    """Return set of sink names that currently have an audiomux loopback module."""
    result = subprocess.run(["pactl", "list", "modules", "short"],
                            capture_output=True, text=True)
    sinks = set()
    for line in result.stdout.splitlines():
        if "module-loopback" not in line:
            continue
        if f"source={VIRTUAL_SINK_NAME}.monitor" not in line:
            continue
        for part in line.split():
            if part.startswith("sink="):
                sinks.add(part[5:])
    return sinks


def get_state():
    saved   = load_state()
    offsets = load_offsets()

    with pulsectl.Pulse("audiomux-source") as pulse:
        server = pulse.server_info()

        has_virtual = any(s.name == VIRTUAL_SINK_NAME for s in pulse.sink_list())

        if has_virtual and server.default_sink_name == VIRTUAL_SINK_NAME:
            # Virtual sink is active — ground truth is the live loopbacks
            active_sink_names = _live_loopback_sinks()
            if not active_sink_names:
                active_sink_names = {PRIMARY_SINK}
        else:
            # Virtual sink is stale or bypassed — trust the current default sink
            active_sink_names = {server.default_sink_name}

        has_combine_source = any(
            s.name == COMBINE_SOURCE_NAME for s in pulse.source_list())
        if has_combine_source and saved.get("active_sources"):
            active_source_names = set(saved["active_sources"])
        else:
            active_source_names = {server.default_source_name}

        sinks = []
        for sink in pulse.sink_list():
            if sink.name in (VIRTUAL_SINK_NAME,) or "monitor" in sink.name:
                continue
            sinks.append({
                "name":        sink.name,
                "description": sink.description,
                "volume":      round(pulse.volume_get_all_chans(sink) * 100),
                "is_primary":  sink.name == PRIMARY_SINK,
                "active":      sink.name in active_sink_names,
                "offset":      offsets.get(sink.name, 0),
            })

        sources = []
        for source in pulse.source_list():
            if source.name in (COMBINE_SOURCE_NAME,) or "monitor" in source.name:
                continue
            sources.append({
                "name":        source.name,
                "description": source.description,
                "volume":      round(pulse.volume_get_all_chans(source) * 100),
                "is_primary":  source.name == PRIMARY_SOURCE,
                "active":      source.name in active_source_names,
                "offset":      offsets.get(source.name, 0),
            })

    return {"sinks": sinks, "sources": sources}


# ── sink routing (virtual + loopbacks) ────────────────────────────────────────

def _ensure_virtual_sink(state):
    if state.get("virtual_sink_module_id") is None:
        mid = pactl("load-module", "module-null-sink",
                    f"sink_name={VIRTUAL_SINK_NAME}",
                    "sink_properties=device.description=AudioMux")
        if mid.isdigit():
            state["virtual_sink_module_id"] = int(mid)
            pactl("set-default-sink", VIRTUAL_SINK_NAME)


def _add_loopback(state, sink_name):
    offsets = load_offsets()
    latency = max(50, BASE_LATENCY_MS + offsets.get(sink_name, 0))
    mid = pactl("load-module", "module-loopback",
                f"source={VIRTUAL_SINK_NAME}.monitor",
                f"sink={sink_name}",
                f"latency_msec={latency}")
    if mid.isdigit():
        state.setdefault("loopback_modules", {})[sink_name] = int(mid)


def _remove_loopback(state, sink_name):
    mid = state.get("loopback_modules", {}).pop(sink_name, None)
    if mid is not None:
        pactl("unload-module", mid)


def set_active_sinks(names):
    if not names:
        names = [PRIMARY_SINK]

    state = load_state()
    current_loopbacks = set(state.get("loopback_modules", {}).keys())
    names_set = set(names)

    _ensure_virtual_sink(state)

    for sink_name in current_loopbacks - names_set:
        _remove_loopback(state, sink_name)

    for sink_name in names_set - current_loopbacks:
        _add_loopback(state, sink_name)

    state["active_sinks"] = list(names_set)
    save_state(state)


def set_sink_offset(name, offset_ms):
    offsets = load_offsets()
    offsets[name] = offset_ms
    save_offsets(offsets)
    # If this sink has an active loopback, reload it with the new latency
    state = load_state()
    if name in state.get("loopback_modules", {}):
        _remove_loopback(state, name)
        _add_loopback(state, name)
        save_state(state)


# ── source routing (combine) ──────────────────────────────────────────────────

def set_active_sources(names):
    if not names:
        names = [PRIMARY_SOURCE]

    state = load_state()
    if state.get("source_module_id") is not None:
        pactl("unload-module", state["source_module_id"])
        state["source_module_id"] = None

    if len(names) == 1:
        pactl("set-default-source", names[0])
    else:
        mid = pactl("load-module", "module-combine-source",
                    f"source_name={COMBINE_SOURCE_NAME}",
                    f"master={names[0]}",
                    f"slaves={','.join(names)}")
        if mid.isdigit():
            state["source_module_id"] = int(mid)
            pactl("set-default-source", COMBINE_SOURCE_NAME)

    state["active_sources"] = list(names)
    save_state(state)


def set_source_offset(name, offset_ms):
    offsets = load_offsets()
    offsets[name] = offset_ms
    save_offsets(offsets)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    if not args:
        print(json.dumps(get_state()))
        return

    action = args[0]

    if action == "set-active-sinks":
        names = args[1].split(",") if len(args) > 1 and args[1] else []
        set_active_sinks(names)
    elif action == "set-active-sources":
        names = args[1].split(",") if len(args) > 1 and args[1] else []
        set_active_sources(names)
    elif action == "unbond-all":
        set_active_sinks([PRIMARY_SINK])
        set_active_sources([PRIMARY_SOURCE])
    elif action == "set-sink-vol" and len(args) >= 3:
        pactl("set-sink-volume", args[1], f"{args[2]}%")
    elif action == "set-source-vol" and len(args) >= 3:
        pactl("set-source-volume", args[1], f"{args[2]}%")
    elif action == "set-sink-offset" and len(args) >= 3:
        set_sink_offset(args[1], int(args[2]))
    elif action == "set-source-offset" and len(args) >= 3:
        set_source_offset(args[1], int(args[2]))


if __name__ == "__main__":
    main()
