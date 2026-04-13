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

When the audiomux-syncd daemon is running, commands are forwarded to it
via a Unix domain socket.  When the daemon is not running, falls back to
direct pactl graph management (legacy mode).
"""

import sys, os, json, socket, subprocess
import pulsectl

PRIMARY_SINK        = "alsa_output.pci-0000_03_00.6.analog-stereo"
PRIMARY_SOURCE      = "alsa_input.pci-0000_03_00.6.analog-stereo"
VIRTUAL_SINK_NAME   = "audiomux_virtual"
COMBINE_SOURCE_NAME = "audiomux_combined_source"
STATE_FILE          = f"/run/user/{os.getuid()}/audiomux.json"
SOCKET_PATH         = f"/run/user/{os.getuid()}/audiomux-syncd.sock"
OFFSETS_FILE        = os.path.expanduser("~/.config/audiomux-offsets.json")

BASE_LATENCY_MS = 200   # loopback buffer before per-device offset


# ── daemon IPC ───────────────────────────────────────────────────────────────

def _daemon_request(cmd_dict):
    """Send a JSON command to the daemon socket, return the response dict.

    Raises ConnectionRefusedError or FileNotFoundError if the daemon is not
    running, which the caller uses to fall back to legacy mode.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(30.0)
        sock.connect(SOCKET_PATH)
        sock.sendall(json.dumps(cmd_dict).encode() + b"\n")
        # Read until newline
        buf = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                break
        return json.loads(buf.split(b"\n")[0])
    finally:
        sock.close()


def _daemon_available():
    """Check if the daemon socket exists and is connectable."""
    try:
        resp = _daemon_request({"cmd": "ping"})
        return resp.get("ok", False)
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return False


# ── offset config ────────────────────────────────────────────────────────────

def load_offsets():
    try:
        with open(OFFSETS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_offsets(offsets):
    with open(OFFSETS_FILE, "w") as f:
        json.dump(offsets, f, indent=2)


# ── state file ───────────────────────────────────────────────────────────────

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


# ── pactl wrapper ────────────────────────────────────────────────────────────

def pactl(*args):
    result = subprocess.run(["pactl"] + [str(a) for a in args],
                            capture_output=True, text=True)
    return result.stdout.strip()


def _service_property(service_name, property_name):
    result = subprocess.run(
        ["systemctl", "--user", "show", service_name, f"-p{property_name}"],
        capture_output=True, text=True)
    if result.returncode != 0:
        return ""
    prefix = f"{property_name}="
    for line in result.stdout.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    return ""


# ── queries (legacy mode) ───────────────────────────────────────────────────

def _live_loopback_modules():
    """Return mapping of sink names to live audiomux loopback module ids."""
    result = subprocess.run(["pactl", "list", "modules", "short"],
                            capture_output=True, text=True)
    modules = {}
    for line in result.stdout.splitlines():
        if "module-loopback" not in line:
            continue
        if f"source={VIRTUAL_SINK_NAME}.monitor" not in line:
            continue
        parts = line.split()
        if not parts:
            continue
        module_id = parts[0]
        if not module_id.isdigit():
            continue
        for part in line.split():
            if part.startswith("sink="):
                modules[part[5:]] = int(module_id)
    return modules


def _live_loopback_sinks():
    """Return set of sink names that currently have an audiomux loopback module."""
    return set(_live_loopback_modules())


def _has_virtual_sink():
    result = subprocess.run(["pactl", "list", "sinks", "short"],
                            capture_output=True, text=True)
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) > 1 and parts[1] == VIRTUAL_SINK_NAME:
            return True
    return False


def _live_virtual_sink_module_id():
    result = subprocess.run(["pactl", "list", "modules", "short"],
                            capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if "module-null-sink" not in line:
            continue
        if f"sink_name={VIRTUAL_SINK_NAME}" not in line:
            continue
        parts = line.split()
        if parts and parts[0].isdigit():
            return int(parts[0])
    return None


def _preferred_name(primary_name, available_names, fallback_name=None):
    if primary_name in available_names:
        return primary_name
    if fallback_name in available_names:
        return fallback_name
    return next(iter(available_names), None)


def _reconcile_state(state, sink_names, source_names, default_sink_name,
                     default_source_name):
    changed = False
    stale_sinks_removed = False

    loopback_modules = state.setdefault("loopback_modules", {})
    live_loopbacks = _live_loopback_modules()

    for sink_name, mid in list(live_loopbacks.items()):
        if sink_name in sink_names:
            continue
        pactl("unload-module", mid)
        live_loopbacks.pop(sink_name, None)
        loopback_modules.pop(sink_name, None)
        changed = True
        stale_sinks_removed = True

    for sink_name in list(loopback_modules):
        if sink_name in sink_names:
            continue
        mid = loopback_modules.pop(sink_name, None)
        if mid is not None:
            pactl("unload-module", mid)
        changed = True
        stale_sinks_removed = True

    synced_loopbacks = {
        sink_name: mid for sink_name, mid in live_loopbacks.items()
        if sink_name in sink_names
    }
    if synced_loopbacks != loopback_modules:
        state["loopback_modules"] = synced_loopbacks
        loopback_modules = synced_loopbacks
        changed = True

    active_sinks = [
        name for name in (state.get("active_sinks") or []) if name in sink_names
    ]
    if not active_sinks:
        fallback_sink = _preferred_name(PRIMARY_SINK, sink_names, default_sink_name)
        active_sinks = [fallback_sink] if fallback_sink else []
    if active_sinks != (state.get("active_sinks") or []):
        state["active_sinks"] = active_sinks
        changed = True

    active_sources = [
        name for name in (state.get("active_sources") or []) if name in source_names
    ]
    if not active_sources:
        fallback_source = _preferred_name(
            PRIMARY_SOURCE, source_names, default_source_name)
        active_sources = [fallback_source] if fallback_source else []
    if active_sources != (state.get("active_sources") or []):
        state["active_sources"] = active_sources
        changed = True

    # If the system default moved away from AudioMux, collapse the saved group
    # to the real default and tear down stale loopbacks so UI and graph match.
    if default_sink_name != VIRTUAL_SINK_NAME:
        desired_sinks = [default_sink_name] if default_sink_name in sink_names else []
        if desired_sinks != (state.get("active_sinks") or []):
            state["active_sinks"] = desired_sinks
            changed = True

        for sink_name in list(loopback_modules):
            mid = loopback_modules.pop(sink_name, None)
            if mid is not None:
                pactl("unload-module", mid)
                stale_sinks_removed = True
                changed = True

        if state.get("virtual_sink_module_id") is not None and _has_virtual_sink():
            pactl("unload-module", state["virtual_sink_module_id"])
            state["virtual_sink_module_id"] = None
            changed = True

    if changed:
        save_state(state)

    return stale_sinks_removed


def get_state():
    saved   = load_state()
    offsets = load_offsets()

    with pulsectl.Pulse("audiomux-source") as pulse:
        server = pulse.server_info()
        sink_list = pulse.sink_list()
        source_list = pulse.source_list()
        sink_names = {
            sink.name for sink in sink_list
            if sink.name != VIRTUAL_SINK_NAME and "monitor" not in sink.name
        }
        source_names = {
            source.name for source in source_list
            if source.name != COMBINE_SOURCE_NAME and "monitor" not in source.name
        }

        stale_sinks_removed = _reconcile_state(
            saved, sink_names, source_names, server.default_sink_name,
            server.default_source_name)
        if stale_sinks_removed and server.default_sink_name == VIRTUAL_SINK_NAME:
            set_active_sinks(saved.get("active_sinks") or [], force_rebuild=True)
            saved = load_state()

        has_virtual = any(s.name == VIRTUAL_SINK_NAME for s in sink_list)

        if has_virtual and server.default_sink_name == VIRTUAL_SINK_NAME:
            # Virtual sink is active — ground truth is the live loopbacks
            active_sink_names = _live_loopback_sinks() & sink_names
            if not active_sink_names:
                for sink_name in saved.get("active_sinks") or []:
                    _add_loopback(saved, sink_name)
                if saved.get("active_sinks"):
                    save_state(saved)
                active_sink_names = set(saved.get("active_sinks") or [])
        else:
            # Virtual sink is stale or bypassed — trust the current default sink
            active_sink_names = {server.default_sink_name}

        has_combine_source = any(
            s.name == COMBINE_SOURCE_NAME for s in source_list)
        if has_combine_source and saved.get("active_sources"):
            active_source_names = set(saved["active_sources"]) & source_names
        else:
            active_source_names = {server.default_source_name}

        sinks = []
        for sink in sink_list:
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
        for source in source_list:
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

    wireplumber_restarts = _service_property("wireplumber.service", "NRestarts")
    wireplumber_active_at = _service_property(
        "wireplumber.service", "ActiveEnterTimestamp")

    diagnostics = {
        "wireplumber_restarts": int(wireplumber_restarts or "0"),
        "wireplumber_active_at": wireplumber_active_at,
    }

    return {"sinks": sinks, "sources": sources, "diagnostics": diagnostics}


# ── sink routing (virtual + loopbacks) — legacy mode ─────────────────────────

def _ensure_virtual_sink(state):
    live_mid = _live_virtual_sink_module_id()
    if live_mid is None or not _has_virtual_sink():
        state["virtual_sink_module_id"] = None
    else:
        state["virtual_sink_module_id"] = live_mid

    if state.get("virtual_sink_module_id") is None:
        mid = pactl("load-module", "module-null-sink",
                    f"sink_name={VIRTUAL_SINK_NAME}",
                    "sink_properties=device.description=AudioMux")
        if mid.isdigit():
            state["virtual_sink_module_id"] = int(mid)
    if _has_virtual_sink():
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


def _set_mute(name, muted):
    pactl("set-sink-mute", name, 1 if muted else 0)


def set_active_sinks(names, force_rebuild=False):
    with pulsectl.Pulse("audiomux-set-sinks") as pulse:
        sink_names = {
            sink.name for sink in pulse.sink_list()
            if sink.name != VIRTUAL_SINK_NAME and "monitor" not in sink.name
        }
        names = [name for name in names if name in sink_names]
        if not names:
            fallback = _preferred_name(
                PRIMARY_SINK, sink_names, pulse.server_info().default_sink_name)
            names = [fallback] if fallback else []

    state = load_state()
    current_loopbacks = set(state.get("loopback_modules", {}).keys())
    names_set = set(names)

    _ensure_virtual_sink(state)

    if force_rebuild and current_loopbacks:
        for sink_name in list(current_loopbacks):
            _remove_loopback(state, sink_name)
        current_loopbacks = set()

    for sink_name in current_loopbacks - names_set:
        _remove_loopback(state, sink_name)

    for sink_name in names_set - current_loopbacks:
        _add_loopback(state, sink_name)

    if force_rebuild:
        _set_mute(VIRTUAL_SINK_NAME, False)
        for sink_name in names_set:
            _set_mute(sink_name, False)

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


# ── source routing (combine) — legacy mode ───────────────────────────────────

def set_active_sources(names):
    with pulsectl.Pulse("audiomux-set-sources") as pulse:
        source_names = {
            source.name for source in pulse.source_list()
            if source.name != COMBINE_SOURCE_NAME and "monitor" not in source.name
        }
        names = [name for name in names if name in source_names]
        if not names:
            fallback = _preferred_name(
                PRIMARY_SOURCE, source_names, pulse.server_info().default_source_name)
            names = [fallback] if fallback else []

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


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    # Try daemon path first
    use_daemon = os.path.exists(SOCKET_PATH)

    if not args:
        if use_daemon:
            try:
                resp = _daemon_request({"cmd": "get-state"})
                if resp.get("ok"):
                    print(json.dumps(resp["state"]))
                    return
            except (ConnectionRefusedError, FileNotFoundError, OSError):
                pass
        # Fallback to legacy
        print(json.dumps(get_state()))
        return

    action = args[0]

    if use_daemon:
        try:
            if action == "set-active-sinks":
                names = args[1].split(",") if len(args) > 1 and args[1] else []
                resp = _daemon_request(
                    {"cmd": "set-active-sinks", "names": names})
            elif action == "set-active-sources":
                names = args[1].split(",") if len(args) > 1 and args[1] else []
                resp = _daemon_request(
                    {"cmd": "set-active-sources", "names": names})
            elif action == "unbond-all":
                resp = _daemon_request({"cmd": "unbond-all"})
            elif action == "set-sink-vol" and len(args) >= 3:
                resp = _daemon_request(
                    {"cmd": "set-sink-vol", "name": args[1], "pct": args[2]})
            elif action == "set-source-vol" and len(args) >= 3:
                resp = _daemon_request(
                    {"cmd": "set-source-vol", "name": args[1], "pct": args[2]})
            elif action == "set-sink-offset" and len(args) >= 3:
                resp = _daemon_request(
                    {"cmd": "set-sink-offset",
                     "name": args[1], "offset_ms": int(args[2])})
            elif action == "set-source-offset" and len(args) >= 3:
                resp = _daemon_request(
                    {"cmd": "set-source-offset",
                     "name": args[1], "offset_ms": int(args[2])})
            elif action == "set-master-sink" and len(args) >= 2:
                resp = _daemon_request(
                    {"cmd": "set-master-sink", "name": args[1]})
            elif action == "check-sync":
                resp = _daemon_request({"cmd": "check-sync"})
                print(json.dumps(resp))
                return
            else:
                resp = None

            if resp is not None:
                if not resp.get("ok"):
                    print(json.dumps(resp), file=sys.stderr)
                return
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            pass  # fall through to legacy

    # Legacy mode (daemon not running)
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
