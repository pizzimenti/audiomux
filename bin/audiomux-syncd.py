#!/usr/bin/env python3
"""
audiomux-syncd — AudioMux sync daemon

Long-running service that owns the PipeWire routing graph and exposes
state to the plasmoid via a runtime JSON file and a Unix domain socket.

Replaces the stateless per-invocation graph management that was previously
done directly by audiomux-source.py.

Loopbacks are created via pw-loopback (PipeWire-native, adaptive resampling
enabled by default) instead of pactl load-module module-loopback (PulseAudio
compat, resampling disabled).
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

# Shared measurement library (same directory)
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

BASE_LATENCY_MS    = 50    # pw-loopback buffer (much lower than legacy 200ms)
RECONCILE_INTERVAL = 30    # seconds between full reconciliation sweeps

# Sync measurement
MEASURE_INTERVAL   = 3     # seconds between measurements
MEASURE_DURATION   = 0.5   # seconds of audio to capture per measurement
MEASURE_WINDOW     = 20    # rolling window size (20 × 3s = 60s of history)
DRIFT_RECOVER_MS   = 30    # rebuild loopback if offset exceeds this for N cycles
DRIFT_RECOVER_COUNT = 3    # consecutive exceeded measurements before recovery
JUMP_THRESHOLD_MS  = 50    # immediate rebuild if offset jumps this much in one cycle

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


# ── graph queries ────────────────────────────────────────────────────────────

def _has_virtual_sink():
    result = subprocess.run(
        ["pactl", "list", "sinks", "short"],
        capture_output=True, text=True,
    )
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) > 1 and parts[1] == VIRTUAL_SINK_NAME:
            return True
    return False


def _live_virtual_sink_module_id():
    result = subprocess.run(
        ["pactl", "list", "modules", "short"],
        capture_output=True, text=True,
    )
    for line in result.stdout.splitlines():
        if "module-null-sink" not in line:
            continue
        if f"sink_name={VIRTUAL_SINK_NAME}" not in line:
            continue
        parts = line.split()
        if parts and parts[0].isdigit():
            return int(parts[0])
    return None


def _live_pactl_loopback_modules():
    """Return {sink_name: module_id} for legacy pactl loopbacks.

    Used during migration to clean up old-style loopbacks.
    """
    result = subprocess.run(
        ["pactl", "list", "modules", "short"],
        capture_output=True, text=True,
    )
    modules = {}
    for line in result.stdout.splitlines():
        if "module-loopback" not in line:
            continue
        if f"source={VIRTUAL_SINK_NAME}.monitor" not in line:
            continue
        parts = line.split()
        if not parts or not parts[0].isdigit():
            continue
        module_id = int(parts[0])
        for part in parts:
            if part.startswith("sink="):
                modules[part[5:]] = module_id
    return modules


def _preferred_name(primary, available, fallback=None):
    if primary in available:
        return primary
    if fallback in available:
        return fallback
    return next(iter(available), None)


# ── GraphManager ─────────────────────────────────────────────────────────────

class GraphManager:
    """Owns the PipeWire routing graph: virtual sink, loopbacks, sources.

    Loopbacks are managed as pw-loopback child processes.  When the daemon
    stops, all loopback processes are terminated — the daemon owns the graph.
    """

    def __init__(self):
        self._saved = self._load_saved()
        self._loopback_procs = {}   # {sink_name: subprocess.Popen}
        self._master_sink = None
        self._state_dirty = True
        self._sync_measurer = None  # set after SyncMeasurer is created
        self._reconciling = False   # re-entry guard
        self._legacy_cleaned = False
        self._pending_offset_rebuilds = {}  # {sink_name: timestamp}
        self._state_cache = None            # cached get_state_json result
        self._state_cache_time = 0          # when cache was last built

    # ── saved state (persisted across daemon restarts) ───────────────────

    @staticmethod
    def _load_saved():
        # Try daemon-specific state first, fall back to legacy state file
        for path in (INTERNAL_STATE, STATE_FILE):
            try:
                with open(path) as f:
                    data = json.load(f)
                if "active_sinks" in data or "virtual_sink_module_id" in data:
                    return data
            except Exception:
                pass
        return {
            "virtual_sink_module_id": None,
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

    # ── virtual sink (module-null-sink, server-side) ─────────────────────

    def _ensure_virtual_sink(self):
        live_mid = _live_virtual_sink_module_id()
        if live_mid is None or not _has_virtual_sink():
            self._saved["virtual_sink_module_id"] = None
        else:
            self._saved["virtual_sink_module_id"] = live_mid

        if self._saved.get("virtual_sink_module_id") is None:
            mid = pactl("load-module", "module-null-sink",
                        f"sink_name={VIRTUAL_SINK_NAME}",
                        "sink_properties=device.description=AudioMux")
            if mid.isdigit():
                self._saved["virtual_sink_module_id"] = int(mid)
                self._mark_dirty()
        if _has_virtual_sink():
            pactl("set-default-sink", VIRTUAL_SINK_NAME)

    # ── loopbacks (pw-loopback child processes) ──────────────────────────

    def _loopback_alive(self, sink_name):
        proc = self._loopback_procs.get(sink_name)
        return proc is not None and proc.poll() is None

    def _compute_delays(self):
        """Compute per-sink delay so the slowest device gets 0 extra delay
        and faster devices are delayed to match.

        Offset meaning: how much extra hardware latency this device has (ms).
        Higher offset = slower device.  The device with the highest offset
        gets --delay 0; others get enough delay to compensate."""
        offsets = load_offsets()
        active = set(self._saved.get("active_sinks") or [])
        active_offsets = {name: offsets.get(name, 0) for name in active}
        max_offset = max(active_offsets.values()) if active_offsets else 0
        return {name: max(0, max_offset - off)
                for name, off in active_offsets.items()}

    def _add_loopback(self, sink_name):
        if self._loopback_alive(sink_name):
            return
        # Kill stale process if it exited
        self._loopback_procs.pop(sink_name, None)

        delays = self._compute_delays()
        delay_ms = delays.get(sink_name, 0)
        delay_s = delay_ms / 1000.0

        # Sanitize sink name for use as a PipeWire node name
        safe_name = sink_name.replace(".", "_").replace(":", "_")

        cmd = [
            "pw-loopback",
            "--name", f"audiomux_lb_{safe_name}",
            "--group", f"audiomux_lb_{safe_name}",
            "--channels", "2",
            "--latency", str(BASE_LATENCY_MS),
            "--capture", VIRTUAL_SINK_NAME,
            "--capture-props", "stream.capture.sink=true",
            "--playback", sink_name,
        ]
        if delay_s > 0:
            cmd.extend(["--delay", f"{delay_s:.3f}"])

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._loopback_procs[sink_name] = proc
        log.info("started pw-loopback for %s (pid %d, delay %dms)",
                 sink_name, proc.pid, delay_ms)

    def _remove_loopback(self, sink_name):
        proc = self._loopback_procs.pop(sink_name, None)
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            log.info("stopped pw-loopback for %s (pid %d)", sink_name, proc.pid)

    def _remove_all_loopbacks(self):
        for sink_name in list(self._loopback_procs):
            self._remove_loopback(sink_name)

    def _active_loopback_sinks(self):
        """Return set of sink names with a live pw-loopback process."""
        return {name for name in self._loopback_procs
                if self._loopback_alive(name)}

    # ── master sink ──────────────────────────────────────────────────────

    def _resolve_master(self, active_sinks):
        """Pick the master sink from the active set."""
        if self._master_sink and self._master_sink in active_sinks:
            return self._master_sink
        if PRIMARY_SINK in active_sinks:
            return PRIMARY_SINK
        return next(iter(active_sinks)) if active_sinks else None

    # ── legacy cleanup ───────────────────────────────────────────────────

    def _cleanup_legacy_loopbacks(self):
        """Remove any old pactl module-loopback instances from before the
        switch to pw-loopback.  Skip sinks that already have a live
        pw-loopback process (pw-loopback may register a PulseAudio-visible
        module internally)."""
        legacy = _live_pactl_loopback_modules()
        for sink_name, mid in legacy.items():
            if self._loopback_alive(sink_name):
                continue
            log.info("cleaning up legacy module-loopback for %s (module %d)",
                     sink_name, mid)
            pactl("unload-module", mid)

    # ── reconciliation ───────────────────────────────────────────────────

    def reconcile(self):
        """Ensure the live graph matches desired state."""
        if self._reconciling:
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

        # Clean up legacy pactl loopbacks once on startup
        if not self._legacy_cleaned:
            self._cleanup_legacy_loopbacks()
            self._legacy_cleaned = True

        # If default sink moved away from AudioMux, collapse
        if server.default_sink_name != VIRTUAL_SINK_NAME:
            desired = ([server.default_sink_name]
                       if server.default_sink_name in sink_names else [])
            self._saved["active_sinks"] = desired
            self._remove_all_loopbacks()
            if (self._saved.get("virtual_sink_module_id") is not None
                    and _has_virtual_sink()):
                pactl("unload-module", self._saved["virtual_sink_module_id"])
                self._saved["virtual_sink_module_id"] = None
            self._mark_dirty()
            self.flush_state()
            return

        # Ensure virtual sink exists
        self._ensure_virtual_sink()

        # Reconcile active_sinks list against available sinks
        active = [
            n for n in (self._saved.get("active_sinks") or [])
            if n in sink_names
        ]
        if not active:
            fb = _preferred_name(
                PRIMARY_SINK, sink_names, server.default_sink_name)
            active = [fb] if fb else []

        active_set = set(active)

        # Reconcile active_sources
        active_sources = [
            n for n in (self._saved.get("active_sources") or [])
            if n in source_names
        ]
        if not active_sources:
            fb = _preferred_name(
                PRIMARY_SOURCE, source_names, server.default_source_name)
            active_sources = [fb] if fb else []

        # Kill loopbacks for sinks that are no longer active or available
        live = self._active_loopback_sinks()
        for sn in live - active_set:
            self._remove_loopback(sn)

        # Restart dead loopbacks, add new ones
        for sn in active_set:
            if not self._loopback_alive(sn):
                self._add_loopback(sn)

        self._master_sink = self._resolve_master(active_set)

        self._saved["active_sinks"] = active
        self._saved["active_sources"] = active_sources
        # Remove legacy key if present
        self._saved.pop("loopback_modules", None)
        self._mark_dirty()
        self.flush_state()

    # ── commands ─────────────────────────────────────────────────────────

    def get_state_json(self):
        """Return cached state, refreshing from PipeWire at most every 2s."""
        now = time.time()
        if self._state_cache and now - self._state_cache_time < 2.0:
            return self._state_cache
        self._state_cache = self._build_state_json()
        self._state_cache_time = now
        return self._state_cache

    def invalidate_state_cache(self):
        """Force next get_state_json to rebuild from PipeWire."""
        self._state_cache = None

    def _build_state_json(self):
        """Query PipeWire and build the JSON state dict."""
        offsets = load_offsets()
        active_sink_set = self._active_loopback_sinks()
        # If no loopbacks alive, trust saved state (might be starting up)
        if not active_sink_set:
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

            # If virtual sink is not active, trust system default
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

    def _set_active_sinks_inner(self, names, force_rebuild=False):
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

        names_set = set(names)
        current = self._active_loopback_sinks()

        self._ensure_virtual_sink()

        if force_rebuild:
            self._remove_all_loopbacks()
            current = set()

        for sn in current - names_set:
            self._remove_loopback(sn)
        for sn in names_set - current:
            self._add_loopback(sn)

        if force_rebuild:
            pactl("set-sink-mute", VIRTUAL_SINK_NAME, 0)
            for sn in names_set:
                pactl("set-sink-mute", sn, 0)

        self._master_sink = self._resolve_master(names_set)
        self._saved["active_sinks"] = list(names_set)
        self._mark_dirty()
        self.flush_state()

    def set_active_sinks(self, names):
        self._set_active_sinks_inner(names)

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
        offsets[name] = max(0, offset_ms)
        save_offsets(offsets)
        # Schedule a debounced rebuild of ALL loopbacks since the relative
        # delays depend on the max offset across all active sinks.
        now = time.time()
        for sn in list(self._loopback_procs):
            if self._loopback_alive(sn):
                self._pending_offset_rebuilds[sn] = now

    def flush_pending_offset_rebuilds(self):
        """Rebuild loopbacks whose offset changed, after a debounce period."""
        if not self._pending_offset_rebuilds:
            return
        now = time.time()
        ready = [name for name, ts in self._pending_offset_rebuilds.items()
                 if now - ts >= 0.8]
        for name in ready:
            del self._pending_offset_rebuilds[name]
            if self._loopback_alive(name):
                log.info("applying offset change for %s", name)
                self._remove_loopback(name)
                self._add_loopback(name)

    def set_source_offset(self, name, offset_ms):
        offsets = load_offsets()
        offsets[name] = offset_ms
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

    def shutdown(self):
        """Clean up all loopback processes."""
        log.info("terminating %d loopback processes", len(self._loopback_procs))
        self._remove_all_loopbacks()


# ── SyncMeasurer ─────────────────────────────────────────────────────────────

class SyncMeasurer:
    """Periodically measures sync offset between master and follower sinks.

    Runs cross-correlation of monitor stream captures every MEASURE_INTERVAL
    seconds.  Maintains a rolling window of measurements per follower sink
    and computes offset, drift rate, jitter, and confidence.  Triggers
    loopback rebuild if a follower drifts beyond the allowed threshold.
    """

    def __init__(self, graph):
        self._graph = graph
        # {sink_name: deque of (timestamp, offset_ms, confidence)}
        self._history = collections.defaultdict(
            lambda: collections.deque(maxlen=MEASURE_WINDOW))
        # {sink_name: consecutive count of exceeded measurements}
        self._exceed_count = collections.defaultdict(int)
        self._last_offset = {}  # {sink_name: last measured offset_ms}
        self.enabled = False  # disabled by default; parecord polling causes mic clicking

    async def run(self):
        while True:
            await asyncio.sleep(MEASURE_INTERVAL)
            if not self.enabled:
                continue
            try:
                await self._measure_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("sync measurement failed")

    async def _measure_all(self):
        master = self._graph._master_sink
        active = self._graph._active_loopback_sinks()

        if not master or master not in active or len(active) < 2:
            return

        followers = active - {master}

        for follower in followers:
            try:
                offset_ms, confidence = await asyncio.get_event_loop().run_in_executor(
                    None, self._measure_pair, master, follower)
            except Exception:
                log.exception("measurement failed for %s vs %s",
                              master, follower)
                continue

            now = time.time()

            if confidence < 0.1:
                # Too quiet to measure — record as silent
                self._history[follower].append((now, None, 0.0))
                self._exceed_count[follower] = 0
                continue

            # Check for sudden jump (BT rebuffer, HDMI resync, etc.)
            prev_offset = self._last_offset.get(follower)
            self._last_offset[follower] = offset_ms

            self._history[follower].append((now, offset_ms, confidence))

            if (prev_offset is not None
                    and confidence > 0.5
                    and abs(offset_ms - prev_offset) > JUMP_THRESHOLD_MS):
                log.warning(
                    "follower %s jumped %.1fms → %.1fms (delta %.1fms) "
                    "— immediate rebuild",
                    follower, prev_offset, offset_ms,
                    offset_ms - prev_offset)
                self._graph._remove_loopback(follower)
                self._graph._add_loopback(follower)
                self._exceed_count[follower] = 0
                self._last_offset[follower] = None
                continue

            # Sustained drift recovery
            if abs(offset_ms) > DRIFT_RECOVER_MS:
                self._exceed_count[follower] += 1
                if self._exceed_count[follower] >= DRIFT_RECOVER_COUNT:
                    log.warning(
                        "follower %s offset %.1fms exceeds threshold "
                        "for %d consecutive measurements — rebuilding",
                        follower, offset_ms, DRIFT_RECOVER_COUNT)
                    self._graph._remove_loopback(follower)
                    self._graph._add_loopback(follower)
                    self._exceed_count[follower] = 0
                    self._last_offset[follower] = None
            else:
                self._exceed_count[follower] = 0

    @staticmethod
    def _measure_pair(master_sink, follower_sink):
        """Record from both monitors, cross-correlate.  Runs in a thread."""
        master_buf = []
        follower_buf = []
        errors = []

        def rec(sink, buf):
            try:
                buf.extend(record_monitor(sink, MEASURE_DURATION))
            except Exception as e:
                errors.append(str(e))

        t1 = threading.Thread(target=rec, args=(master_sink, master_buf))
        t2 = threading.Thread(target=rec, args=(follower_sink, follower_buf))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        if errors:
            raise RuntimeError("; ".join(errors))

        if is_silence(master_buf) or is_silence(follower_buf):
            return 0.0, 0.0  # silent — can't measure

        ds_master = downsample(master_buf)
        ds_follower = downsample(follower_buf)

        lag_samples, corr = xcorr_offset(ds_master, ds_follower)
        return lag_to_ms(lag_samples), corr

    def get_sync_info(self, sink_name):
        """Return sync telemetry dict for a given sink."""
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

        history = self._history.get(sink_name)
        if not history or len(history) == 0:
            return {
                "role": "follower",
                "offset_ms": 0,
                "drift_ms_per_min": 0,
                "jitter_ms": 0,
                "confidence": 0,
                "status": "measuring",
            }

        # Filter to measurements with actual data (not silent)
        valid = [(ts, off, conf) for ts, off, conf in history
                 if off is not None and conf > 0.1]

        if not valid:
            return {
                "role": "follower",
                "offset_ms": 0,
                "drift_ms_per_min": 0,
                "jitter_ms": 0,
                "confidence": 0,
                "status": "silent",
            }

        offsets = [off for _, off, _ in valid]
        confidences = [conf for _, _, conf in valid]

        current_offset = offsets[-1]
        mean_confidence = sum(confidences) / len(confidences)
        jitter = (math.sqrt(sum((o - sum(offsets) / len(offsets)) ** 2
                                for o in offsets) / len(offsets))
                  if len(offsets) > 1 else 0)

        # Drift rate via simple linear regression over timestamps
        drift = 0.0
        if len(valid) >= 3:
            ts_list = [ts for ts, _, _ in valid]
            t0 = ts_list[0]
            xs = [t - t0 for t in ts_list]
            ys = offsets
            n = len(xs)
            sum_x = sum(xs)
            sum_y = sum(ys)
            sum_xy = sum(x * y for x, y in zip(xs, ys))
            sum_x2 = sum(x * x for x in xs)
            denom = n * sum_x2 - sum_x * sum_x
            if denom != 0:
                slope = (n * sum_xy - sum_x * sum_y) / denom
                drift = slope * 60  # ms per minute

        if abs(current_offset) > DRIFT_RECOVER_MS:
            status = "degraded"
        elif mean_confidence < 0.5:
            status = "degraded"
        else:
            status = "synced"

        return {
            "role": "follower",
            "offset_ms": round(current_offset, 1),
            "drift_ms_per_min": round(drift, 2),
            "jitter_ms": round(jitter, 1),
            "confidence": round(mean_confidence, 2),
            "status": status,
        }


# ── PipeWireMonitor ──────────────────────────────────────────────────────────

class PipeWireMonitor:
    """Watch PipeWire graph changes via pw-dump --monitor."""

    DEBOUNCE_SEC = 2.0  # min seconds between reconcile triggers

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

            # pw-dump --monitor emits top-level JSON arrays.
            # Track bracket depth to find complete arrays.
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
    """Debounced writer: publishes plasmoid-readable JSON at most 2Hz."""

    def __init__(self, graph):
        self._graph = graph
        self._pending = False
        self._min_interval = 0.5  # 2 Hz

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
    """Unix domain socket server for CLI commands."""

    def __init__(self, graph, state_writer, measurer=None):
        self._graph = graph
        self._state_writer = state_writer
        self._measurer = measurer
        self._server = None

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
            sock.close()
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
            # Run dispatch in executor for long-running commands (calibration)
            response = await asyncio.get_event_loop().run_in_executor(
                None, self._dispatch, request)
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        except asyncio.TimeoutError:
            log.warning("client timed out")
        except (ConnectionResetError, BrokenPipeError):
            pass  # client disconnected
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
                state = self._graph.get_state_json()
                return {"ok": True, "state": state}

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
                # Acoustic calibration runs in a SUBPROCESS so the
                # daemon never holds the mic.  If the subprocess hangs,
                # kill -9 releases everything.
                active = self._graph._active_loopback_sinks()
                if len(active) < 2:
                    return {"ok": False,
                            "error": "need 2+ active outputs to calibrate"}
                with pulsectl.Pulse("audiomux-calibrate") as pulse:
                    mic = pulse.server_info().default_source_name
                if not mic or "monitor" in mic:
                    return {"ok": False, "error": "no microphone found"}

                sinks = sorted(active)
                log.info("calibration: starting subprocess (mic=%s, "
                         "sinks=%s)", mic, sinks)

                # Mute loopbacks during calibration
                for sn in list(active):
                    self._graph._remove_loopback(sn)

                # Build tone specs: 8 tones in a rising diatonic scale,
                # alternating between speakers.  Each speaker gets 4 tones.
                n_tones = min(8, len(CALIBRATE_FREQS))
                round_specs = []
                for i in range(n_tones):
                    sink = sinks[i % len(sinks)]
                    freq = CALIBRATE_FREQS[i]
                    round_specs.append({"sink": sink, "freq": freq})

                # Run calibration in isolated subprocess
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

                # ALWAYS restore loopbacks
                log.info("calibration: restoring loopbacks")
                for sn in sinks:
                    self._graph._add_loopback(sn)

                if cal_error:
                    self._graph.invalidate_state_cache()
                    self._state_writer.notify()
                    return {"ok": False, "error": cal_error}

                # Process results from subprocess
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
                        log.info("calibration: round %d %s = "
                                 "%.1fms (conf %.2f, %.0fHz)",
                                 rn + 1, sn, delay, conf,
                                 data.get("freq", 0))

                # Average good measurements per sink
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

                # Apply offsets (normalized: fastest = 0)
                good_results = {k: v for k, v in results.items()
                                if "error" not in v}
                if good_results:
                    # Bluetooth codec latency is systematically
                    # underestimated by ~45ms because the calibration
                    # tone traverses the codec faster than real audio.
                    BT_COMPENSATION_MS = 0
                    for sn in good_results:
                        if "bluez" in sn:
                            good_results[sn] = dict(good_results[sn])
                            good_results[sn]["delay_ms"] += BT_COMPENSATION_MS
                            log.info("calibration: added %dms BT "
                                     "compensation to %s",
                                     BT_COMPENSATION_MS, sn)

                    min_delay = min(v["delay_ms"]
                                    for v in good_results.values())
                    offsets = load_offsets()
                    for sn, data in good_results.items():
                        offsets[sn] = max(0, round(
                            data["delay_ms"] - min_delay))
                    save_offsets(offsets)
                    log.info("calibration: normalized "
                             "(min=%.1fms subtracted)", min_delay)
                    # Rebuild with new delays
                    for sn in list(
                            self._graph._active_loopback_sinks()):
                        self._graph._remove_loopback(sn)
                    for sn in sinks:
                        self._graph._add_loopback(sn)
                    applied = {k: offsets.get(k, 0) for k in sinks}
                    log.info("calibration: offsets applied %s", applied)
                else:
                    log.warning("calibration: no usable readings")

                self._graph.invalidate_state_cache()
                self._state_writer.notify()
                return {"ok": True, "mic": mic, "results": results}

            elif cmd == "ping":
                return {"ok": True, "pid": os.getpid()}

            else:
                return {"ok": False, "error": f"unknown command: {cmd}"}

        except Exception as e:
            log.exception("command %s failed", cmd)
            return {"ok": False, "error": str(e)}


# ── main ─────────────────────────────────────────────────────────────────────

async def _main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log.info("starting audiomux-syncd (pid %d)", os.getpid())

    graph = GraphManager()
    measurer = SyncMeasurer(graph)
    graph._sync_measurer = measurer

    # Initial setup: establish virtual sink and loopbacks from saved state.
    # On a fresh start with no state, bootstrap with the primary or default sink.
    try:
        saved_sinks = graph._saved.get("active_sinks")
        if saved_sinks:
            graph._set_active_sinks_inner(saved_sinks)
        else:
            # No saved state — bootstrap with PRIMARY_SINK or current default
            with pulsectl.Pulse("audiomux-syncd-bootstrap") as pulse:
                server = pulse.server_info()
                boot_sink = PRIMARY_SINK if any(
                    s.name == PRIMARY_SINK for s in pulse.sink_list()
                ) else server.default_sink_name
            log.info("no saved state, bootstrapping with %s", boot_sink)
            graph._set_active_sinks_inner([boot_sink])
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
        # Schedule a hard exit in case graceful shutdown hangs
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
    offset_flush_task = asyncio.create_task(
        _offset_flush_loop(graph))

    await stop_event.wait()

    log.info("shutting down")
    monitor_task.cancel()
    writer_task.cancel()
    reconcile_task.cancel()
    measurer_task.cancel()
    offset_flush_task.cancel()
    await monitor.stop()
    await socket_server.stop()
    graph.shutdown()


def _reconcile_and_notify(graph, state_writer):
    try:
        graph.reconcile()
        state_writer.notify()
    except Exception:
        log.exception("reconciliation failed")


async def _offset_flush_loop(graph):
    """Check for pending offset rebuilds every 200ms."""
    while True:
        await asyncio.sleep(0.2)
        try:
            graph.flush_pending_offset_rebuilds()
        except Exception:
            log.exception("offset flush failed")


async def _periodic_reconcile(graph, state_writer):
    """Full reconciliation sweep as fallback for missed pw-dump events."""
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
