#!/usr/bin/env python3
"""
Measure timing offset between two PipeWire sink monitors.
Play audio through audiomux_virtual (music, video, anything with transients),
then run this script.

Usage:  python3 audiomux-measure.py
"""

import subprocess, threading, struct, sys, math

RATE      = 48000
DURATION  = 2.0
CHANNELS  = 2
S16_FRAME = CHANNELS * 2

HDMI   = "alsa_output.pci-0000_03_00.1.pro-output-7"
ANALOG = "alsa_output.pci-0000_03_00.6.analog-stereo"

# Downsample factor — xcorr runs on 48000/DS = 1000 Hz signal
DS = 48


def record_monitor(sink, duration):
    n_frames = int(duration * RATE)
    cmd = ["parecord", "--device", f"{sink}.monitor",
           "--rate", str(RATE), "--channels", str(CHANNELS),
           "--format", "s16le", "--raw", "--latency-msec", "10"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    raw = proc.stdout.read(n_frames * S16_FRAME)
    proc.terminate(); proc.wait()
    # Convert to mono float
    samples = []
    for i in range(0, len(raw) - S16_FRAME + 1, S16_FRAME):
        l, r = struct.unpack_from("<hh", raw, i)
        samples.append((l + r) / 2.0)
    return samples


def downsample(samples, factor):
    """Simple block-average downsample."""
    out = []
    for i in range(0, len(samples) - factor + 1, factor):
        out.append(sum(samples[i:i+factor]) / factor)
    return out


def xcorr_offset(a, b, max_lag_ms=500, ds_rate=1000):
    """Cross-correlate downsampled signals, return lag in original samples."""
    max_lag = int(max_lag_ms / 1000 * ds_rate)
    n = min(len(a), len(b))

    mean_a = sum(a) / n
    mean_b = sum(b) / n
    a = [x - mean_a for x in a[:n]]
    b = [x - mean_b for x in b[:n]]
    sa = math.sqrt(sum(x*x for x in a))
    sb = math.sqrt(sum(x*x for x in b))
    if sa == 0 or sb == 0:
        return 0, 0.0

    best_lag, best_corr = 0, -1.0
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            s = sum(x*y for x, y in zip(a[lag:], b[:n-lag]))
        else:
            s = sum(x*y for x, y in zip(a[:n+lag], b[-lag:]))
        c = s / (sa * sb)
        if c > best_corr:
            best_corr, best_lag = c, lag

    # Convert lag from ds_rate samples back to original samples
    return best_lag * DS, best_corr


def main():
    print("Recording 2s from both monitors — make sure audio is playing …", flush=True)

    hdmi_buf, analog_buf, errors = [], [], []

    def rec(sink, buf):
        try: buf.extend(record_monitor(sink, DURATION))
        except Exception as e: errors.append(f"{sink}: {e}")

    t1 = threading.Thread(target=rec, args=(HDMI, hdmi_buf))
    t2 = threading.Thread(target=rec, args=(ANALOG, analog_buf))
    t1.start(); t2.start()
    t1.join(); t2.join()

    if errors:
        for e in errors: print("ERROR:", e, file=sys.stderr)
        sys.exit(1)

    print(f"Captured {len(hdmi_buf)} + {len(analog_buf)} samples. Downsampling …", flush=True)
    ds_hdmi   = downsample(hdmi_buf,   DS)
    ds_analog = downsample(analog_buf, DS)

    print(f"Cross-correlating {len(ds_hdmi)} + {len(ds_analog)} points …", flush=True)
    lag_samples, corr = xcorr_offset(ds_hdmi, ds_analog)
    lag_ms = lag_samples / RATE * 1000

    print(f"\nCross-correlation peak: {corr:.4f}")
    print(f"Lag: {lag_samples} samples ≈ {lag_ms:+.1f} ms")
    if lag_ms > 1:
        print(f"→ HDMI is {lag_ms:.0f} ms faster than analog")
        print(f"  Add {lag_ms:.0f} to current SINK_LATENCY_EXTRA_MS for HDMI")
    elif lag_ms < -1:
        print(f"→ HDMI is {-lag_ms:.0f} ms slower than analog")
        print(f"  Subtract {-lag_ms:.0f} from current SINK_LATENCY_EXTRA_MS for HDMI")
    else:
        print("→ Outputs appear in sync!")


if __name__ == "__main__":
    main()
