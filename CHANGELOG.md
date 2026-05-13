# Changelog

All notable changes to this project will be documented in this file.

## 0.3.0 - 2026-05-13

Sync-redesign cycle: replaces the brittle manual-delay model with
acoustic calibration + passive drift monitoring, and stops the daemon
from fighting WirePlumber over the system default sink.

### Added

- GCC-PHAT-based acoustic calibrator (`audiomux_calibrate.py` rewrite)
  for per-sink hardware delay measurement.
- Passive `DriftMonitor` that polls `Params.Latency.Input` via
  `pw-dump`, exposing per-follower drift telemetry in `sinks[].sync`
  without the constant `parecord` mic captures that caused audible
  clicking in 0.2.x.
- Plasmoid drift-status badge per active follower sink — "synced",
  "measuring…", or the current drift in ms — driven by the new
  DriftMonitor telemetry.
- Phase 0 probe scripts under `probes/` for the rejected
  combine-sink + `latencyOffsetNsec` graph shape (kept for reference).
- Observability logs for hotplug events, BT codec buffer-depth changes,
  manual offset edits, calibration peak-ratio per round, and per-
  follower drift summaries every 30 s.

### Changed

- Plasmoid header button renamed "Sync Delay" → "Calibrate"
  (in-progress label "Calibrating…") to match the calibrator rewrite.
- Per-sink +/- manual offset buttons hidden behind `showManualOffsets`
  (default false); kept as an escape hatch but no longer a primary
  tool now that calibration is reliable.
- Daemon now disengages entirely when `active_sinks` is ≤ 1, removing
  `audiomux_virtual` from the Plasma sink list when AudioMux isn't
  actually multiplexing anything. Re-engages automatically when a
  second sink is checked in the applet.
- Reconcile defers null-sink creation until after deciding to engage,
  avoiding a load + immediate teardown when the active set is small.
- Bumped daemon and plasmoid version to 0.3.0; daemon now publishes
  `daemon_version` in its state JSON so a future plasmoid can compare
  against its own version and surface a "please update" hint.

### Fixed

- Disengaged reconcile no longer reverts user clicks in the Plasma
  Audio Volume widget by repinning `active_sinks[0]` as default.
- Daemon bootstrap no longer pins the system default sink. Previously
  the bootstrap call to `set-default-sink` wrote PipeWire's
  `default.configured.audio.sink` metadata, which WirePlumber's
  `find-selected-default-node` hook gives a +30000 priority boost —
  silently overriding `priority.session` rules in
  `monitor.bluez.rules` / `monitor.alsa.rules`. The metadata
  outlives WirePlumber restarts, so a single bootstrap could clobber
  priority-based autoswitch indefinitely.
- Defends `audiomux_virtual` as default when running: WirePlumber's
  BT-sink auto-promotion no longer disarms the daemon mid-session by
  pulling the default away from the virtual sink. Explicit step-aside
  still available via the applet's Unbond action.

### Reverted

- The Phase-1 `module-combine-sink` + `Props.latencyOffsetNsec` graph
  shape introduced earlier in this cycle. Bench testing showed
  `latencyOffsetNsec` is a reporting hint to latency-aware clients,
  not an actual buffer-delay value — Phase 0 Probe 2 only verified the
  write landed, not that audio was delayed. Graph reverted to the
  0.2.0 shape (one `module-null-sink` named `audiomux_virtual` + one
  `pw-loopback` per active hardware sink with `--delay` from the
  calibration result). Migration on startup unloads any stray Phase-1
  combine-sink modules and zeroes leftover `latencyOffsetNsec` on
  hardware sink nodes.

## 0.2.0 - 2026-04-13

### Added

- Introduced the `audiomux-syncd` PipeWire-native sync daemon to own routing,
  per-sink loopback lifecycle, runtime state publishing, and socket IPC for the
  plasmoid.
- Added acoustic calibration via the plasmoid's `Sync Delay` action and the
  `bin/audiomux_calibrate.py` helper to measure real hardware delay per output.
- Added shared sync and measurement helpers in `bin/audiomux_sync_lib.py`,
  including monitor capture, cross-correlation, tone generation, and mic
  recording utilities.
- Added a systemd user unit for the sync daemon under
  `systemd/audiomux-syncd.service`.

### Changed

- Switched multi-output routing from stateless PulseAudio-compatible graph
  rebuilds toward daemon-managed `pw-loopback` playback branches with a 50ms
  base latency in daemon mode.
- Reworked the offset model so the fastest measured output becomes the zero
  point and other active sinks receive only the additional delay required to
  align with the slowest device.
- Updated the plasmoid UI to surface master/follower state, sync status, and
  calibration results while removing the automatic sink-offset reset on
  activation.
- Hardened install and uninstall flows for daemon lifecycle management,
  plasmashell refresh, panel applet placement, and safe panel config backup.
- Improved daemon fallback and diagnostics so daemon-only commands return clear
  errors when the daemon is not available.

### Fixed

- Preserved saved sink offsets when re-enabling an output instead of resetting
  them to `0ms`.
- Made `parecord` capture paths bounded and reliably cleaned up to avoid hung
  reads and leaked subprocess resources during monitor or mic capture.
- Corrected calibration result shaping so per-tone rounds align with the
  daemon's result processing logic.
- Added a configuration hook for Bluetooth compensation through
  `AUDIOMUX_BT_COMPENSATION_MS` and exposed calibration-in-progress state to
  installers and status checks.
