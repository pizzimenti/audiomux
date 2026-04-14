#!/usr/bin/env python3
"""
audiomux-syncd — AudioMux sync daemon

Long-running service that owns the PipeWire routing graph and exposes
state to the plasmoid via a runtime JSON file and a Unix domain socket.

Phase 1 architecture (see REDESIGN.md §3.1):

  * One `module-combine-sink` named `audiomux_virtual` fans out to the
    user-selected hardware sinks. Probe 1 confirmed the combine sink and its
    backend stream nodes share a single `node.driver-id` / `node.link-group`.
  * Per-sink static latency offsets are applied via
    `pw-cli set-param <hw-sink-id> Props '{ latencyOffsetNsec = N }'` on
    the hardware sink nodes. Probe 2 confirmed these writes are live, zero-
    delta, and require no relink — the previous pw-loopback `--delay`
    rebuild-on-change dance is gone.
  * The fastest sink is the baseline (0 added delay); slower sinks are given
    positive `latencyOffsetNsec` to slow the faster sinks down to match.

This supersedes the 0.2.0 architecture of `module-null-sink` + one
`pw-loopback` per output sink.
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

_UID               = os.getuid()
RUN_DIR            = f"/run/user/{_UID}"
STATE_FILE         = f"{RUN_DIR}/audiomux.json"         # plasmoid reads this
INTERNAL_STATE     = f"{RUN_DIR}/audiomux-syncd.json"   # daemon internal state
SOCKET_PATH        = f"{RUN_DIR}/audiomux-syncd.sock"
OFFSETS_FILE       = os.path.expanduser("~/.config/audiomux-offsets.json")

RECONCILE_INTERVAL = 30    # seconds between full reconciliation sweeps

# Sync measurement (Phase 3 will replace this with passive pw-dump telemetry)
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


def _live_combine_module_ids():
    """Return every currently-loaded module-combine-sink for VIRTUAL_SINK_NAME.

    Returns a list — in normal operation it's length 0 or 1, but duplicates
    can accumulate if the daemon crashes mid-reload or if a pactl race
    slips one past the unload. Callers should pick the first for "is one
    live?" checks and unload all on migration/cleanup.
    """
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


def _live_combine_module_id():
    ids = _live_combine_module_ids()
    return ids[0] if ids else None


def _live_null_sink_module_id():
    """Find a legacy module-null-sink for VIRTUAL_SINK_NAME (0.2.0 leftovers)."""
    result = subprocess.run(
        ["pactl", "list", "modules", "short"],
        capture_output=True, text=True,
    )
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        if parts[1] != "module-null-sink":
            continue
        args = parts[2] if len(parts) >= 3 else ""
        if f"sink_name={VIRTUAL_SINK_NAME}" in args:
            return int(parts[0])
    return None


def _combine_member_sinks(module_id):
    """Read the `sinks=a,b,c` argument of a running combine-sink module."""
    if module_id is None:
        return []
    result = subprocess.run(
        ["pactl", "list", "modules", "short"],
        capture_output=True, text=True,
    )
    target = str(module_id)
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or parts[0] != target:
            continue
        for token in parts[2].split():
            if token.startswith("sinks="):
                return [s for s in token[len("sinks="):].split(",") if s]
    return []


def _pw_dump():
    result = subprocess.run(["pw-dump"], capture_output=True, text=True)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return []


def _find_sink_node_id(sink_name, dump=None):
    """Return the PipeWire node id for a hardware sink by its PulseAudio name."""
    if dump is None:
        dump = _pw_dump()
    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("node.name") == sink_name:
            return obj.get("id")
    return None


def _pw_set_latency_offset_ns(node_id, nsec):
    """Set Props.latencyOffsetNsec on a sink node. Idempotent; safe to repeat."""
    if node_id is None:
        return False
    result = subprocess.run(
        ["pw-cli", "set-param", str(node_id), "Props",
         "{ latencyOffsetNsec = %d }" % int(nsec)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.warning("pw-cli set-param failed for node %s: %s",
                    node_id, result.stderr.strip()[:120])
        return False
    return True


def _kill_legacy_pw_loopbacks():
    """Terminate any stray `pw-loopback` processes launched by a prior 0.2.0
    daemon that wasn't shut down cleanly."""
    try:
        result = subprocess.run(
            ["pgrep", "-af", "pw-loopback.*audiomux_lb_"],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return
    for line in result.stdout.splitlines():
        pid_s = line.split(None, 1)[0]
        if not pid_s.isdigit():
            continue
        pid = int(pid_s)
        try:
            os.kill(pid, signal.SIGTERM)
            log.info("migration: killed legacy pw-loopback pid=%d", pid)
        except ProcessLookupError:
            pass


# ── GraphManager ─────────────────────────────────────────────────────────────

class GraphManager:
    """Owns the PipeWire routing graph.

    Graph shape: one `module-combine-sink` named `audiomux_virtual`, whose
    `sinks=` argument enumerates the user-selected hardware sinks. Each
    hardware sink carries a `Props.latencyOffsetNsec` that compensates for
    its static hardware-path delay (set once by calibration).

    Sink-set changes → unload + reload combine-sink (one operation).
    Offset changes   → pw-cli set-param on the hardware sink (instant, no
                       relink, no audio dropout).
    """

    def __init__(self):
        self._saved = self._load_saved()
        self._combine_module_id = None
        self._master_sink = None
        self._state_dirty = True
        self._sync_measurer = None
        self._reconciling = False
        self._calibrating = False  # set by SocketServer around check-sync
        self._state_cache = None
        self._state_cache_time = 0
        self._offsets_fingerprint = None  # (tuple(active_sinks), offsets_dict_tuple)
        self._mutation_lock = threading.Lock()  # serialises combine-sink load/unload

    # ── persistent state ─────────────────────────────────────────────────

    @staticmethod
    def _load_saved():
        for path in (INTERNAL_STATE, STATE_FILE):
            try:
                with open(path) as f:
                    data = json.load(f)
                if "active_sinks" in data:
                    # Strip legacy keys from 0.2.0 state
                    for legacy in ("virtual_sink_module_id", "loopback_modules"):
                        data.pop(legacy, None)
                    return data
            except Exception:
                pass
        return {
            "combine_module_id": None,
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

    # ── combine-sink lifecycle ───────────────────────────────────────────

    def _load_combine_sink(self, sinks):
        if not sinks:
            return False
        members = ",".join(sinks)
        mid = pactl(
            "load-module", "module-combine-sink",
            f"sink_name={VIRTUAL_SINK_NAME}",
            f"sinks={members}",
            "sink_properties=device.description=AudioMux",
        )
        if not mid.isdigit():
            log.error("failed to load combine-sink: %r", mid)
            return False
        module_id = int(mid)
        self._combine_module_id = module_id
        self._saved["combine_module_id"] = module_id
        self._mark_dirty()
        # Wait briefly for the sink to register before flipping default.
        for _ in range(20):
            if _has_sink(VIRTUAL_SINK_NAME):
                break
            time.sleep(0.05)
        pactl("set-default-sink", VIRTUAL_SINK_NAME)
        log.info("loaded combine-sink id=%d sinks=%s", module_id, list(sinks))
        return True

    def _unload_combine_sink(self):
        for mid in _live_combine_module_ids():
            pactl("unload-module", str(mid))
            log.info("unloaded combine-sink id=%d", mid)
        self._combine_module_id = None
        self._saved["combine_module_id"] = None
        self._mark_dirty()

    def _ensure_combine_sink(self, desired_sinks):
        """Make the live combine-sink's membership match `desired_sinks`.

        No-op when already correct. Reloads when membership changes.
        Held under `_mutation_lock` so concurrent callers (reconcile from the
        pw-dump monitor + set_active_sinks from the socket) can't both load
        a combine-sink at the same time.
        """
        desired = sorted(set(desired_sinks))
        with self._mutation_lock:
            live_ids = _live_combine_module_ids()

            if not desired:
                if live_ids:
                    self._unload_combine_sink()
                return

            if len(live_ids) == 1:
                current = sorted(_combine_member_sinks(live_ids[0]))
                if current == desired:
                    self._combine_module_id = live_ids[0]
                    self._saved["combine_module_id"] = live_ids[0]
                    return

            # Either >1 duplicates live, or membership drifted. Clean slate.
            if live_ids:
                self._unload_combine_sink()
            self._load_combine_sink(desired)

    # ── offset application ───────────────────────────────────────────────

    def _compute_active_offsets(self):
        """Return (active_offsets_dict, max_offset). Reads the offsets file."""
        offsets = load_offsets()
        active = self._saved.get("active_sinks") or []
        active_offsets = {n: int(offsets.get(n, 0) or 0) for n in active}
        max_offset = max(active_offsets.values()) if active_offsets else 0
        return active_offsets, max_offset

    def _apply_offsets(self, dump=None, force=False):
        """Write per-sink latencyOffsetNsec for every active sink.

        Offset semantics (unchanged from 0.2.0):
          offsets[sink] = this sink's measured hardware delay (ms, >= 0).
          The fastest sink (lowest offset) becomes the baseline; slower
          sinks get 0 compensation; faster sinks get (max_offset - offset)
          extra delay added so everyone arrives together.

        No-op when the fingerprint (active_sinks + offset values) hasn't
        changed since last apply. Pass force=True to re-apply regardless
        (e.g. after node IDs changed on hotplug).
        """
        active_offsets, max_offset = self._compute_active_offsets()
        fingerprint = tuple(sorted(active_offsets.items()))
        if not force and fingerprint == self._offsets_fingerprint:
            return
        if not active_offsets:
            self._offsets_fingerprint = fingerprint
            return
        if dump is None:
            dump = _pw_dump()
        for name, off in active_offsets.items():
            compensate_ms = max(0, max_offset - off)
            node_id = _find_sink_node_id(name, dump)
            if node_id is None:
                log.warning("apply_offsets: no node id for %s", name)
                continue
            _pw_set_latency_offset_ns(node_id, compensate_ms * 1_000_000)
        self._offsets_fingerprint = fingerprint
        log.info("applied offsets: %s (max=%d)", active_offsets, max_offset)

    def _clear_offset_for(self, sink_name, dump=None):
        """Zero latencyOffsetNsec on a single sink — used before calibration."""
        if dump is None:
            dump = _pw_dump()
        node_id = _find_sink_node_id(sink_name, dump)
        if node_id is not None:
            _pw_set_latency_offset_ns(node_id, 0)

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

        # If the user has switched the system default away from us and toward
        # a real sink, collapse: unload combine and step aside.
        if (server.default_sink_name != VIRTUAL_SINK_NAME
                and server.default_sink_name in sink_names):
            self._saved["active_sinks"] = [server.default_sink_name]
            self._unload_combine_sink()
            self._mark_dirty()
            self.flush_state()
            return

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

        self._ensure_combine_sink(active)
        self._saved["active_sinks"] = active
        self._saved["active_sources"] = active_sources
        self._mark_dirty()
        self.flush_state()
        self._apply_offsets()
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
        self._ensure_combine_sink(names)
        self._saved["active_sinks"] = names
        self._mark_dirty()
        self.flush_state()
        self._apply_offsets()
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
        offsets[name] = max(0, int(offset_ms))
        save_offsets(offsets)
        # Instant: no debounce, no rebuild — probe 2 confirmed live-editable.
        self._apply_offsets()

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

    # ── shutdown ─────────────────────────────────────────────────────────

    def shutdown(self):
        log.info("unloading combine-sink on shutdown")
        self._unload_combine_sink()


# ── SyncMeasurer ─────────────────────────────────────────────────────────────

class SyncMeasurer:
    """Phase-1 stub. Disabled by default. Phase 3 replaces this with passive
    pw-dump telemetry (no mic involvement)."""

    def __init__(self, graph):
        self._graph = graph
        self._history = collections.defaultdict(
            lambda: collections.deque(maxlen=MEASURE_WINDOW))
        self._exceed_count = collections.defaultdict(int)
        self._last_offset = {}
        self.enabled = False

    async def run(self):
        while True:
            await asyncio.sleep(MEASURE_INTERVAL)
            # Phase 3 replaces this body with pw-dump-based drift monitoring.

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
        return {
            "role": "follower",
            "offset_ms": 0,
            "drift_ms_per_min": 0,
            "jitter_ms": 0,
            "confidence": 0,
            "status": "not_measured",
        }


# ── PipeWireMonitor ──────────────────────────────────────────────────────────

class PipeWireMonitor:
    """Watch PipeWire graph changes via pw-dump --monitor."""

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
        """Run the acoustic-calibration subprocess.

        Phase 1 note: still uses the 0.2.0 `audiomux_calibrate.py` subprocess
        (Goertzel-based per-sink-tone measurement). Phase 2 replaces this with
        GCC-PHAT between mic and sink monitors — see REDESIGN.md §3.2.
        """
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

            # Phase 2 calibrator plays sweeps *directly* to each hardware
            # sink (sequentially, one at a time, to avoid acoustic crosstalk
            # between speakers). Tear the combine-sink down so any user
            # audio playing to audiomux_virtual can't bleed into the
            # measurement, and zero out each sink's latencyOffsetNsec so we
            # measure raw hardware delay rather than already-compensated.
            self._graph._unload_combine_sink()
            dump = _pw_dump()
            for sn in sinks:
                self._graph._clear_offset_for(sn, dump)
            self._graph._offsets_fingerprint = None

            # Build tone specs
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

            # Always restore the graph. A reconcile may have fired while
            # the combine-sink was unloaded and collapsed active_sinks —
            # reassert the full set here.
            log.info("calibration: restoring combine-sink")
            self._graph._saved["active_sinks"] = list(sinks)
            self._graph._mark_dirty()
            self._graph.flush_state()
            self._graph._ensure_combine_sink(sinks)
            self._graph._offsets_fingerprint = None
            self._graph._apply_offsets()

            if cal_error:
                self._graph.invalidate_state_cache()
                self._state_writer.notify()
                return {"ok": False, "error": cal_error}

            # Process results
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
                    log.info("calibration: round %d %s = %.1fms "
                             "(conf %.2f, %.0fHz)",
                             rn + 1, sn, delay, conf,
                             data.get("freq", 0))

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
                self._graph._apply_offsets()
                applied = {k: offsets.get(k, 0) for k in sinks}
                log.info("calibration: offsets applied %s", applied)
            else:
                log.warning("calibration: no usable readings")

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

    # Migration from 0.2.0: kill any stray pw-loopback children and remove
    # a legacy module-null-sink named audiomux_virtual before we load a
    # combine-sink with the same name.
    _kill_legacy_pw_loopbacks()
    legacy_null = _live_null_sink_module_id()
    if legacy_null is not None:
        log.info("migration: unloading legacy null-sink module-id=%d",
                 legacy_null)
        pactl("unload-module", str(legacy_null))

    graph = GraphManager()
    measurer = SyncMeasurer(graph)
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
