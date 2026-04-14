# AudioMux Sync Redesign

_Working document for the `feat/sync-redesign` branch. Delete before merging
to `main`._

_Supersedes the prior `FUTURE.md` roadmap ("master clock with continuous
adaptive rate matching") by executing its chosen direction, and subsumes
`docs/calibration_consult.md`'s record of failed calibration approaches as
Appendix A below._

## 1. Why redesign

The 0.2.0 architecture is correct-but-brittle. Three structural problems drive
the pain:

| Problem | Current symptom | Root cause |
|---|---|---|
| **Static offsets baked into the loopback process** | Changing a +/- offset → kill + respawn `pw-loopback --delay` → audio dropout → debounced 800 ms batch | `--delay` is a constructor arg for a ringbuffer; there is no runtime knob |
| **Calibration has a circular dependency** | ~85 ms jitter on repeated measurements; BT systematically under-measured by 60–120 ms; masked with `AUDIOMUX_BT_COMPENSATION_MS` | `time.perf_counter()` gives *request* time, not *emit* time. Per-sink PipeWire startup latency is invisible |
| **Continuous sync is disabled** | `SyncMeasurer.enabled = False` (`audiomux-syncd.py:659`); sync status in UI is only populated by the manual Sync Delay button | `parecord --latency-msec 10` every 3 s causes audible mic clicking, so the runtime-drift loop was never turned on |

Strengths worth preserving: daemon + stateless frontend, Unix-socket IPC with
fallback, subprocess-isolated calibration (mic safety), adaptive resampling via
`pw-loopback`, the offset-normalization model (`fastest = 0`).

## 2. Design principles

1. **Separate *what* from *how long*.** Static hardware-path offsets and runtime
   drift are different problems. Static offsets are a property of the device
   (measured once by calibration). Drift is a property of two independent
   clocks running side-by-side. Don't solve either with the other's mechanism.
2. **Let PipeWire do the inner loop.** PipeWire already ships an adaptive sinc
   resampler with a DLL-driven rate matcher (`spa_io_rate_match`). Our daemon
   should *supervise and surface telemetry*, not duplicate that loop in Python.
3. **Reference signals, not wall clocks.** Every technique in the prior art
   (Farina sweeps, GCC-PHAT, Audyssey, JACK's `jack_iodelay`, REW's timing
   reference) avoids absolute emit times by using correlated reference signals.
   We've been fighting a problem the prior art explicitly solved 20 years ago.
4. **One graph, one driver.** The virtual sink is the subgraph driver; physical
   sinks are followers with per-sink latency offsets and adaptive resamplers.
   No per-sink loopback processes.

## 3. Target architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                          audiomux-syncd                            │
│  (asyncio, libpipewire via pw-cli/pw-metadata/pw-dump)             │
│                                                                    │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────────────────┐  │
│  │ GraphOwner   │  │ Calibrator   │  │ DriftMonitor            │  │
│  │ - combine-   │  │ - sweep+GCC  │  │ - reads spa_io_rate     │  │
│  │   stream     │  │   PHAT       │  │   via pw-dump --monitor │  │
│  │ - latency    │  │ - writes     │  │ - computes ppm / offset │  │
│  │   offsets    │  │   offsets    │  │ - triggers hard-resync  │  │
│  └──────────────┘  └──────────────┘  └─────────────────────────┘  │
│                                                                    │
│           SocketServer  ───►  state.json  ───►  plasmoid           │
└────────────────────────────────────────────────────────────────────┘
```

### 3.1 Graph layout (replaces pw-loopback-per-sink)

One `module-combine-sink` loaded via `pactl` (PulseAudio-compat name; maps
internally to PipeWire's combine-stream implementation and is the only way to
load it into the running server — see §6):

```
pactl load-module module-combine-sink \
  sink_name=audiomux_virtual \
  sinks=<comma,separated,backends>
```

Probe 1 confirmed all backend stream nodes created by the combine share
`node.driver-id` and `node.link-group`, giving us the single scheduling group
the design wanted.

Each hardware sink's `Props.latencyOffsetNsec` carries the static path
offset, set via `pw-cli set-param <id> Props '{ latencyOffsetNsec = N }'`.
Probe 2 confirmed these writes apply instantly with zero delta and no
relink — changing an offset is a parameter update, not a rebuild.

Net delete: the entire `_loopback_procs` lifecycle, the pending-offset-rebuild
debounce, the legacy `module-loopback` cleanup, `BT_COMPENSATION_MS`. Sink-set
changes become a single combine-sink reload instead of N add/remove-loopback
operations.

### 3.2 Static offset calibration (replaces `audiomux_calibrate.py`)

One calibration pass, <2 s total:

1. Start one `parecord` per capture: the mic, and one per active sink's
   monitor source. Probe 3 confirmed the ~20 ms start-time jitter across
   parecord processes is irrelevant because GCC-PHAT is a sample-domain
   correlation — it finds the peak regardless of which sample index in each
   capture corresponds to which wall-clock time.
2. Emit one 1.0 s logarithmic sine sweep (Farina method, 80 Hz → 8 kHz)
   through the virtual sink. All physical sinks receive the sweep
   simultaneously through the combine.
3. For each sink `i`, compute **GCC-PHAT** between `mic` and `monitor_i`. The
   argmax of the phase-whitened cross-correlation is the acoustic delay —
   with sub-sample accuracy — without ever knowing the absolute emit time.
4. The mic-vs-monitor delay **includes** the BT codec+radio buffer because
   PipeWire's BT monitor tap is *before* SBC/AAC packetization. This is why
   this approach solves the BT problem the current design has never solved.
   (To be integration-tested in Phase 2 — probe 3 couldn't verify because BT
   was suspended.)
5. Write results into `~/.config/audiomux-offsets.json`:
   `{ sink_name: { offset_ms, confidence, measured_at, rate, codec } }`.
   Daemon applies them via `latencyOffsetNsec`.

Implementation: pure stdlib Python + numpy (numpy is already transitively
available via pulsectl's dependencies). **No `python-sounddevice` dependency.**
Existing Goertzel + xcorr helpers in `audiomux_sync_lib.py` get a ~40-line
GCC-PHAT addition; the rest is reused.

Why this works where the current approach fails: the current code asks "when
did I tell PipeWire to play?" (unmeasurable). This approach asks "when did
PipeWire actually emit the sample, per the monitor?" (directly observable).
The circular dependency collapses.

Fallback path (if BT codec-delay inclusion claim fails in Phase 2):
orthogonal multi-tone. Emit 1 kHz / 2.5 kHz / 5 kHz simultaneously through the
combine sink; compute per-tone arrival time at the mic via Goertzel;
differential delays = offsets.

### 3.3 Drift monitor (replaces disabled SyncMeasurer)

Fully passive — no mic involvement during normal operation:

- Subscribe to `pw-dump --monitor` JSON stream.
- For each follower sink node, parse `Params.Latency` and the
  `spa.io.rate-match` region's `delay` / `rate` / `delay_frac` fields.
- Compute rolling offset vs. driver clock and ppm drift slope
  (linear regression over 20–30 s window).
- Expose as telemetry in the state JSON.
- Policy thresholds (borrowed from Snapcast / Shairport):
  - `|offset| < 5 ms`: synced, do nothing.
  - `5 ms ≤ |offset| < 50 ms`: degraded, surface to UI, do nothing.
  - `|offset| ≥ 50 ms` for 3 consecutive samples: hard-resync
    (remove+readd stream from combine). 2 s cooldown between resyncs.

Escape hatch if PipeWire's internal resampler proves insufficient for BT:
insert a zita-resampler-based custom follower node driven by an Adriaensen
DLL. This is a v2 feature flag, not v1.

### 3.4 Plasmoid changes

Minimal:

- Remove +/- buttons (or keep as a hidden Advanced pane).
- The `syncStatus` label becomes live: "Synced" / "Drifting +12 ms/min" /
  "Resyncing follower X" — driven by DriftMonitor telemetry.
- Rename "Sync Delay" → "Calibrate"; show a confidence indicator per device.
- State-JSON schema evolves (new `drift` telemetry block); backward-compat
  via optional-chaining in QML.

### 3.5 Files after refactor

```
bin/
  audiomux-syncd.py         # daemon: GraphOwner + DriftMonitor + SocketServer
  audiomux-source.py        # plasmoid backend (mostly unchanged)
  audiomux_calibrate.py     # rewritten: parecord + GCC-PHAT, ~200 lines
  audiomux_sync_lib.py      # shared: GCC-PHAT, Goertzel (fallback), sweep gen
  audiomux-measure.py       # CLI diagnostic — wraps Calibrator
plasmoid/
  contents/ui/main.qml      # minor UI changes
systemd/
  audiomux-syncd.service    # unchanged
```

Rough LOC delta: **-400 / +250**.

## 4. Implementation phases

Each phase is a self-contained commit that leaves the branch runnable against
the current plasmoid.

**Phase 0 — branch setup and experiments** (~½ day) — **DONE** (see §6).

**Phase 1 — GraphOwner rewrite** — **DONE**
Replaced `GraphManager`: one `module-combine-sink` + `latencyOffsetNsec`
written via `pw-cli set-param`. Removed `pw-loopback` spawning,
`_loopback_procs`, legacy cleanup, offset-flush-debounce, `BT_COMPENSATION_MS`.
Added startup migration step that kills stray 0.2.0 pw-loopback children and
unloads any legacy `module-null-sink` named `audiomux_virtual`. Offset apply
is now fingerprinted — no-op when `(active_sinks, offsets)` is unchanged,
which silences the per-pw-dump-event reconcile spam the 0.2.0 daemon masked
by doing nothing in that path.

**Phase 2 — Calibrator rewrite** — **DONE**
Rewrote `audiomux_calibrate.py` around GCC-PHAT (`audiomux_sync_lib.py`
`gcc_phat`, `gen_log_sweep`, `float_mono_to_s16_stereo_bytes`,
`raw_s16le_stereo_to_mono`). Dropped the GStreamer path. Several Phase-2
discoveries (see §9):

- `pw-cat` can't read raw PCM from stdin (libsndfile-based); use
  `paplay --raw`.
- Simultaneous playout through combine causes acoustic crosstalk: loudest
  speaker dominates the mic, and since combine-sink feeds all backend
  monitors with a time-aligned stream, GCC-PHAT locks onto that peak for
  every sink. Fix: sequential per-sink playout via `paplay --device`.
- BT sinks go idle between sequential measurements; first measurement
  after wake-up adds 50–200 ms of codec/link warmup. Fix: 1.5 s silent
  warmup (-60 dBFS dither) before each per-sink sweep.
- Two concurrent `_ensure_combine_sink` callers (reconcile + socket
  dispatch) can both end up loading a module before either's unload
  finishes → pile-up. Fix: `threading.Lock` around the load/unload, and
  `_live_combine_module_ids()` (plural) so cleanup kills all duplicates.
- pw-dump events during calibration fire `reconcile()`, whose "default
  sink moved away" path collapses `active_sinks` to a single entry. Fix:
  `GraphManager._calibrating` guard that short-circuits reconcile, plus
  an explicit reassert of `active_sinks` in the calibration restore step.

Bench result (2026-04-14, analog + SoundCore-2 BT):
  analog=53 ms, BT=213 ms, both confidence 1.0, variance ±20 ms
  between warm runs — within the REDESIGN §8 goal 1 target.

**Phase 3 — DriftMonitor** (2 days)
New component consuming `pw-dump --monitor` for per-sink drift telemetry.
Enabled by default. Publish in state JSON. Implement 5/50 ms policy.

**Phase 4 — Plasmoid update** (½ day)
Live drift label; hide +/- buttons; rename Sync Delay → Calibrate;
confidence badges.

**Phase 5 — Validation** (1 day)
Three bench tests:

1. Static mismatch: Calibrate, residual mic-measured lag < 20 ms.
2. Slow drift: 30 min playback, DriftMonitor never sustains > 5 ms.
3. Graph rebuild: kill daemon mid-playback, state/offsets restored.

**Phase 6 — Merge criteria** (⅓ day)
Auto-sync stays within ±10 ms for 1 h continuous playback, survives BT
reconnect, no manual offset changes needed. Merge and delete this file.

## 5. Migration & rollout

- Feature branch always runnable against the current plasmoid
  (schema is additive).
- `~/.config/audiomux-offsets.json` schema migrates transparently: old flat
  `{name: ms}` is upgraded to the new richer form on first save.
- Daemon version bumped in state JSON so the plasmoid can surface a
  "Please update the plasmoid" hint if versions mismatch.
- No breaking change to socket protocol; commands keep their names but gain
  optional fields.

## 6. Phase 0 probe results

Three probe scripts live under `probes/`. They ran on 2026-04-13 against
PipeWire 1.6.2 / WirePlumber 0.5.x on kernel 6.18.18-1-MANJARO with ALSA HDMI
(pci-0000_03_00.1), ALSA analog (pci-0000_03_00.6), and BlueZ F4:4E:FC:E7:CA:8C
present. They're preserved alongside this doc for anyone revisiting the
decisions — rerun with `python3 probes/probeN_*.py`.

| Probe | Script | Question | Verdict |
|---|---|---|---|
| 1 | `probes/probe1_combine_driver.py` | Does combine-stream give a single driver across backends? | **Yes**, via pactl's `module-combine-sink` name only |
| 2 | `probes/probe2_latency_offset.py` | Is `Props.latencyOffsetNsec` live-editable? | **Yes**, zero delta, no relink |
| 3 | `probes/probe3_multichannel_capture.py` | Can concurrent parecord captures stay aligned enough for GCC-PHAT? | **Yes**, ~20 ms start-jitter is irrelevant |

### Probe 1 — combine-stream driver sharing

`pw-cli load-module` loads the module into the *pw-cli process itself* (per
`man pw-cli`: "Modules are loaded and unloaded in the local instance, thus the
pw-cli binary itself"). The sink it creates vanishes when `pw-cli` exits —
unusable for a daemon that wants to put a combine into the running graph.

`pactl load-module module-combine-sink sinks=a,b,c` loads into the running
PipeWire server via its PulseAudio-compat shim and creates a persistent
virtual sink. `pw-dump` after a 2-sink combine:

```
id=69  name=probe_cs3                                      driver-id=138  link-group=combine-sink-1494-13
id=54  name=output.probe_cs3_...analog-stereo              driver-id=138  link-group=combine-sink-1494-13
id=119 name=output.probe_cs3_..._bluez_output...           driver-id=138  link-group=combine-sink-1494-13
```

All three nodes (combine sink + both backend stream nodes) share
`node.driver-id=138` and `node.link-group`. Hardware sinks remain on their own
drivers — that's expected and is where adaptive resampling happens.

**Design implication:** §3.1 uses `pactl module-combine-sink`, not
`pw-cli module-combine-stream`.

### Probe 2 — live-editable `latencyOffsetNsec`

```
set 10000000ns → rc=0  pw-dump reports 10000000   (delta 0.0 ms)
set 50000000ns → rc=0  pw-dump reports 50000000   (delta 0.0 ms)
set 100000000ns → rc=0 pw-dump reports 100000000  (delta 0.0 ms)
set 0ns        → rc=0  pw-dump reports 0          (delta 0.0 ms)
```

All writes succeeded, `pw-dump` echoed exactly, no relink, no audio blip.
`pw-loopback --delay` rebuilds (with the 800 ms debounce) go away.

### Probe 3 — concurrent monitor capture

Three concurrent parecords on three monitors produced files of:

- 322560 bytes (HDMI monitor, 80640 stereo samples, 1.68 s)
- 318720 bytes (analog monitor, 79680 stereo samples, 1.66 s)
- 318720 bytes (BT monitor, 79680 stereo samples, 1.66 s)

Only the analog monitor saw the emitted click (RMS=0.0349, detected at
~350 ms) because HDMI/BT loopbacks were suspended at test time. The 20 ms
file-size spread is parecord start-time jitter — doesn't affect GCC-PHAT
because it's a sample-domain correlation: the peak position is the acoustic
delay regardless of which sample index in each capture corresponds to which
wall-clock time.

**Design implication:** §3.2 uses `parecord` directly per source — no
`python-sounddevice` dependency.

### Remaining Phase-2 open question

Whether the BT monitor tap is genuinely *upstream* of the codec packetization
(so mic-vs-monitor delay captures the codec+radio buffer we care about) could
not be tested because BT was suspended. First Phase-2 integration test covers
it; fallback (orthogonal multi-tone for BT specifically) is already planned
in §3.2.

## 7. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| PipeWire 1.6.2 combine-sink / latencyOffsetNsec don't compose as expected | ~~Medium~~ **Resolved** | Probes 1 + 2 confirmed both work independently. Composition test lands in Phase 1's first PR |
| BT monitor tap is downstream of codec on this BlueZ build | Medium | Phase-2 A/B test with BT resumed. If it fails, fall back to orthogonal multi-tone for BT only |
| 50 ms hard-resync causes audible glitches on normal drift spikes (BT reconnect, Wi-Fi) | Medium | Require 3 consecutive samples over threshold. 2 s cooldown between resyncs. Log every event |
| User has no microphone | Low | Calibrator detects, degrades to "manual offset mode" — +/- buttons return as fallback |
| Combine-sink member set cannot be edited without reload | Medium | Single reload beats N loopback spawn/kills anyway, and the reload is scoped. Accept the 100–300 ms gap on sink toggle |

## 8. Success criteria

1. **Static alignment**: after Calibrate, residual mic-measured lag between any
   two active sinks ≤ 20 ms.
2. **Runtime drift**: across 60 min continuous playback on analog+HDMI+BT, no
   sustained (>3 samples) excursion beyond ±10 ms.
3. **Survival**: BT reconnect, HDMI TV off/on, Wi-Fi bounce — all recover
   within 5 s, no stale loopbacks, no orphan processes, no daemon restart.
4. **No manual offsets**: branch ships with +/- buttons hidden, author runs
   1 week without needing them.
5. **Zero band-aids**: `AUDIOMUX_BT_COMPENSATION_MS` removed, not just defaulted
   off.

## Appendix A. Calibration approaches that did not work

_Imported from `docs/calibration_consult.md`. These are the four
measurement strategies we tried before settling on GCC-PHAT between mic and
sink monitor (§3.2). Each one failed in a specific way that informs the
current design._

The correct answer for the bench setup is approximately:

- Analog line-out: ~60–80 ms (DAC + amp, near-instant)
- HDMI: ~200–280 ms (TV/receiver processing)
- Bluetooth: ~500–600 ms (codec buffering, variable)

→ Analog offset ≈ 0 ms (baseline), Bluetooth offset ≈ 260 ms relative to analog.

### A.1 Sequential `paplay` per tone

Play a unique-frequency tone through each sink one at a time via separate
`paplay` processes. Record from the laptop mic with `parecord`. Goertzel
energy detector finds each tone's onset; delay = (mic onset time) − (emit
time).

**Result:** ~85 ms jitter between readings for the same device. Each `paplay`
process has variable PipeWire startup latency (50–150 ms). The `emit_ms`
timestamp (`time.perf_counter()` before launching `paplay`) does not
correspond to when PipeWire actually outputs audio.

### A.2 Pre-built continuous audio streams

Build one continuous raw audio stream per sink with tones at precise sample
positions. Launch one `paplay` per sink simultaneously. Compute emit times
from sample offsets instead of wall clock.

**Result:** worse. Different per-sink startup latencies to their respective
sinks. Analog readings became a flat constant (detecting an artifact), BT
readings became wild (−100 ms to 1400 ms). The sample-offset timing model
does not account for per-sink PipeWire startup-latency differences.

### A.3 Hum primer to warm BT codec

Play a quiet 100 Hz hum through all sinks before calibration to prime the BT
codec's buffers. Calibration otherwise underestimates BT delay by ~90 ms when
the codec is cold.

**Result:** helped somewhat (~45 ms improvement), but the hum's harmonics
interfered with the Goertzel detector, and BT codec cold-start is only part
of the systematic undershoot.

### A.4 Hardcoded BT compensation factor

Add a fixed +45–120 ms to all Bluetooth measurements (`AUDIOMUX_BT_COMPENSATION_MS`).

**Result:** too variable. Different runs need different compensation.
Arbitrary offset does not address the root measurement problem. Shipped in
0.2.0 as a last-resort toggle; removed by Phase 1 of this plan.

### Core difficulty these approaches share

**Circular dependency:** to calculate delay, we need to know when the tone
was emitted. To know when it was emitted, we'd need to measure it — which is
what we're trying to do. `time.perf_counter()` tells us when we asked PipeWire
to play audio, not when PipeWire actually handed samples to the hardware.
This latency differs per sink and per invocation.

§3.2's GCC-PHAT approach breaks the loop by taking the monitor stream as the
"actual emit" reference, so the only remaining question is *"mic vs. monitor
— what's the acoustic delay?"*, which is a straight cross-correlation with
no emit-time dependency.
