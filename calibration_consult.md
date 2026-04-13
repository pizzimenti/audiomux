# AudioMux Calibration Design Consultation

## Problem statement

We have a Linux audio multiplexer (AudioMux) that routes one virtual PipeWire
sink to multiple physical outputs simultaneously — HDMI, analog line-out, and
Bluetooth speakers. Each output has different hardware processing delay:

- **Analog line-out**: ~60-80ms (DAC + amp, near-instant)
- **HDMI**: ~200-280ms (TV/receiver processing)
- **Bluetooth**: ~500-600ms (codec buffering, variable)

We need to measure each output's real end-to-end delay so we can add
compensating delay to the faster outputs, bringing all outputs into audible
sync. The correct answer for our test setup is approximately:

- Analog: 0ms offset (fastest, baseline)
- Bluetooth: ~260ms offset (relative to analog)

## What we've tried

### Approach 1: Sequential paplay per tone
Play a unique-frequency tone through each sink one at a time using separate
`paplay` processes. Record from the laptop mic with `parecord`. Use a Goertzel
energy detector to find each tone's onset in the recording. Calculate delay as
(mic onset time) - (emit time).

**Result**: ~85ms jitter between readings for the SAME device. Each `paplay`
process has variable PipeWire startup latency (50-150ms). The `emit_ms`
timestamp (taken with `time.perf_counter()` before launching `paplay`) doesn't
correspond to when PipeWire actually outputs audio.

### Approach 2: Pre-built audio streams
Build one continuous raw audio stream per sink with tones at precise sample
positions. Launch one `paplay` per sink simultaneously. Calculate emit times
from sample offsets instead of wall clock.

**Result**: Worse. The two `paplay` processes have different startup latencies
to their respective sinks. Analog readings became a flat constant (detecting
an artifact), BT readings became wild (-100ms to 1400ms). The sample-offset
timing model doesn't account for per-sink PipeWire startup latency differences.

### Approach 3: Hum primer
Play a quiet 100Hz hum through all sinks before calibration to prime BT codec
buffers. Calibration underestimates BT delay by ~90ms when BT codec is cold.

**Result**: Helped somewhat (~45ms improvement) but the hum's harmonics
interfered with the Goertzel detector, and BT codec cold-start is only part
of the systematic undershoot.

### Approach 4: BT compensation factor
Add a hardcoded +45-120ms to all Bluetooth measurements.

**Result**: Too variable. Different runs need different compensation. Arbitrary
offset doesn't address the root measurement problem.

## Core difficulty

**Circular dependency**: to calculate delay, we need to know when the tone was
emitted. To know when it was emitted, we'd need to measure it — which is what
we're trying to do.

`time.perf_counter()` tells us when we asked PipeWire to play audio, but
PipeWire's internal buffering adds variable latency before audio reaches the
hardware. This latency differs per sink (analog vs BT) and per invocation.

## Constraints

- Linux, PipeWire 1.6.2, WirePlumber 0.5.13
- Python 3.14 daemon (audiomux-syncd)
- Calibration runs in an isolated subprocess (mic safety)
- Available tools: `paplay`, `parecord`, `pw-play`, `pw-record`, `pw-cat`,
  `pw-cli`, `pw-dump`, GStreamer with pipewiresrc/pipewiresink
- Laptop built-in mic for recording
- Must not hold the mic open outside calibration
- Calibration should take <5 seconds total
- Target accuracy: ±20ms (threshold of human perception for audio sync)

## System details

- Sinks: ALSA analog (PCI), ALSA HDMI (PCI), Bluetooth (SBC/AAC codec)
- Sample rate: 48000 Hz
- Default PipeWire quantum: 1024 samples (~21ms)
- pw-loopback base latency: 50ms

## Questions

1. Is there an approach that avoids needing to know absolute emit times?
   For example, could we play a REFERENCE tone through ALL sinks
   simultaneously and just measure the difference in arrival times at the
   mic? That would give relative delays directly without needing emit
   timestamps.

2. Can PipeWire's own timing metadata (clock, latency reporting via
   `pw-dump` or node properties) tell us each sink's actual output latency
   without acoustic measurement?

3. Is there a way to get sample-accurate playback timestamps from PipeWire
   (e.g., via GStreamer pipeline clock, or pw-stream callbacks) that would
   give us the actual moment audio reached the hardware?

4. Would a loopback approach work better — route audio to a sink AND record
   from that sink's monitor simultaneously, measuring the round-trip through
   PipeWire? This wouldn't capture hardware delay but would give us
   PipeWire's internal per-sink latency.

5. Are there existing tools or libraries (e.g., JACK's latency compensation,
   PipeWire's latency reporting API) designed for exactly this problem?
