#!/usr/bin/env python3
"""
audiomux-syncd — AudioMux sync daemon

Long-running service that owns the PipeWire routing graph and exposes
state to the plasmoid via a runtime JSON file and a Unix domain socket.

Graph shape (post-Phase-1-revert, see REDESIGN.md §9 addendum):

  * One `module-null-sink` named `audiomux_virtual` — the virtual sink
    the rest of the system treats as default. This is the 0.2.0 shape,
    chosen after the first attempt at using `module-combine-sink` +
    `Props.latencyOffsetNsec` turned out to be broken: `latencyOffsetNsec`
    is a reporting-only hint in PipeWire (confirmed by bench mic test on
    2026-04-14 — setting 500 ms shifted mic arrival by 45 ms, pure
    jitter). Only `pw-loopback --delay` actually buffers samples.
  * One `pw-loopback` child process per active hardware sink, each with
    its own `--delay` computed from calibration. The slowest sink is the
    baseline (0 ms added); faster sinks get positive delay to align.

Sink-set changes → add/remove loopback processes (no audio disruption to
existing sinks). Offset changes → respawn the affected loopbacks (brief
gap on that one sink during respawn; the others keep playing). The
Phase-2 GCC-PHAT calibrator and the Phase-3 DriftMonitor (passive
Params.Latency-based telemetry) are unchanged — only the delay-apply
mechanism changed.
"""

import asyncio
import collections
import json
import logging
import math
import os
import signal
import subprocess
import sys
import threading
import time

import pulsectl

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audiomux_sync_lib import (
    record_monitor, downsample, is_silence, xcorr_offset, lag_to_ms,
    CALIBRATE_FREQS,
)

# ── constants ────────────────────────────────────────────────────────────────

PRIMARY_SINK        = "alsa_output.pci-0000_03_00.6.analog-stereo"
PRIMARY_SOURCE      = "alsa_input.pci-0000_03_00.6.analog-stereo"
VIRTUAL_SINK_NAME   = "audiomux_virtual"
COMBINE_SOURCE_NAME = "audiomux_combined_source"
LOOPBACK_NAME_PREFIX = "audiomux_lb_"

_UID               = os.getuid()
RUN_DIR            = f"/run/user/{_UID}"
STATE_FILE         = f"{RUN_DIR}/audiomux.json"         # plasmoid reads this
INTERNAL_STATE     = f"{RUN_DIR}/audiomux-syncd.json"   # daemon internal state
SOCKET_PATH        = f"{RUN_DIR}/audiomux-syncd.sock"
OFFSETS_FILE       = os.path.expanduser("~/.config/audiomux-offsets.json")

BASE_LATENCY_MS    = 50    # pw-loopback buffer — same as 0.2.0
RECONCILE_INTERVAL = 30    # seconds between full reconciliation sweeps

# Drift measurement (passive, from pw-dump Params.Latency)
MEASURE_INTERVAL   = 3
MEASURE_DURATION   = 0.5
MEASURE_WINDOW     = 20
DRIFT_RECOVER_MS   = 30
DRIFT_RECOVER_COUNT = 3
JUMP_THRESHOLD_MS  = 50

log = logging.getLogger("audiomux-syncd")


# ── helpers ──────────────────────────────────────────────────────────────────

def pactl(*args):
    result = subprocess.run(
        ["pactl"] + [str(a) for a in args],
        capture_output=True, text=True,
    )
    return result.stdout.strip()


def _service_property(service_name, property_name):
    result = subprocess.run(
        ["systemctl", "--user", "show", service_name, f"-p{property_name}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return ""
    prefix = f"{property_name}="
    for line in result.stdout.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    return ""


def load_offsets():
    try:
        with open(OFFSETS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_offsets(offsets):
    with open(OFFSETS_FILE, "w") as f:
        json.dump(offsets, f, indent=2)


def _preferred_name(primary, available, fallback=None):
    if primary in available:
        return primary
    if fallback in available:
        return fallback
    return next(iter(available), None)


def _safe_name(sink_name):
    return sink_name.replace(".", "_").replace(":", "_")


# ── graph queries ────────────────────────────────────────────────────────────

def _has_sink(name):
    result = subprocess.run(
        ["pactl", "list", "sinks", "short"],
        capture_output=True, text=True,
    )
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) > 1 and parts[1] == name:
            return True
    return False


def _live_null_sink_module_ids():
    """Return all module-null-sink modules whose sink_name == VIRTUAL_SINK_NAME."""
    result = subprocess.run(
        ["pactl", "list", "modules", "short"],
        capture_output=True, text=True,
    )
    ids = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        if parts[1] != "module-null-sink":
            continue
        args = parts[2] if len(parts) >= 3 else ""
        if f"sink_name={VIRTUAL_SINK_NAME}" in args:
            ids.append(int(parts[0]))
    return ids


def _live_combine_sink_module_ids():
    """Phase-1 leftovers: module-combine-sink with our VIRTUAL_SINK_NAME."""
    result = subprocess.run(
        ["pactl", "list", "modules", "short"],
        capture_output=True, text=True,
    )
    ids = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        if parts[1] != "module-combine-sink":
            continue
        args = parts[2] if len(parts) >= 3 else ""
        if f"sink_name={VIRTUAL_SINK_NAME}" in args:
            ids.append(int(parts[0]))
    return ids


def _pw_dump():
    result = subprocess.run(["pw-dump"], capture_output=True, text=True)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return []


def _find_sink_node_id(sink_name, dump=None):
    if dump is None:
        dump = _pw_dump()
    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("node.name") == sink_name:
            return obj.get("id")
    return None


def _pw_clear_latency_offset(node_id):
    """Zero out any stale Props.latencyOffsetNsec on a hardware sink node.

    The short-lived Phase-1 code wrote non-zero values here; the
    compensation is now done via pw-loopback --delay, so we need the
    reporting hint to be zero again or latency-aware apps will back-time
    media by the wrong amount.
    """
    if node_id is None:
        return
    subprocess.run(
        ["pw-cli", "set-param", str(node_id), "Props",
         "{ latencyOffsetNsec = 0 }"],
        capture_output=True, text=True,
    )


def _kill_orphan_loopbacks(owned_pids=()):
    """Terminate stray `pw-loopback` processes we don't own — e.g. leftovers
    from a crashed previous daemon."""
    try:
        result = subprocess.run(
            ["pgrep", "-af", f"pw-loopback.*{LOOPBACK_NAME_PREFIX}"],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return
    owned = set(owned_pids)
    for line in result.stdout.splitlines():
        pid_s = line.split(None, 1)[0]
        if not pid_s.isdigit():
            continue
        pid = int(pid_s)
        if pid in owned:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            log.info("migration: killed orphan pw-loopback pid=%d", pid)
        except ProcessLookupError:
            pass


# ── GraphManager ─────────────────────────────────────────────────────────────

class GraphManager:
    """Owns the PipeWire routing graph: null-sink + per-sink pw-loopback.

    Sink-set changes → spawn/kill individual loopbacks (non-disruptive to
    sinks that are keeping their state). Offset changes → respawn just the
    affected loopback(s). Calibration → kill all loopbacks, run subprocess
    against the bare hardware sinks, rebuild loopbacks with new delays.
    """

    def __init__(self):
        self._saved = self._load_saved()
        self._null_sink_module_id = None
        self._loopback_procs = {}   # {sink_name: subprocess.Popen}
        self._loopback_delays = {}  # {sink_name: delay_ms the loopback was spawned with}
        self._master_sink = None
        self._state_dirty = True
        self._sync_measurer = None
        self._reconciling = False
        self._calibrating = False
        self._state_cache = None
        self._state_cache_time = 0
        self._mutation_lock = threading.Lock()
        self._known_sinks = set()    # for hotplug logging
        self._known_sources = set()  # for hotplug logging

    # ── persistent state ─────────────────────────────────────────────────

    @staticmethod
    def _load_saved():
        for path in (INTERNAL_STATE, STATE_FILE):
            try:
                with open(path) as f:
                    data = json.load(f)
                if "active_sinks" in data:
                    for legacy in ("virtual_sink_module_id",
                                    "loopback_modules",
                                    "combine_module_id"):
                        data.pop(legacy, None)
                    return data
            except Exception:
                pass
        return {
            "null_sink_module_id": None,
            "active_sinks": None,
            "source_module_id": None,
            "active_sources": None,
        }

    def _mark_dirty(self):
        self._state_dirty = True

    def flush_state(self):
        if not self._state_dirty:
            return
        with open(INTERNAL_STATE, "w") as f:
            json.dump(self._saved, f)
        self._state_dirty = False

    # ── null-sink lifecycle ──────────────────────────────────────────────

    def _ensure_null_sink(self):
        ids = _live_null_sink_module_ids()
        if len(ids) > 1:
            # Collapse duplicates — keep first, unload rest.
            for mid in ids[1:]:
                pactl("unload-module", str(mid))
                log.info("migration: unloaded duplicate null-sink id=%d", mid)
            ids = ids[:1]

        # Phase-1 leftover: drop any module-combine-sink named audiomux_virtual.
        for mid in _live_combine_sink_module_ids():
            pactl("unload-module", str(mid))
            log.info("migration: unloaded phase-1 combine-sink id=%d", mid)

        if ids and _has_sink(VIRTUAL_SINK_NAME):
            self._null_sink_module_id = ids[0]
            self._saved["null_sink_module_id"] = ids[0]
            pactl("set-default-sink", VIRTUAL_SINK_NAME)
            return

        mid = pactl("load-module", "module-null-sink",
                    f"sink_name={VIRTUAL_SINK_NAME}",
                    "sink_properties=device.description=AudioMux")
        if not mid.isdigit():
            log.error("failed to load null-sink: %r", mid)
            return
        self._null_sink_module_id = int(mid)
        self._saved["null_sink_module_id"] = int(mid)
        self._mark_dirty()
        for _ in range(20):
            if _has_sink(VIRTUAL_SINK_NAME):
                break
            time.sleep(0.05)
        pactl("set-default-sink", VIRTUAL_SINK_NAME)
        log.info("loaded null-sink id=%d", self._null_sink_module_id)

    # ── loopback lifecycle ───────────────────────────────────────────────

    def _compute_delays(self):
        """For each active sink, compute the pw-loopback --delay (ms).

        Offset semantics (unchanged since 0.2.0):
          offsets[sink] = this sink's measured hardware delay (ms, >= 0).
          Slowest sink (max offset) is the baseline → 0 added delay.
          Faster sinks get `max_offset - offset[sink]` to slow them down.
        """
        offsets = load_offsets()
        active = self._saved.get("active_sinks") or []
        raw = {n: int(offsets.get(n, 0) or 0) for n in active}
        mx = max(raw.values()) if raw else 0
        return {n: max(0, mx - v) for n, v in raw.items()}

    def _spawn_loopback(self, sink_name, delay_ms):
        safe = _safe_name(sink_name)
        cmd = [
            "pw-loopback",
            "--name", f"{LOOPBACK_NAME_PREFIX}{safe}",
            "--group", f"{LOOPBACK_NAME_PREFIX}{safe}",
            "--channels", "2",
            "--latency", str(BASE_LATENCY_MS),
            "--capture", VIRTUAL_SINK_NAME,
            "--capture-props", "stream.capture.sink=true",
            "--playback", sink_name,
        ]
        if delay_ms > 0:
            cmd.extend(["--delay", f"{delay_ms / 1000.0:.3f}"])

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._loopback_procs[sink_name] = proc
        self._loopback_delays[sink_name] = delay_ms
        log.info("spawned pw-loopback for %s pid=%d delay=%dms",
                 sink_name, proc.pid, delay_ms)

    def _loopback_alive(self, sink_name):
        proc = self._loopback_procs.get(sink_name)
        return proc is not None and proc.poll() is None

    def _kill_loopback(self, sink_name):
        proc = self._loopback_procs.pop(sink_name, None)
        self._loopback_delays.pop(sink_name, None)
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        log.info("killed pw-loopback for %s pid=%d", sink_name, proc.pid)

    def _kill_all_loopbacks(self):
        for sink in list(self._loopback_procs):
            self._kill_loopback(sink)

    def _ensure_loopbacks(self):
        """Reconcile the set of live loopbacks against active_sinks and the
        currently-stored delays. Spawn missing, respawn delay-changed, kill
        sinks that left the active set."""
        active = set(self._saved.get("active_sinks") or [])
        delays = self._compute_delays()

        # Kill loopbacks for sinks that dropped out of the active set.
        for sink in list(self._loopback_procs):
            if sink not in active:
                self._kill_loopback(sink)

        # Reap dead procs so _loopback_alive is accurate.
        for sink in list(self._loopback_procs):
            if not self._loopback_alive(sink):
                log.warning("pw-loopback for %s died; will respawn", sink)
                self._loopback_procs.pop(sink, None)
                self._loopback_delays.pop(sink, None)

        # Spawn / respawn.
        for sink in active:
            desired = delays.get(sink, 0)
            if not self._loopback_alive(sink):
                self._spawn_loopback(sink, desired)
            elif self._loopback_delays.get(sink) != desired:
                self._kill_loopback(sink)
                self._spawn_loopback(sink, desired)

    # ── master sink selection ────────────────────────────────────────────

    def _resolve_master(self, active_sinks):
        if self._master_sink and self._master_sink in active_sinks:
            return self._master_sink
        if PRIMARY_SINK in active_sinks:
            return PRIMARY_SINK
        return next(iter(active_sinks)) if active_sinks else None

    # ── reconciliation ───────────────────────────────────────────────────

    def reconcile(self):
        if self._reconciling or self._calibrating:
            return
        self._reconciling = True
        try:
            self._reconcile_inner()
        finally:
            self._reconciling = False

    def _reconcile_inner(self):
        with pulsectl.Pulse("audiomux-syncd-reconcile") as pulse:
            server = pulse.server_info()
            sink_list = pulse.sink_list()
            source_list = pulse.source_list()

        sink_names = {
            s.name for s in sink_list
            if s.name != VIRTUAL_SINK_NAME and "monitor" not in s.name
        }
        source_names = {
            s.name for s in source_list
            if s.name != COMBINE_SOURCE_NAME and "monitor" not in s.name
        }

        # Hotplug logging — surface new/removed devices. Useful when
        # watching BT speakers connect / disconnect during beta testing.
        if self._known_sinks:
            for sn in sorted(sink_names - self._known_sinks):
                log.info("hotplug: new sink appeared: %s", sn)
            for sn in sorted(self._known_sinks - sink_names):
                log.info("hotplug: sink disappeared: %s", sn)
        if self._known_sources:
            for sn in sorted(source_names - self._known_sources):
                log.info("hotplug: new source appeared: %s", sn)
            for sn in sorted(self._known_sources - source_names):
                log.info("hotplug: source disappeared: %s", sn)
        self._known_sinks = set(sink_names)
        self._known_sources = set(source_names)

        with self._mutation_lock:
            self._ensure_null_sink()

        # Defend audiomux_virtual as the default sink. WirePlumber's
        # rescue-on-connect policy auto-switches the default to newly-
        # appeared BT sinks, and that shouldn't disarm audiomux — we own
        # the default sink while we're running. If the user genuinely
        # wants to step aside, the applet has an explicit Unbond action.
        if (server.default_sink_name != VIRTUAL_SINK_NAME
                and _has_sink(VIRTUAL_SINK_NAME)):
            log.info("reasserting default sink to %s (was %s)",
                     VIRTUAL_SINK_NAME, server.default_sink_name)
            pactl("set-default-sink", VIRTUAL_SINK_NAME)

        active = [
            n for n in (self._saved.get("active_sinks") or [])
            if n in sink_names
        ]
        if not active:
            fb = _preferred_name(
                PRIMARY_SINK, sink_names, server.default_sink_name)
            active = [fb] if fb else []

        active_sources = [
            n for n in (self._saved.get("active_sources") or [])
            if n in source_names
        ]
        if not active_sources:
            fb = _preferred_name(
                PRIMARY_SOURCE, source_names, server.default_source_name)
            active_sources = [fb] if fb else []

        self._saved["active_sinks"] = active
        self._saved["active_sources"] = active_sources
        self._mark_dirty()
        self.flush_state()

        with self._mutation_lock:
            self._ensure_loopbacks()

        self._master_sink = self._resolve_master(set(active))

    # ── state JSON for plasmoid ──────────────────────────────────────────

    def get_state_json(self):
        now = time.time()
        if self._state_cache and now - self._state_cache_time < 2.0:
            return self._state_cache
        self._state_cache = self._build_state_json()
        self._state_cache_time = now
        return self._state_cache

    def invalidate_state_cache(self):
        self._state_cache = None

    def _build_state_json(self):
        offsets = load_offsets()
        active_sink_set = set(self._saved.get("active_sinks") or [])
        active_source_set = set(self._saved.get("active_sources") or [])

        with pulsectl.Pulse("audiomux-syncd-state") as pulse:
            server = pulse.server_info()
            sink_list = pulse.sink_list()
            source_list = pulse.source_list()

            source_names = {
                s.name for s in source_list
                if s.name != COMBINE_SOURCE_NAME and "monitor" not in s.name
            }

            has_virtual = any(s.name == VIRTUAL_SINK_NAME for s in sink_list)
            if not has_virtual or server.default_sink_name != VIRTUAL_SINK_NAME:
                active_sink_set = {server.default_sink_name}

            has_combine = any(
                s.name == COMBINE_SOURCE_NAME for s in source_list)
            if not (has_combine and active_source_set):
                active_source_set = {server.default_source_name}
            else:
                active_source_set = active_source_set & source_names

            master = self._resolve_master(active_sink_set)

            sinks = []
            for s in sink_list:
                if s.name == VIRTUAL_SINK_NAME or "monitor" in s.name:
                    continue
                entry = {
                    "name":        s.name,
                    "description": s.description,
                    "volume":      round(pulse.volume_get_all_chans(s) * 100),
                    "is_primary":  s.name == PRIMARY_SINK,
                    "active":      s.name in active_sink_set,
                    "offset":      offsets.get(s.name, 0),
                    "role":        "master" if s.name == master else "follower",
                }
                if self._sync_measurer and s.name in active_sink_set:
                    entry["sync"] = self._sync_measurer.get_sync_info(s.name)
                sinks.append(entry)

            sources = []
            for s in source_list:
                if s.name == COMBINE_SOURCE_NAME or "monitor" in s.name:
                    continue
                sources.append({
                    "name":        s.name,
                    "description": s.description,
                    "volume":      round(pulse.volume_get_all_chans(s) * 100),
                    "is_primary":  s.name == PRIMARY_SOURCE,
                    "active":      s.name in active_source_set,
                    "offset":      offsets.get(s.name, 0),
                })

        wp_restarts = _service_property("wireplumber.service", "NRestarts")
        wp_active   = _service_property(
            "wireplumber.service", "ActiveEnterTimestamp")

        return {
            "sinks": sinks,
            "sources": sources,
            "master_sink": master,
            "diagnostics": {
                "wireplumber_restarts": int(wp_restarts or "0"),
                "wireplumber_active_at": wp_active,
            },
        }

    # ── command methods ──────────────────────────────────────────────────

    def set_active_sinks(self, names):
        with pulsectl.Pulse("audiomux-syncd-set-sinks") as pulse:
            sink_names = {
                s.name for s in pulse.sink_list()
                if s.name != VIRTUAL_SINK_NAME and "monitor" not in s.name
            }
            names = [n for n in names if n in sink_names]
            if not names:
                fb = _preferred_name(
                    PRIMARY_SINK, sink_names,
                    pulse.server_info().default_sink_name)
                names = [fb] if fb else []

        names = sorted(set(names))
        self._saved["active_sinks"] = names
        self._mark_dirty()
        self.flush_state()
        with self._mutation_lock:
            self._ensure_null_sink()
            self._ensure_loopbacks()
        self._master_sink = self._resolve_master(set(names))

    def set_active_sources(self, names):
        with pulsectl.Pulse("audiomux-syncd-set-sources") as pulse:
            source_names = {
                s.name for s in pulse.source_list()
                if s.name != COMBINE_SOURCE_NAME and "monitor" not in s.name
            }
            names = [n for n in names if n in source_names]
            if not names:
                fb = _preferred_name(
                    PRIMARY_SOURCE, source_names,
                    pulse.server_info().default_source_name)
                names = [fb] if fb else []

        if self._saved.get("source_module_id") is not None:
            pactl("unload-module", self._saved["source_module_id"])
            self._saved["source_module_id"] = None

        if len(names) == 1:
            pactl("set-default-source", names[0])
        else:
            mid = pactl("load-module", "module-combine-source",
                        f"source_name={COMBINE_SOURCE_NAME}",
                        f"master={names[0]}",
                        f"slaves={','.join(names)}")
            if mid.isdigit():
                self._saved["source_module_id"] = int(mid)
                pactl("set-default-source", COMBINE_SOURCE_NAME)

        self._saved["active_sources"] = list(names)
        self._mark_dirty()
        self.flush_state()

    def set_sink_offset(self, name, offset_ms):
        offsets = load_offsets()
        old = int(offsets.get(name, 0) or 0)
        new = max(0, int(offset_ms))
        offsets[name] = new
        if old != new:
            log.info("manual: offset %s  %d ms → %d ms", name, old, new)
        save_offsets(offsets)
        # Delay changes are relative — any one offset change can shift
        # every sink's --delay, so let _ensure_loopbacks sort out which
        # procs actually need respawning (it compares against the last
        # known delay per sink).
        with self._mutation_lock:
            self._ensure_loopbacks()

    def set_source_offset(self, name, offset_ms):
        offsets = load_offsets()
        offsets[name] = int(offset_ms)
        save_offsets(offsets)

    def set_master_sink(self, name):
        active = set(self._saved.get("active_sinks") or [])
        if name not in active:
            return False
        self._master_sink = name
        log.info("master sink set to %s", name)
        return True

    def unbond_all(self):
        self.set_active_sinks([PRIMARY_SINK])
        self.set_active_sources([PRIMARY_SOURCE])

    def active_sinks(self):
        return list(self._saved.get("active_sinks") or [])

    def owned_loopback_pids(self):
        return [p.pid for p in self._loopback_procs.values() if p.poll() is None]

    # ── calibration helpers ──────────────────────────────────────────────

    def prepare_for_calibration(self, sinks):
        """Tear down all loopbacks so the calibrator can play directly to
        hardware sinks with no interference. Also zero any stale
        latencyOffsetNsec (leftover from the short-lived Phase-1 attempt)
        on each hardware sink."""
        with self._mutation_lock:
            self._kill_all_loopbacks()
        dump = _pw_dump()
        for sn in sinks:
            node_id = _find_sink_node_id(sn, dump)
            _pw_clear_latency_offset(node_id)

    def restore_after_calibration(self, sinks):
        """Re-spawn loopbacks with whatever offsets are now in the file."""
        self._saved["active_sinks"] = list(sinks)
        self._mark_dirty()
        self.flush_state()
        with self._mutation_lock:
            self._ensure_null_sink()
            self._ensure_loopbacks()

    # ── shutdown ─────────────────────────────────────────────────────────

    def shutdown(self):
        log.info("shutting down — killing %d loopbacks",
                 len(self._loopback_procs))
        self._kill_all_loopbacks()


# ── DriftMonitor ─────────────────────────────────────────────────────────────

class DriftMonitor:
    """Passive drift telemetry from `pw-dump` snapshots (unchanged from
    Phase 3). Samples each active follower sink's `Params.Latency.Input.minNs`
    at `MEASURE_INTERVAL` cadence and surfaces offset/drift/jitter in the
    state JSON."""

    RESYNC_COOLDOWN_S = 2.0

    # Every N samples, dump a summary of each follower's current state at
    # INFO so beta testers can see drift without toggling debug logs.
    SUMMARY_EVERY_N_SAMPLES = 10

    def __init__(self, graph):
        self._graph = graph
        self._history = collections.defaultdict(
            lambda: collections.deque(maxlen=MEASURE_WINDOW))
        self._exceed_count = collections.defaultdict(int)
        self._last_resync_ts = 0.0
        self._lock = threading.Lock()
        self._sample_count = 0
        self._last_reported_latency = {}  # {sink: ns} — for per-sample change logs
        self.enabled = True

    async def run(self):
        while True:
            await asyncio.sleep(MEASURE_INTERVAL)
            if not self.enabled or self._graph._calibrating:
                continue
            try:
                self._sample()
                self._maybe_resync()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("drift sampling failed")

    def _sample(self):
        dump = _pw_dump()
        active = set(self._graph.active_sinks())
        now = time.time()
        sampled = {}
        with self._lock:
            for obj in dump:
                if obj.get("type") != "PipeWire:Interface:Node":
                    continue
                info = obj.get("info") or {}
                props = info.get("props") or {}
                name = props.get("node.name", "")
                if name not in active:
                    continue
                params = info.get("params") or {}
                latency = params.get("Latency", []) or []
                input_ns = None
                for lat in latency:
                    if (isinstance(lat, dict)
                            and lat.get("direction") == "Input"):
                        input_ns = lat.get("minNs")
                        break
                if input_ns is None:
                    continue
                self._history[name].append((now, int(input_ns)))
                sampled[name] = int(input_ns)
            for n in list(self._history):
                if n not in active:
                    self._history.pop(n, None)
                    self._exceed_count.pop(n, None)

            # Surface per-sink Params.Latency whenever it changes materially.
            # BT codec buffers are the most interesting case — they jump
            # around when the remote device wakes / reconnects.
            for name, ns in sampled.items():
                prev = self._last_reported_latency.get(name)
                if prev is None or abs(prev - ns) >= 1_000_000:  # >= 1 ms shift
                    log.info("drift: %s Params.Latency.Input = %.1f ms "
                             "(prev %s)", name, ns / 1_000_000.0,
                             f"{prev/1e6:.1f}ms" if prev is not None else "—")
                    self._last_reported_latency[name] = ns

            self._sample_count += 1
            if (self.SUMMARY_EVERY_N_SAMPLES > 0
                    and self._sample_count % self.SUMMARY_EVERY_N_SAMPLES == 0):
                master = self._graph._master_sink
                parts = []
                for s in sorted(sampled):
                    role = "M" if s == master else "F"
                    info = self._compute_locked(s, master)
                    if info is None:
                        parts.append(f"{s}[{role}]=…")
                    else:
                        parts.append(f"{s}[{role}] lat={info['latency_ms']:.0f}ms "
                                     f"off={info['offset_ms']:+.0f} "
                                     f"drift={info['drift_ms_per_min']:+.1f}/min "
                                     f"jit={info['jitter_ms']:.1f}")
                if parts:
                    log.info("drift summary: %s", " | ".join(parts))

    def _maybe_resync(self):
        active = set(self._graph.active_sinks())
        master = self._graph._master_sink
        now = time.time()
        if now - self._last_resync_ts < self.RESYNC_COOLDOWN_S:
            return
        triggered = False
        with self._lock:
            for sink in active:
                if sink == master:
                    continue
                info = self._compute_locked(sink, master)
                if info is None:
                    continue
                if abs(info["offset_ms"]) >= DRIFT_RECOVER_MS:
                    self._exceed_count[sink] += 1
                    if self._exceed_count[sink] >= DRIFT_RECOVER_COUNT:
                        triggered = True
                        self._exceed_count[sink] = 0
                else:
                    self._exceed_count[sink] = 0
        if triggered:
            log.warning("drift monitor: hard resync (sustained offset ≥ "
                        "%dms on a follower)", DRIFT_RECOVER_MS)
            self._last_resync_ts = now
            try:
                with self._graph._mutation_lock:
                    self._graph._ensure_loopbacks()
            except Exception:
                log.exception("resync attempt failed")

    def _compute_locked(self, sink_name, master):
        hist = self._history.get(sink_name)
        if not hist or len(hist) < 2:
            return None
        ts_list = [t for t, _ in hist]
        lat_ms = [ns / 1_000_000.0 for _, ns in hist]
        sorted_ms = sorted(lat_ms)
        median = sorted_ms[len(sorted_ms) // 2]
        current = lat_ms[-1]
        offset_ms = current - median

        jitter_ms = 0.0
        if len(lat_ms) > 1:
            mean = sum(lat_ms) / len(lat_ms)
            jitter_ms = (sum((y - mean) ** 2 for y in lat_ms)
                         / len(lat_ms)) ** 0.5

        drift_ms_per_min = 0.0
        if len(hist) >= 3:
            t0 = ts_list[0]
            xs = [t - t0 for t in ts_list]
            ys = lat_ms
            n = len(xs)
            sx, sy = sum(xs), sum(ys)
            sxy = sum(x * y for x, y in zip(xs, ys))
            sxx = sum(x * x for x in xs)
            denom = n * sxx - sx * sx
            if denom > 0:
                slope = (n * sxy - sx * sy) / denom
                drift_ms_per_min = slope * 60.0

        return {
            "offset_ms": offset_ms,
            "drift_ms_per_min": drift_ms_per_min,
            "jitter_ms": jitter_ms,
            "latency_ms": current,
            "samples": len(hist),
        }

    def get_sync_info(self, sink_name):
        master = self._graph._master_sink
        if sink_name == master:
            return {
                "role": "master",
                "offset_ms": 0,
                "drift_ms_per_min": 0,
                "jitter_ms": 0,
                "confidence": 1.0,
                "status": "reference",
            }
        with self._lock:
            info = self._compute_locked(sink_name, master)
            exceed = self._exceed_count.get(sink_name, 0)
        if info is None:
            return {
                "role": "follower",
                "offset_ms": 0,
                "drift_ms_per_min": 0,
                "jitter_ms": 0,
                "confidence": 0.0,
                "status": "measuring",
            }
        offset = info["offset_ms"]
        if abs(offset) >= DRIFT_RECOVER_MS:
            status = "resyncing" if exceed >= DRIFT_RECOVER_COUNT - 1 else "degraded"
        elif abs(offset) >= 5:
            status = "degraded"
        else:
            status = "synced"
        return {
            "role": "follower",
            "offset_ms": round(offset, 1),
            "drift_ms_per_min": round(info["drift_ms_per_min"], 2),
            "jitter_ms": round(info["jitter_ms"], 2),
            "latency_ms": round(info["latency_ms"], 1),
            "confidence": 1.0,
            "status": status,
            "samples": info["samples"],
        }


SyncMeasurer = DriftMonitor


# ── PipeWireMonitor ──────────────────────────────────────────────────────────

class PipeWireMonitor:
    DEBOUNCE_SEC = 2.0

    def __init__(self, on_change):
        self._on_change = on_change
        self._proc = None
        self._last_trigger = 0.0

    async def run(self):
        while True:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("pw-dump --monitor crashed, restarting in 5s")
                await asyncio.sleep(5)

    async def _run_once(self):
        self._proc = await asyncio.create_subprocess_exec(
            "pw-dump", "--monitor", "--no-colors",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        bracket_depth = 0
        in_array = False
        while True:
            chunk = await self._proc.stdout.read(4096)
            if not chunk:
                break
            for byte in chunk:
                ch = chr(byte)
                if ch == "[":
                    if bracket_depth == 0:
                        in_array = True
                    bracket_depth += 1
                elif ch == "]":
                    bracket_depth -= 1
                    if bracket_depth == 0 and in_array:
                        in_array = False
                        now = time.time()
                        if now - self._last_trigger < self.DEBOUNCE_SEC:
                            continue
                        self._last_trigger = now
                        try:
                            self._on_change()
                        except Exception:
                            log.exception(
                                "reconcile failed after pw-dump event")
        rc = await self._proc.wait()
        if rc != 0:
            log.warning("pw-dump exited with code %d", rc)

    async def stop(self):
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            await self._proc.wait()


# ── StateWriter ──────────────────────────────────────────────────────────────

class StateWriter:
    def __init__(self, graph):
        self._graph = graph
        self._pending = False
        self._min_interval = 0.5

    async def run(self):
        while True:
            await asyncio.sleep(self._min_interval)
            if self._pending:
                self._pending = False
                try:
                    state = self._graph.get_state_json()
                    tmp = STATE_FILE + ".tmp"
                    with open(tmp, "w") as f:
                        json.dump(state, f)
                    os.replace(tmp, STATE_FILE)
                except Exception:
                    log.exception("failed to write state file")

    def notify(self):
        self._pending = True


# ── SocketServer ─────────────────────────────────────────────────────────────

class SocketServer:
    def __init__(self, graph, state_writer, measurer=None):
        self._graph = graph
        self._state_writer = state_writer
        self._measurer = measurer
        self._server = None
        self._calibration_in_progress = False

    async def start(self):
        self._cleanup_stale_socket()
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o600)
        log.info("listening on %s", SOCKET_PATH)

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass

    def _cleanup_stale_socket(self):
        if not os.path.exists(SOCKET_PATH):
            return
        import socket
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(SOCKET_PATH)
            log.error("another audiomux-syncd is already running")
            sys.exit(1)
        except ConnectionRefusedError:
            log.info("removing stale socket %s", SOCKET_PATH)
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
        finally:
            sock.close()

    async def _handle_client(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=30.0)
            if not line:
                return
            request = json.loads(line.decode())
            response = await asyncio.get_event_loop().run_in_executor(
                None, self._dispatch, request)
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        except asyncio.TimeoutError:
            log.warning("client timed out")
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            log.exception("error handling client")
            try:
                err = json.dumps({"ok": False, "error": "internal error"})
                writer.write(err.encode() + b"\n")
                await writer.drain()
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass

    def _dispatch(self, req):
        cmd = req.get("cmd", "")
        try:
            if cmd == "get-state":
                return {"ok": True, "state": self._graph.get_state_json()}

            elif cmd == "set-active-sinks":
                self._graph.set_active_sinks(req.get("names", []))
                self._graph.invalidate_state_cache()
                self._state_writer.notify()
                return {"ok": True}

            elif cmd == "set-active-sources":
                self._graph.set_active_sources(req.get("names", []))
                self._graph.invalidate_state_cache()
                self._state_writer.notify()
                return {"ok": True}

            elif cmd == "set-sink-offset":
                self._graph.set_sink_offset(
                    req["name"], int(req["offset_ms"]))
                self._graph.invalidate_state_cache()
                self._state_writer.notify()
                return {"ok": True}

            elif cmd == "set-source-offset":
                self._graph.set_source_offset(
                    req["name"], int(req["offset_ms"]))
                self._graph.invalidate_state_cache()
                self._state_writer.notify()
                return {"ok": True}

            elif cmd == "set-sink-vol":
                pactl("set-sink-volume", req["name"], f"{req['pct']}%")
                self._graph.invalidate_state_cache()
                self._state_writer.notify()
                return {"ok": True}

            elif cmd == "set-source-vol":
                pactl("set-source-volume", req["name"], f"{req['pct']}%")
                self._graph.invalidate_state_cache()
                self._state_writer.notify()
                return {"ok": True}

            elif cmd == "set-master-sink":
                ok = self._graph.set_master_sink(req["name"])
                if ok:
                    self._graph.invalidate_state_cache()
                    self._state_writer.notify()
                    return {"ok": True}
                return {"ok": False, "error": "sink not active"}

            elif cmd == "unbond-all":
                self._graph.unbond_all()
                self._graph.invalidate_state_cache()
                self._state_writer.notify()
                return {"ok": True}

            elif cmd == "check-sync":
                return self._run_calibration()

            elif cmd == "ping":
                return {
                    "ok": True,
                    "pid": os.getpid(),
                    "calibration_in_progress": self._calibration_in_progress,
                }

            else:
                return {"ok": False, "error": f"unknown command: {cmd}"}

        except Exception as e:
            log.exception("command %s failed", cmd)
            return {"ok": False, "error": str(e)}

    def _run_calibration(self):
        """Run the Phase-2 GCC-PHAT calibrator as an isolated subprocess."""
        self._calibration_in_progress = True
        self._graph._calibrating = True
        try:
            active = list(self._graph.active_sinks())
            if len(active) < 2:
                return {"ok": False,
                        "error": "need 2+ active outputs to calibrate"}

            with pulsectl.Pulse("audiomux-calibrate") as pulse:
                mic = pulse.server_info().default_source_name
            if not mic or "monitor" in mic:
                return {"ok": False, "error": "no microphone found"}

            sinks = sorted(active)
            log.info("calibration: starting subprocess (mic=%s, sinks=%s)",
                     mic, sinks)

            # Tear down loopbacks so the calibrator plays straight to each
            # hardware sink without our own compensation bias.
            self._graph.prepare_for_calibration(sinks)

            n_tones = min(8, len(CALIBRATE_FREQS))
            round_specs = [
                {"sink": sinks[i % len(sinks)], "freq": CALIBRATE_FREQS[i]}
                for i in range(n_tones)
            ]

            cal_script = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "audiomux_calibrate.py")
            cal_cmd = [sys.executable, cal_script, mic] + sinks
            cal_error = None
            cal_result = None
            try:
                proc = subprocess.Popen(
                    cal_cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                stdout, stderr = proc.communicate(
                    input=json.dumps(round_specs).encode(),
                    timeout=45,
                )
                if proc.returncode != 0:
                    cal_error = f"subprocess exited {proc.returncode}"
                    if stderr:
                        cal_error += f": {stderr.decode()[:200]}"
                else:
                    cal_result = json.loads(stdout.decode())
                    if not cal_result.get("ok"):
                        cal_error = cal_result.get("error", "unknown")
            except subprocess.TimeoutExpired:
                log.warning("calibration subprocess timed out, killing")
                proc.kill()
                proc.wait()
                cal_error = "calibration timed out (mic released)"
            except Exception as e:
                cal_error = str(e)
                log.exception("calibration subprocess failed")

            if cal_error:
                log.info("calibration: restoring graph after error")
                self._graph.restore_after_calibration(sinks)
                self._graph.invalidate_state_cache()
                self._state_writer.notify()
                return {"ok": False, "error": cal_error}

            all_measurements = {sn: [] for sn in sinks}
            for rn, results_round in enumerate(
                    cal_result.get("rounds", [])):
                if "error" in results_round:
                    log.warning("calibration: round %d error: %s",
                                rn + 1, results_round["error"])
                    continue
                for sn, data in results_round.items():
                    delay = data.get("delay_ms", 0)
                    conf = data.get("confidence", 0)
                    all_measurements[sn].append((delay, conf))
                    pr = data.get("peak_ratio")
                    log.info("calibration: round %d %s = %.1fms "
                             "(conf %.2f, peak_ratio=%s)",
                             rn + 1, sn, delay, conf,
                             f"{pr:.1f}" if pr is not None else "—")

            results = {}
            for sn in sinks:
                good = [(d, c) for d, c in all_measurements[sn]
                        if c > 0.1 and d >= 0]
                if good:
                    avg_delay = sum(d for d, _ in good) / len(good)
                    avg_conf = sum(c for _, c in good) / len(good)
                    results[sn] = {
                        "delay_ms": round(avg_delay, 1),
                        "confidence": round(avg_conf, 2),
                        "rounds": len(good),
                    }
                else:
                    results[sn] = {
                        "delay_ms": 0, "confidence": 0,
                        "rounds": 0, "error": "no signal detected",
                    }

            good_results = {k: v for k, v in results.items()
                            if "error" not in v}
            if good_results:
                min_delay = min(v["delay_ms"]
                                for v in good_results.values())
                offsets = load_offsets()
                for sn, data in good_results.items():
                    offsets[sn] = max(0, round(
                        data["delay_ms"] - min_delay))
                save_offsets(offsets)
                log.info("calibration: normalized (min=%.1fms subtracted)",
                         min_delay)
                applied = {k: offsets.get(k, 0) for k in sinks}
                log.info("calibration: offsets applied %s", applied)
            else:
                log.warning("calibration: no usable readings")

            self._graph.restore_after_calibration(sinks)
            self._graph.invalidate_state_cache()
            self._state_writer.notify()
            return {"ok": True, "mic": mic, "results": results}
        finally:
            self._calibration_in_progress = False
            self._graph._calibrating = False


# ── main ─────────────────────────────────────────────────────────────────────

async def _main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log.info("starting audiomux-syncd (pid %d)", os.getpid())

    # Migration: kill stray pw-loopbacks from a crashed previous daemon, and
    # unload any Phase-1 module-combine-sink that's still hanging around.
    _kill_orphan_loopbacks(owned_pids=())
    for mid in _live_combine_sink_module_ids():
        log.info("migration: unloading phase-1 combine-sink id=%d", mid)
        pactl("unload-module", str(mid))

    graph = GraphManager()
    measurer = DriftMonitor(graph)
    graph._sync_measurer = measurer

    try:
        saved_sinks = graph._saved.get("active_sinks")
        if saved_sinks:
            graph.set_active_sinks(saved_sinks)
        else:
            with pulsectl.Pulse("audiomux-syncd-bootstrap") as pulse:
                server = pulse.server_info()
                boot_sink = PRIMARY_SINK if any(
                    s.name == PRIMARY_SINK for s in pulse.sink_list()
                ) else server.default_sink_name
            log.info("no saved state, bootstrapping with %s", boot_sink)
            graph.set_active_sinks([boot_sink])
    except Exception:
        log.exception("initial setup failed")

    state_writer = StateWriter(graph)
    state_writer.notify()

    socket_server = SocketServer(graph, state_writer, measurer)
    await socket_server.start()

    monitor = PipeWireMonitor(on_change=lambda: _reconcile_and_notify(
        graph, state_writer))

    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _handle_signal():
        log.info("received shutdown signal")
        stop_event.set()
        def _force_exit():
            import time as _time
            _time.sleep(5)
            log.warning("graceful shutdown timed out, forcing exit")
            os._exit(1)
        threading.Thread(target=_force_exit, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal)

    monitor_task = asyncio.create_task(monitor.run())
    writer_task  = asyncio.create_task(state_writer.run())
    reconcile_task = asyncio.create_task(
        _periodic_reconcile(graph, state_writer))
    measurer_task = asyncio.create_task(measurer.run())

    await stop_event.wait()

    log.info("shutting down")
    monitor_task.cancel()
    writer_task.cancel()
    reconcile_task.cancel()
    measurer_task.cancel()
    await monitor.stop()
    await socket_server.stop()
    graph.shutdown()


def _reconcile_and_notify(graph, state_writer):
    try:
        graph.reconcile()
        state_writer.notify()
    except Exception:
        log.exception("reconciliation failed")


async def _periodic_reconcile(graph, state_writer):
    while True:
        await asyncio.sleep(RECONCILE_INTERVAL)
        try:
            graph.reconcile()
            state_writer.notify()
        except Exception:
            log.exception("periodic reconciliation failed")


def main():
    asyncio.run(_main())


if __name__ == "__main__":
    main()
