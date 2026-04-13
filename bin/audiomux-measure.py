#!/usr/bin/env python3
"""
Measure timing offset between two PipeWire sink monitors.
Play audio through audiomux_virtual (music, video, anything with transients),
then run this script.

Usage:  python3 audiomux-measure.py [sink_a] [sink_b]
"""

import sys
import threading

from audiomux_sync_lib import (
    RATE, DS, record_monitor, downsample, xcorr_offset, lag_to_ms,
)

HDMI   = "alsa_output.pci-0000_03_00.1.pro-output-7"
ANALOG = "alsa_output.pci-0000_03_00.6.analog-stereo"

DURATION = 2.0


def main():
    sink_a = sys.argv[1] if len(sys.argv) > 1 else HDMI
    sink_b = sys.argv[2] if len(sys.argv) > 2 else ANALOG

    print(f"Recording {DURATION}s from monitors — make sure audio is playing …",
          flush=True)
    print(f"  A: {sink_a}")
    print(f"  B: {sink_b}")

    buf_a, buf_b, errors = [], [], []

    def rec(sink, buf):
        try:
            buf.extend(record_monitor(sink, DURATION))
        except Exception as e:
            errors.append(f"{sink}: {e}")

    t1 = threading.Thread(target=rec, args=(sink_a, buf_a))
    t2 = threading.Thread(target=rec, args=(sink_b, buf_b))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    if errors:
        for e in errors:
            print("ERROR:", e, file=sys.stderr)
        sys.exit(1)

    print(f"Captured {len(buf_a)} + {len(buf_b)} samples. Downsampling …",
          flush=True)
    ds_a = downsample(buf_a, DS)
    ds_b = downsample(buf_b, DS)

    print(f"Cross-correlating {len(ds_a)} + {len(ds_b)} points …", flush=True)
    lag_samples, corr = xcorr_offset(ds_a, ds_b)
    lag_ms = lag_to_ms(lag_samples)

    print(f"\nCross-correlation peak: {corr:.4f}")
    print(f"Lag: {lag_samples} samples ≈ {lag_ms:+.1f} ms")
    if lag_ms > 1:
        print(f"→ A is {lag_ms:.0f} ms faster than B")
        print(f"  Add {lag_ms:.0f} to A's offset to bring them in sync")
    elif lag_ms < -1:
        print(f"→ A is {-lag_ms:.0f} ms slower than B")
        print(f"  Subtract {-lag_ms:.0f} from A's offset to bring them in sync")
    else:
        print("→ Outputs appear in sync!")


if __name__ == "__main__":
    main()
