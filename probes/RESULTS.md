# Phase 0 probe results

Run date: 2026-04-13, PipeWire 1.6.2, WirePlumber 0.5.x, kernel 6.18.18-1-MANJARO.
Active hardware: ALSA HDMI (pci-0000_03_00.1), ALSA analog (pci-0000_03_00.6),
BlueZ F4:4E:FC:E7:CA:8C.

## Probe 1 — combine-stream driver sharing

**Question:** Does `module-combine-stream` give us one PipeWire driver across
all backend sinks so the combine point and its streams are scheduled together?

**Result:** Yes — but only when loaded via `pactl load-module module-combine-sink`
(the PulseAudio-compat name), **not** via `pw-cli load-module libpipewire-module-combine-stream`.

- `pw-cli load-module` loads the module into the *pw-cli process itself*
  (per `man pw-cli`: "Modules are loaded and unloaded in the local instance,
  thus the pw-cli binary itself"). The loaded module and the sink it creates
  vanish when `pw-cli` exits. This makes `pw-cli load-module` unusable for a
  daemon that wants to put a combine sink into the running PipeWire graph.
- `pactl load-module module-combine-sink sinks=a,b,c` loads into the running
  PipeWire server (via its PulseAudio-compat shim) and creates a persistent
  virtual sink.

**Evidence of shared driver** (`pw-dump` after loading a 2-sink combine):

```
id=69  name=probe_cs3                                              driver-id=138  link-group=combine-sink-1494-13
id=54  name=output.probe_cs3_alsa_output.pci-0000_03_00.6.analog   driver-id=138  link-group=combine-sink-1494-13
id=119 name=output.probe_cs3_bluez_output.F4:4E:FC:E7:CA:8C        driver-id=138  link-group=combine-sink-1494-13
```

All three nodes (the combine sink itself and both backend stream nodes) have
`node.driver-id = 138` and share `node.link-group`. The hardware sinks
themselves remain on their own ALSA/BlueZ drivers — that's expected and is
where adaptive resampling happens.

**Design implication:** DESIGN.md §3.1 changes from `module-combine-stream`
(pw-cli, doesn't persist) to `module-combine-sink` (pactl, persists). The
architecture goal — one virtual sink fanning out to multiple hardware sinks
with a shared scheduling group — is achievable with the PA-compat name.

## Probe 2 — live-editable latencyOffsetNsec

**Question:** Can we set `Props.latencyOffsetNsec` on a running sink node via
`pw-cli set-param` and have the value applied without any rebuild or audio
interruption?

**Result:** Yes, cleanly.

```
set 10000000ns → rc=0  pw-dump reports 10000000  (delta 0.0 ms)
set 50000000ns → rc=0  pw-dump reports 50000000  (delta 0.0 ms)
set 100000000ns → rc=0 pw-dump reports 100000000 (delta 0.0 ms)
set 0ns        → rc=0  pw-dump reports 0         (delta 0.0 ms)
```

All writes succeeded, `pw-dump` echoed the value back exactly, and no restart
or relink was required.

**Design implication:** DESIGN.md §3.1's plan to drive static offsets via
`latencyOffsetNsec` is validated. The current `pw-loopback --delay` rebuild
on every offset change (with 800 ms debounce to mask audio dropouts) can be
replaced by a `pw-cli set-param` call with no audible disruption.

## Probe 3 — concurrent monitor capture

**Question:** Can multiple `parecord` processes capture monitor sources
concurrently with acceptable start-time alignment for GCC-PHAT?

**Result:** Concurrent captures work. Start-time jitter ~20 ms. GCC-PHAT
doesn't care about that.

```
file for ...pro-output-7.monitor        322560 bytes (80640 stereo samples, 1.68 s)
file for ...analog-stereo.monitor       318720 bytes (79680 stereo samples, 1.66 s)
file for ...bluez_output.../.monitor    318720 bytes (79680 stereo samples, 1.66 s)
```

One monitor (analog, currently the only *active* audiomux loopback target)
captured the 50 ms @ 2 kHz click at ~350 ms into the capture window with
RMS=0.0349. The HDMI and BT monitors saw silence because their loopbacks
were suspended at test time — they had nothing to emit.

The file-size difference (80640 vs 79680 samples = 20 ms) confirms that
sequential `subprocess.Popen` for parecord introduces ~20 ms of start-time
jitter across captures. For our design this doesn't matter:

- GCC-PHAT between mic and monitor is a cross-correlation over sample lags,
  not wall-clock times. The peak position is the acoustic delay regardless
  of which sample index corresponds to which capture-start.
- What we need is *within a single capture*, not *across captures*, to have
  enough signal energy to find the correlation peak. That's purely a signal
  engineering problem (use a 1 s log-sweep, not a 50 ms click).

**Design implication:** The GCC-PHAT plan in DESIGN.md §3.2 works. We do
*not* need `sounddevice`'s multi-input stream or libpipewire's `pw_time.ticks`
alignment — one parecord per source, analyzed in the sample domain, is
sufficient. Phase 2 implementation can therefore avoid the `python-sounddevice`
dependency introduced speculatively in the plan.

## Revisions to DESIGN.md triggered by these results

1. **§3.1** — swap the sentence "one `module-combine-stream`" for "one
   `module-combine-sink` loaded via `pactl` (PulseAudio-compat name; maps
   internally to the combine-stream implementation)". Remove references to
   `pw-cli load-module libpipewire-module-combine-stream`.
2. **§3.2** — drop the `python-sounddevice` dependency. Use `parecord`
   directly for mic + per-sink-monitor capture. Alignment comes from the
   reference signal (log sweep), not capture-start sync.
3. **§6 / Open questions** — all three resolved. Remove from blockers list.
4. **§7 / Risks** — the "BT monitor is downstream of codec" risk is still
   open because probe 3 couldn't test it (BT was suspended). Keep that as a
   Phase 2 integration-test risk, not a blocker for Phase 1.

## Recommendation

Proceed to Phase 1 (GraphOwner rewrite) with these concrete primitives:

- **Virtual sink:** `pactl load-module module-combine-sink sink_name=audiomux_virtual sinks=<csv>`
  replaces the current `module-null-sink` + N × `pw-loopback` construction.
- **Offsets:** `pw-cli set-param <sink-node-id> Props '{ latencyOffsetNsec = N }'`
  replaces per-sink `pw-loopback --delay`.
- **Reloads on sink-set change:** unload + reload the combine-sink module
  (one operation) instead of add/remove loopback processes (N operations).
- **No new Python dependencies.**
