# Phase 0 probes

Three scripts that answer the open questions in `DESIGN.md` §6 before we commit
to the Phase 1 implementation.

| Probe | Question | Determines |
|---|---|---|
| `probe1_combine_driver.py` | Does `module-combine-stream` share one driver across backend sinks? | Whether the "one graph, one driver" story in §3.1 holds |
| `probe2_latency_offset.py` | Is `Props.latencyOffsetNsec` live-editable on a running sink? | Whether per-sink static offsets can avoid loopback rebuilds |
| `probe3_multichannel_capture.py` | Can N concurrent `parecord` captures stay sample-aligned when we play a shared click? | Whether GCC-PHAT against monitor streams will work without libpipewire alignment |

Run order doesn't matter; they're independent and clean up after themselves.

```
python3 probes/probe1_combine_driver.py
python3 probes/probe2_latency_offset.py
python3 probes/probe3_multichannel_capture.py
```

Each script writes a short interpretation block at the end describing what its
output means for the design. Record the results in `probes/RESULTS.md` so the
design decisions are traceable after the branch is merged.
