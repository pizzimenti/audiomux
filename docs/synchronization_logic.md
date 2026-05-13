# AudioMux Synchronization Logic

## Intent

Multiple audio outputs (HDMI, analog line-out, Bluetooth speakers) each have
different hardware processing delays.  HDMI adds TV processing latency
(~100-250ms), Bluetooth adds codec buffering (~200-600ms), and analog is
near-instant (~10-80ms).  The goal is to delay the faster devices just enough
so all outputs arrive at the listener's ears at the same time, while keeping
total latency as low as possible for video sync.

### Key principles

1. **Fastest device = zero added delay.**  The device with the least hardware
   latency becomes the baseline.  All other devices are expressed as a
   positive offset relative to it.  This minimizes total latency — critical
   when audio accompanies video.

2. **Offsets are non-negative.**  A negative offset would mean "arrive before
   physically possible," which is incoherent.  The UI disables the minus
   button at zero, and the daemon clamps to `max(0, offset_ms)`.

3. **The mic is only used during explicit calibration.**  No background
   polling, no continuous measurement.  The mic is acquired when the user
   presses "Sync Delay," used for a few seconds, then released.

4. **Calibration must never hang.**  GStreamer pipelines and recording threads
   have hard timeouts.  If anything stalls, the calibration aborts, loopbacks
   are restored, and an error is reported.  The daemon itself has a 5-second
   force-exit watchdog on shutdown.

## Offset model

Each active sink has a stored **offset** in `~/.config/audiomux-offsets.json`.
The offset represents how much extra hardware latency this device has compared
to the fastest device in the group.

Example with three outputs:

| Device   | Raw hardware delay | Offset (normalized) |
|----------|--------------------|---------------------|
| Analog   | 80ms               | 0ms (fastest)       |
| HDMI     | 210ms              | 130ms               |
| Bluetooth| 580ms              | 500ms               |

## Delay computation

`GraphManager._compute_delays()` converts offsets to pw-loopback `--delay`
values:

```text
max_offset = max(all active offsets)
delay[device] = max_offset - offset[device]
```

Continuing the example:

| Device    | Offset | --delay       | Total arrival                        |
|-----------|--------|---------------|--------------------------------------|
| Analog    | 0ms    | 500ms         | 50ms(buffer) + 500ms + 80ms = 630ms |
| HDMI      | 130ms  | 370ms         | 50ms + 370ms + 210ms = 630ms        |
| Bluetooth | 500ms  | 0ms           | 50ms + 0ms + 580ms = 630ms          |

All arrive at 630ms.  The minimum possible total latency is the slowest
device's `buffer + hardware_delay` — we cannot beat that without dropping the
slow device.  Unchecking Bluetooth before calibrating reduces the spread to
130ms instead of 500ms.

## Acoustic calibration ("Sync Delay" button)

### Architecture

One continuous mic recording session covers all tone playback.  A single
shared clock (GStreamer pipeline clock or `time.perf_counter`) timestamps
both the mic recording start and each tone emission, so delay = (mic onset
time) - (emit time) with no inter-process timing jitter.

### Tone sequence

Round 1 (ascending): each speaker plays a unique frequency, one at a time.
250ms tone, 250ms silence gap, next speaker.

Round 2 (descending): same speakers, reversed frequency assignment.
Confirms round 1 readings and reveals drift.

Frequencies are from a D-major pentatonic (D5=587, A5=880, D6=1175, ...)
chosen to be pleasant, carry well on small speakers including Bluetooth, and
have enough separation for the Goertzel energy detector.

### Detection

`_bandpass_simple` computes per-block energy at each target frequency using a
Goertzel-like dot product (bandwidth=200Hz, block=5ms resolution).
`find_tone_edges` finds the onset by locating the first block where energy
exceeds 30% of the peak, searching only from slightly before the expected
emit time to avoid crosstalk from earlier tones.

### Normalization

After averaging the two rounds per sink, the minimum measured delay is
subtracted from all results so the fastest device becomes offset=0.
Devices that could not be measured (low confidence or negative delay) are
skipped and retain their previous offset.

### Robustness

- Calibration runs in a daemon thread with a hard timeout (measurement
  duration + 10 seconds).
- All GStreamer pipeline teardown is wrapped in try/except.
- Loopbacks are always restored after calibration, even on failure.
- The daemon's shutdown handler schedules `os._exit(1)` after 5 seconds as
  a watchdog against hung GStreamer pipelines.
- The uninstall script sends SIGKILL after 2 seconds if SIGTERM doesn't stop
  the daemon.

## Manual calibration (+/- buttons)

The UI provides +15ms / -15ms buttons per device for manual fine-tuning after
acoustic calibration.  The minus button is disabled when the offset reaches
zero.  Changes are debounced (800ms) before rebuilding the pw-loopback so
rapid clicks don't cause audio dropouts.

## pw-loopback vs pactl module-loopback

The daemon uses `pw-loopback` (PipeWire-native) instead of `pactl load-module
module-loopback` (PulseAudio compatibility).  Benefits:

- Adaptive resampling enabled by default (clock drift correction)
- `--delay` flag for precise offset compensation separate from buffer size
- Process lifecycle tied to the daemon (clean teardown)
- Lower base latency (50ms vs 200ms)
