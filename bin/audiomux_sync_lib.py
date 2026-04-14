"""
audiomux_sync_lib — shared cross-correlation and monitor-recording utilities

Used by both audiomux-syncd (continuous measurement) and audiomux-measure
(one-shot diagnostic).
"""

import math
import struct
import subprocess
import threading
import time

RATE      = 48000
CHANNELS  = 2
S16_FRAME = CHANNELS * 2

# Downsample factor — xcorr runs on 48000/DS = 1000 Hz signal
DS = 48
DS_RATE = RATE // DS  # 1000 Hz


def _capture_parecord_raw(cmd, duration, expected_bytes):
    """Capture raw bytes from parecord for a bounded duration."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stop_timer = threading.Timer(duration, proc.terminate)
    try:
        stop_timer.start()
        try:
            raw, stderr = proc.communicate(timeout=duration + 2.0)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            raw, stderr = proc.communicate()
            raise RuntimeError(
                f"parecord timed out after {duration + 2.0:.1f}s"
            ) from exc
        if proc.returncode not in (0, -15):
            err_text = (stderr or b"").decode(errors="replace").strip()
            raise RuntimeError(err_text or f"parecord exited with {proc.returncode}")
        return raw[:expected_bytes]
    finally:
        stop_timer.cancel()
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()


def record_monitor(sink, duration, rate=RATE):
    """Record from a sink's monitor source, return mono float samples."""
    n_frames = int(duration * rate)
    expected_bytes = n_frames * S16_FRAME
    cmd = [
        "parecord",
        "--device", f"{sink}.monitor",
        "--rate", str(rate),
        "--channels", str(CHANNELS),
        "--format", "s16le",
        "--raw",
        "--latency-msec", "10",
    ]
    raw = _capture_parecord_raw(cmd, duration, expected_bytes)
    return raw_to_mono(raw)


def downsample(samples, factor=DS):
    """Simple block-average downsample."""
    out = []
    for i in range(0, len(samples) - factor + 1, factor):
        out.append(sum(samples[i : i + factor]) / factor)
    return out


def is_silence(samples, threshold=50.0):
    """Return True if signal RMS is below threshold (near-silence)."""
    if not samples:
        return True
    rms = math.sqrt(sum(x * x for x in samples) / len(samples))
    return rms < threshold


def xcorr_offset(a, b, max_lag_ms=500, ds_rate=DS_RATE):
    """Cross-correlate downsampled signals.

    Returns (lag_samples_at_original_rate, correlation_coefficient).
    Positive lag means *a* leads *b* (a's signal arrives earlier).
    """
    max_lag = int(max_lag_ms / 1000 * ds_rate)
    n = min(len(a), len(b))
    if n == 0:
        return 0, 0.0

    mean_a = sum(a) / n
    mean_b = sum(b) / n
    a_centered = [x - mean_a for x in a[:n]]
    b_centered = [x - mean_b for x in b[:n]]
    sa = math.sqrt(sum(x * x for x in a_centered))
    sb = math.sqrt(sum(x * x for x in b_centered))
    if sa == 0 or sb == 0:
        return 0, 0.0

    best_lag, best_corr = 0, -1.0
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            s = sum(x * y for x, y in zip(a_centered[lag:], b_centered[: n - lag]))
        else:
            s = sum(x * y for x, y in zip(a_centered[: n + lag], b_centered[-lag:]))
        c = s / (sa * sb)
        if c > best_corr:
            best_corr, best_lag = c, lag

    # Convert lag from ds_rate back to original sample rate
    return best_lag * DS, best_corr


def lag_to_ms(lag_samples, rate=RATE):
    """Convert a lag in samples to milliseconds."""
    return lag_samples / rate * 1000


# ── acoustic calibration ────────────────────────────────────────────────────

# Hijaz / Phrygian dominant scale starting at D6 — 8 notes alternating
# between speakers.  The augmented 2nd (Eb→F#) gives the characteristic
# Middle Eastern color.  All above 1kHz for BT speaker compatibility.
CALIBRATE_FREQS = [
    1174.66,  # D6
    1244.51,  # Eb6
    1479.98,  # F#6  (augmented 2nd)
    1567.98,  # G6
    1760.00,  # A6
    1864.66,  # Bb6
    2093.00,  # C#7  (augmented 2nd)
    2349.32,  # D7
]
TONE_DURATION = 0.20   # seconds per tone — longer for better detection
TONE_GAP      = 0.10   # seconds of silence between tones
TONE_AMPLITUDE = 0.5
CALIBRATE_SETTLE = 0.25
CALIBRATE_TAIL   = 1.0
CALIBRATE_LATENCY_MSEC = 5
_GST_BUNDLE = None


HUM_FREQ       = 100   # Hz — low hum to prime codec buffers
HUM_AMPLITUDE  = 0.05  # very quiet — just enough to keep codecs in steady-state


def generate_hum_raw(duration, freq=HUM_FREQ, amplitude=HUM_AMPLITUDE,
                     rate=RATE):
    """Generate a quiet low-frequency sine hum as raw S16LE stereo bytes.

    Keeps BT codec buffers and hardware DAC paths in steady-state during
    calibration without being audibly intrusive.
    """
    n_frames = int(duration * rate)
    raw = bytearray()
    for i in range(n_frames):
        val = int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / rate))
        raw += struct.pack("<hh", val, val)
    return bytes(raw)


def generate_single_tone_raw(freq, duration=TONE_DURATION,
                             amplitude=TONE_AMPLITUDE, rate=RATE):
    """Generate a single-frequency sine tone as raw S16LE stereo bytes."""
    n_frames = int(duration * rate)
    raw = bytearray()
    for i in range(n_frames):
        val = int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / rate))
        raw += struct.pack("<hh", val, val)
    return bytes(raw)


def raw_to_mono(raw_bytes):
    """Convert raw S16LE stereo bytes to mono float sample list."""
    samples = []
    for i in range(0, len(raw_bytes) - S16_FRAME + 1, S16_FRAME):
        l, r = struct.unpack_from("<hh", raw_bytes, i)
        samples.append((l + r) / 2.0)
    return samples


def record_mic(device, duration, rate=RATE):
    """Record from a real microphone input, return mono float samples."""
    n_frames = int(duration * rate)
    expected_bytes = n_frames * S16_FRAME
    cmd = [
        "parecord",
        "--device", device,
        "--rate", str(rate),
        "--channels", str(CHANNELS),
        "--format", "s16le",
        "--raw",
        "--latency-msec", "5",
    ]
    raw = _capture_parecord_raw(cmd, duration, expected_bytes)
    return raw_to_mono(raw)


def _energy_at(samples, pos, window):
    """RMS energy of a short window at a given position."""
    end = min(pos + window, len(samples))
    seg = samples[pos:end]
    if not seg:
        return 0.0
    return math.sqrt(sum(x * x for x in seg) / len(seg))


def _bandpass_simple(samples, freq, rate, bandwidth=50):
    """Very simple single-frequency energy detector using Goertzel-like
    dot product against a sine/cosine at the target frequency.

    Returns a list of per-block energy values (block size = rate/bandwidth).
    """
    block = int(rate / bandwidth)
    energies = []
    for start in range(0, len(samples) - block, block):
        seg = samples[start : start + block]
        sin_sum = sum(s * math.sin(2 * math.pi * freq * i / rate)
                      for i, s in enumerate(seg))
        cos_sum = sum(s * math.cos(2 * math.pi * freq * i / rate)
                      for i, s in enumerate(seg))
        energies.append(math.sqrt(sin_sum ** 2 + cos_sum ** 2) / len(seg))
    return energies, block


def find_tone_edges(freq, mic_buf, rate=RATE, search_from_ms=0):
    """Find the onset and offset of a tone at a specific frequency in
    a mic recording.

    search_from_ms: only look for the onset starting from this point
        in the recording (prevents false detections from earlier tones'
        harmonics or crosstalk).

    Returns {"onset_ms", "offset_ms", "confidence"}.
    """
    energies, block = _bandpass_simple(mic_buf, freq, rate)

    if len(energies) < 4:
        return {"onset_ms": 0, "offset_ms": 0, "confidence": 0}

    # Convert search_from_ms to block index
    start_block = max(0, int(search_from_ms / 1000 * rate / block) - 2)

    # Find peak energy only within the search window
    search_energies = energies[start_block:]
    if not search_energies:
        return {"onset_ms": 0, "offset_ms": 0, "confidence": 0}

    peak = max(search_energies)
    if peak == 0:
        return {"onset_ms": 0, "offset_ms": 0, "confidence": 0}

    threshold = peak * 0.3

    # Noise floor from blocks before the search window
    noise_blocks = energies[:max(1, start_block)]
    noise_floor = sum(noise_blocks) / len(noise_blocks) if noise_blocks else 0

    # Find onset: first block above threshold within search window
    onset_block = start_block
    for i in range(start_block, len(energies)):
        if energies[i] > threshold:
            onset_block = i
            break

    # Find offset: last block above threshold
    offset_block = len(energies) - 1
    for i in range(len(energies) - 1, start_block - 1, -1):
        if energies[i] > threshold:
            offset_block = i
            break

    confidence = min(1.0, (peak / (noise_floor + 1e-10)) / 20)

    onset_ms = onset_block * block / rate * 1000
    offset_ms = offset_block * block / rate * 1000

    return {
        "onset_ms": round(onset_ms, 1),
        "offset_ms": round(offset_ms, 1),
        "confidence": round(confidence, 2),
    }


def _get_gst():
    global _GST_BUNDLE
    if _GST_BUNDLE is not None:
        return _GST_BUNDLE
    try:
        import gi
        gi.require_version("Gst", "1.0")
        gi.require_version("GstApp", "1.0")
        from gi.repository import Gst, GstApp
        Gst.init(None)
        _GST_BUNDLE = (Gst, GstApp)
    except Exception:
        _GST_BUNDLE = False
    return _GST_BUNDLE


def _audio_caps(rate):
    Gst, _ = _get_gst()
    return Gst.Caps.from_string(
        f"audio/x-raw,format=S16LE,layout=interleaved,"
        f"channels={CHANNELS},rate={rate}"
    )


class _GstMicRecorder:
    def __init__(self, device, rate):
        gst_bundle = _get_gst()
        if not gst_bundle:
            raise RuntimeError("GStreamer PipeWire bindings unavailable")
        Gst, _ = gst_bundle
        self._Gst = Gst
        self._raw = bytearray()
        self._stop = threading.Event()

        pipeline = Gst.Pipeline.new("audiomux-calibrate-recorder")
        src = Gst.ElementFactory.make("pipewiresrc", None)
        convert = Gst.ElementFactory.make("audioconvert", None)
        resample = Gst.ElementFactory.make("audioresample", None)
        capsfilter = Gst.ElementFactory.make("capsfilter", None)
        sink = Gst.ElementFactory.make("appsink", None)
        if not all([pipeline, src, convert, resample, capsfilter, sink]):
            raise RuntimeError("failed to create GStreamer recorder pipeline")

        src.set_property("target-object", device)
        src.set_property("client-name", "audiomux-calibrate-mic")
        src.set_property("do-timestamp", True)
        capsfilter.set_property("caps", _audio_caps(rate))
        sink.set_property("sync", False)
        sink.set_property("wait-on-eos", False)
        sink.set_property("max-buffers", 64)

        for elem in (src, convert, resample, capsfilter, sink):
            pipeline.add(elem)
        if not src.link(convert):
            raise RuntimeError("failed to link pipewiresrc -> audioconvert")
        if not convert.link(resample):
            raise RuntimeError("failed to link audioconvert -> audioresample")
        if not resample.link(capsfilter):
            raise RuntimeError("failed to link audioresample -> capsfilter")
        if not capsfilter.link(sink):
            raise RuntimeError("failed to link capsfilter -> appsink")

        self._pipeline = pipeline
        self._sink = sink
        self._thread = threading.Thread(
            target=self._pull_samples,
            name="audiomux-calibrate-recorder",
            daemon=True,
        )

    def _pull_samples(self):
        timeout_ns = 100_000_000
        while not self._stop.is_set():
            sample = self._sink.try_pull_sample(timeout_ns)
            if sample is None:
                continue
            buf = sample.get_buffer()
            if buf is None:
                continue
            self._raw.extend(buf.extract_dup(0, buf.get_size()))

    def start(self):
        ret = self._pipeline.set_state(self._Gst.State.PLAYING)
        if ret == self._Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("failed to start mic recorder")
        self._pipeline.get_state(self._Gst.SECOND)
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            self._pipeline.set_state(self._Gst.State.NULL)
        except Exception:
            pass
        self._thread.join(timeout=3)

    def samples(self):
        return raw_to_mono(bytes(self._raw))


class _GstTonePlayer:
    def __init__(self, sink_name, rate):
        gst_bundle = _get_gst()
        if not gst_bundle:
            raise RuntimeError("GStreamer PipeWire bindings unavailable")
        Gst, _ = gst_bundle
        self._Gst = Gst
        self._rate = rate

        pipeline = Gst.Pipeline.new(
            f"audiomux-calibrate-player-{sink_name.replace('.', '_').replace(':', '_')}"
        )
        src = Gst.ElementFactory.make("appsrc", None)
        queue = Gst.ElementFactory.make("queue", None)
        sink = Gst.ElementFactory.make("pipewiresink", None)
        if not all([pipeline, src, queue, sink]):
            raise RuntimeError(f"failed to create tone player for {sink_name}")

        src.set_property("caps", _audio_caps(rate))
        src.set_property("format", Gst.Format.TIME)
        src.set_property("is-live", True)
        src.set_property("block", True)
        src.set_property("do-timestamp", True)
        queue.set_property("flush-on-eos", True)
        sink.set_property("target-object", sink_name)
        sink.set_property("client-name", f"audiomux-calibrate-{sink_name}")
        sink.set_property("async", False)

        for elem in (src, queue, sink):
            pipeline.add(elem)
        if not src.link(queue):
            raise RuntimeError("failed to link appsrc -> queue")
        if not queue.link(sink):
            raise RuntimeError("failed to link queue -> pipewiresink")

        self._pipeline = pipeline
        self._src = src

    def start(self):
        ret = self._pipeline.set_state(self._Gst.State.PLAYING)
        if ret == self._Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("failed to start tone player")
        self._pipeline.get_state(self._Gst.SECOND)

    def play(self, raw):
        buf = self._Gst.Buffer.new_allocate(None, len(raw), None)
        buf.fill(0, raw)
        n_frames = len(raw) // S16_FRAME
        buf.duration = int(n_frames * self._Gst.SECOND / self._rate)
        result = self._src.emit("push-buffer", buf)
        if result != self._Gst.FlowReturn.OK:
            raise RuntimeError(f"push-buffer failed: {result.value_nick}")

    def stop(self):
        try:
            self._src.emit("end-of-stream")
        except Exception:
            pass
        try:
            self._pipeline.set_state(self._Gst.State.NULL)
        except Exception:
            pass


def calibrate_rounds(round_specs, mic_device, rate=RATE):
    """Run one continuous calibration session across multiple rounds.

    round_specs: [{"sink_order": [...], "freq_assignment": {...}}, ...]

    Returns one result dict per round in the same order as round_specs.
    """
    if not round_specs:
        return []

    unique_sinks = []
    seen_sinks = set()
    assignments = []
    for spec in round_specs:
        sink_order = list(spec["sink_order"])
        if len(sink_order) > len(CALIBRATE_FREQS):
            raise RuntimeError(f"too many sinks ({len(sink_order)}), "
                               f"max {len(CALIBRATE_FREQS)}")
        freq_assignment = spec.get("freq_assignment") or {}
        round_assignments = []
        for i, sink in enumerate(sink_order):
            if sink not in seen_sinks:
                unique_sinks.append(sink)
                seen_sinks.add(sink)
            freq = freq_assignment.get(sink, CALIBRATE_FREQS[i])
            round_assignments.append({
                "sink": sink,
                "freq": freq,
                "raw": generate_single_tone_raw(freq, rate=rate),
                "emit_ms": None,
            })
        assignments.append(round_assignments)

    recorder = _GstMicRecorder(mic_device, rate)
    players = {sink: _GstTonePlayer(sink, rate) for sink in unique_sinks}

    # Run the calibration in a worker thread with a hard timeout
    # so a hung GStreamer pipeline can't block forever.
    cal_error = []
    total_slots = sum(len(ra) for ra in assignments)
    max_duration = CALIBRATE_SETTLE + total_slots * (TONE_DURATION + TONE_GAP) + CALIBRATE_TAIL
    timeout_sec = max_duration + 10  # generous headroom

    def _run_calibration():
        try:
            recorder.start()
            for sink in unique_sinks:
                players[sink].start()

            record_start = time.perf_counter()
            time.sleep(CALIBRATE_SETTLE)

            for round_assignments in assignments:
                for info in round_assignments:
                    info["emit_ms"] = (time.perf_counter() - record_start) * 1000
                    players[info["sink"]].play(info["raw"])
                    time.sleep(TONE_GAP)

            time.sleep(CALIBRATE_TAIL)
        except Exception as e:
            cal_error.append(str(e))

    cal_thread = threading.Thread(target=_run_calibration, daemon=True)
    cal_thread.start()
    cal_thread.join(timeout=timeout_sec)

    # Clean up regardless — force-stop all pipelines
    for player in players.values():
        try:
            player.stop()
        except Exception:
            pass
    try:
        recorder.stop()
    except Exception:
        pass

    if cal_thread.is_alive():
        raise RuntimeError(f"calibration timed out after {timeout_sec:.0f}s")
    if cal_error:
        raise RuntimeError(cal_error[0])

    mic_buf = recorder.samples()
    all_results = []
    for round_assignments in assignments:
        results = {}
        for info in round_assignments:
            emit_ms = info["emit_ms"] if info["emit_ms"] is not None else 0.0
            edges = find_tone_edges(info["freq"], mic_buf, rate,
                                    search_from_ms=max(0, emit_ms - 50))
            onset_delay = edges["onset_ms"] - emit_ms
            results[info["sink"]] = {
                "onset_ms": edges["onset_ms"],
                "emit_ms": round(emit_ms, 1),
                "delay_ms": round(onset_delay, 1),
                "confidence": edges["confidence"],
                "freq": info["freq"],
            }
        all_results.append(results)
    return all_results


def calibrate_all(sink_names, mic_device, freq_assignment=None, rate=RATE):
    """Compatibility wrapper for a single calibration round."""
    rounds = calibrate_rounds([{
        "sink_order": list(sink_names),
        "freq_assignment": freq_assignment or {},
    }], mic_device, rate=rate)
    return rounds[0] if rounds else {}


# ── Phase 2: GCC-PHAT calibration helpers ────────────────────────────────────

def _require_numpy():
    try:
        import numpy as np
        return np
    except ImportError as e:
        raise RuntimeError(
            "numpy is required for GCC-PHAT calibration "
            "(install with: python3 -m pip install --user numpy)"
        ) from e


def gen_log_sweep(duration_s=1.0, f0=80.0, f1=8000.0,
                  rate=RATE, amplitude=0.6, fade_ms=10.0):
    """Log-chirp (Farina method).

    Returns a 1-D float32 numpy array in [-1, 1]. Endpoints are faded to
    avoid transients.
    """
    np = _require_numpy()
    n = int(rate * duration_s)
    t = np.arange(n, dtype=np.float64) / rate
    k = math.log(f1 / f0) / duration_s
    phase = 2.0 * math.pi * f0 * (np.exp(k * t) - 1.0) / k
    y = amplitude * np.sin(phase)
    fade = max(1, int(rate * fade_ms / 1000.0))
    if fade * 2 < n:
        y[:fade] *= np.linspace(0.0, 1.0, fade)
        y[-fade:] *= np.linspace(1.0, 0.0, fade)
    return y.astype(np.float32)


def float_mono_to_s16_stereo_bytes(mono_float):
    """Pack a mono float32 signal as stereo S16LE interleaved bytes."""
    np = _require_numpy()
    s = np.clip(mono_float * 32767.0, -32768, 32767).astype(np.int16)
    stereo = np.repeat(s[:, None], 2, axis=1).ravel()
    return stereo.tobytes()


def raw_s16le_stereo_to_mono(path):
    """Load a raw S16LE stereo file and return mono float32 in [-1, 1]."""
    np = _require_numpy()
    data = np.fromfile(path, dtype=np.int16)
    if data.size == 0:
        return np.zeros(0, dtype=np.float32)
    if data.size % 2:
        data = data[:-1]
    data = data.reshape(-1, 2).astype(np.float32)
    return (data[:, 0] + data[:, 1]) * (0.5 / 32768.0)


def gcc_phat(a, b, max_lag_samples=None, eps=1e-12):
    """Generalized Cross-Correlation with PHAse Transform.

    Returns (lag_samples, peak_ratio). Sign convention (empirical, see
    `probes/` synthetic test): positive `lag` means `b` arrived EARLIER
    than `a` by `lag` samples — so `a` is the lagged one.

    For mic-vs-monitor, the natural call is
        lag, conf = gcc_phat(mic, monitor)
    which returns positive `lag` = acoustic arrival delay at the mic
    (monitor emits, mic hears it `lag` samples later).

    peak_ratio is the correlation peak magnitude divided by the mean
    absolute value of the full centered correlation window; larger =
    more confident. With clean log-sweep signals peak_ratio easily
    exceeds 10⁴; with silence it degenerates toward 1.
    """
    np = _require_numpy()
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    if a.size == 0 or b.size == 0:
        return 0, 0.0
    n = a.size + b.size
    nfft = 1 << (n - 1).bit_length()
    A = np.fft.rfft(a, n=nfft)
    B = np.fft.rfft(b, n=nfft)
    R = A * np.conj(B)
    R /= np.abs(R) + eps
    cc = np.fft.irfft(R, n=nfft).astype(np.float32)
    half = nfft // 2
    cc_centered = np.concatenate([cc[-half:], cc[:half + 1]])
    center = half
    if max_lag_samples is not None:
        lo = max(0, center - max_lag_samples)
        hi = min(cc_centered.size, center + max_lag_samples + 1)
        window = cc_centered[lo:hi]
        peak_idx_local = int(np.argmax(np.abs(window)))
        lag = peak_idx_local + lo - center
        peak_val = float(window[peak_idx_local])
    else:
        peak_idx = int(np.argmax(np.abs(cc_centered)))
        lag = peak_idx - center
        peak_val = float(cc_centered[peak_idx])
    mean_abs = float(np.mean(np.abs(cc_centered))) + eps
    peak_ratio = float(abs(peak_val) / mean_abs)
    return lag, peak_ratio
