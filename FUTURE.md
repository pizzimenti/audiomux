# FUTURE

## Latency Sync

### Chosen Direction

Build a PipeWire-native sync service that owns AudioMux routing and keeps all
secondary sinks locked to a single master sink clock with continuous adaptive
rate matching.

This replaces the current "fixed loopback latency plus manual +/- buttons"
model with a real synchronization model:

- One sink is the master clock for the group.
- Every other sink is a follower.
- Followers use small continuous resampling adjustments to stay phase-aligned.
- Static device latency remains as a one-time calibration value, not the main
  runtime control.
- Runtime sync status is measured and exposed in the UI.

### Why This Is The Right End State

- It matches how serious multi-room systems work in public Apple/Sonos material:
  shared timing, future playout, and drift correction instead of manual nudges.
- It fits the real failure mode here: independent hardware clocks drift even if
  the initial offset is correct.
- It gives us a cleaner ownership model than stacking more behavior onto
  `module-loopback`.
- It leaves room for better routing, diagnostics, and automatic recovery later
  without rebuilding the UI again.

### Implementation Plan

1. Introduce a dedicated backend service, tentatively `audiomux-syncd`, that
   owns the output graph and publishes state for the plasmoid.
2. Move sink selection, master selection, static calibration, and live sync
   telemetry out of ad-hoc `pactl` module management and into the service.
3. Build a PipeWire graph with one virtual input sink and one playback branch
   per active physical sink, with the selected primary sink acting as the
   timing reference for the group.
4. Apply continuous rate matching on follower sinks so small clock drift is
   corrected by resampling rather than by unloading and recreating loopbacks.
5. Keep a per-device static trim value for hardware path latency differences,
   but make it calibration-only and editable in finer steps than the current
   `10 ms` buttons.
6. Add a measurement worker that samples monitor streams every few seconds,
   estimates offset, drift slope, jitter, and confidence, and writes that state
   to a runtime JSON file for the UI.
7. Surface sync telemetry in the plasmoid: current lag, drift trend,
   confidence, master sink, and degraded-sync warnings.
8. Add an automatic recovery policy for device hotplug, PipeWire/WirePlumber
   restarts, and follower sinks that fall outside the allowed correction
   window.
9. Keep the current manual offset controls only as a temporary compatibility
   layer during migration, then remove them once live sync is stable.
10. Replace the current hardcoded measurement script with a reusable diagnostic
    tool that can target any active sink pair and can run one-shot or watch
    mode.
11. Add validation coverage for three cases: static mismatch, slow drift over
    time, and graph rebuild after service restart.
12. Define merge criteria for the new path:
    auto-sync stays within a small audible tolerance for long-running playback,
    survives sink reconnects, and removes the need for routine manual offset
    changes.
13. Delete this file before merging the branch back to `main`.
