# AudioMux Sync Redesign — Design Document

_Working document for the `feat/sync-redesign` branch. Delete before merging to `main` (same convention as `FUTURE.md`)._

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
load it into the running server — see `probes/RESULTS.md`):

```
pactl load-module module-combine-sink \
  sink_name=audiomux_virtual \
  sinks=<comma,separated,backends>
```

Probe 1 confirmed all backend stream nodes created by the combine share
`node.driver-id` and `node.link-group`, giving us the single scheduling group
DESIGN wanted.

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

Fallback path (if simultaneous monitor capture is blocked — see §6):
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
  audiomux_calibrate.py     # rewritten: sounddevice + GCC-PHAT, ~200 lines
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

**Phase 0 — branch setup and experiments** (~½ day)
Three probe scripts answer the three open questions in §6. Don't touch daemon
code yet.

**Phase 1 — GraphOwner rewrite** (1–2 days)
Replace `GraphManager` with a class that loads a single `module-combine-stream`
and manages `combine.streams` + `latencyOffsetNsec`. Keep the existing offsets
JSON schema initially. Remove `pw-loopback` spawning, `_loopback_procs`, legacy
cleanup, offset-flush-debounce. Drop `BT_COMPENSATION_MS`.

**Phase 2 — Calibrator rewrite** (2 days)
Rewrite `audiomux_calibrate.py`: one sweep + multi-channel capture + GCC-PHAT.
Drop the GStreamer path. Land orthogonal-multi-tone fallback behind
`AUDIOMUX_CALIBRATE_METHOD=fallback`.

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
reconnect, no manual offset changes needed. Merge, delete `FUTURE.md` and
this file.

## 5. Migration & rollout

- Feature branch always runnable against the current plasmoid
  (schema is additive).
- `~/.config/audiomux-offsets.json` schema migrates transparently: old flat
  `{name: ms}` is upgraded to the new richer form on first save.
- Daemon version bumped in state JSON so the plasmoid can surface a
  "Please update the plasmoid" hint if versions mismatch.
- No breaking change to socket protocol; commands keep their names but gain
  optional fields.

## 6. Open questions — all resolved in Phase 0

All three Phase-0 probes ran on 2026-04-13; results in `probes/RESULTS.md`.

1. **combine driver sharing:** Yes via `pactl load-module module-combine-sink`.
   `pw-cli load-module` is **not** usable (loads into pw-cli process, dies on
   exit).
2. **latencyOffsetNsec live-editable:** Yes — zero delta, no relink, no audio
   interruption.
3. **Concurrent monitor capture:** Works. ~20 ms start jitter across parecord
   processes but that doesn't affect GCC-PHAT because it's a sample-domain
   correlation. No `sounddevice` dependency needed.

Remaining risk for Phase 2 (not a Phase 1 blocker): the BT codec-delay
inclusion claim — whether the BT monitor tap is actually upstream of codec
packetization — was not testable because BT was suspended during probes.
First Phase 2 integration test covers it.

## 7. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| PipeWire 1.6.2 combine-sink / latencyOffsetNsec don't compose as expected | ~~Medium~~ **Resolved** | Probes 1 + 2 confirmed both work independently. Probe that they compose will happen in Phase 1 first PR |
| BT monitor tap is downstream of codec on this BlueZ build | Medium | Phase-2 A/B test with BT resumed. If it fails, fall back to orthogonal multi-tone for BT only |
| 50 ms hard-resync causes audible glitches on normal drift spikes (BT reconnect, Wi-Fi) | Medium | Require 3 consecutive samples over threshold. 2 s cooldown between resyncs. Log every event |
| User has no microphone | Low | Calibrator detects, degrades to "manual offset mode" — +/- buttons return as fallback |
| Combine-sink member set cannot be edited without reload | Medium | Single reload beats N loopback spawn/kills anyway, and the reload is scoped (other apps on other sinks unaffected). Accept the 100–300 ms gap on sink toggle |

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
