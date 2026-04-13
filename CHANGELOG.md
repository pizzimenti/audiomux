# Changelog

All notable changes to this project will be documented in this file.

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
