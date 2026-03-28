# AudioMux

KDE Plasma 6 system tray applet for routing audio to multiple outputs and inputs simultaneously via PipeWire, with per-device volume sliders and latency offset controls.

## What it does

- **Multi-output routing** — check multiple output devices; audio plays through all of them at once without interruption when adding/removing outputs
- **Multi-input routing** — check multiple input devices; they are combined into a single source
- **Volume sliders** — per-device volume control (0–150%)
- **Latency offsets** — ±10ms buttons per non-primary device to align hardware paths that have different inherent delays (e.g. HDMI digital vs. analog amplifier)
- **Persistent offsets** — per-device latency adjustments remembered across sessions in `~/.config/audiomux-offsets.json`

## How it works

**Outputs:** A permanent virtual null sink (`audiomux_virtual`) is set as the default. One `module-loopback` instance per active real output reads from `audiomux_virtual.monitor`. Adding an output creates a new loopback (no audio interruption); removing one unloads just that loopback.

**Inputs:** `module-combine-source` merges multiple inputs into a single default source.

**Latency:** Each loopback uses `latency_msec = 200 + offset`. The primary/reference device (Ryzen HD analog) has no offset controls; all others adjust relative to it.

## Files

```
bin/
  audiomux-source.py   # Backend — called by the plasmoid on every poll/action
  audiomux-measure.py  # Utility — records both outputs and cross-correlates to measure lag
plasmoid/
  metadata.json
  contents/ui/main.qml
  contents/icons/mixer-three-slider-symbolic.svg
```

## Requirements

- KDE Plasma 6 / PipeWire
- Python 3 + `pulsectl` (`pip install --user pulsectl`)
- `pactl` (part of `pipewire-pulse`)

## Installation

```bash
./install.sh
```

Then right-click the panel → Add Widgets → search "AudioMux" and add it.

On subsequent updates, re-run `./install.sh` (or `../update-all.sh`) and restart plasmashell.

## Machine-specific configuration

`bin/audiomux-source.py` has two constants near the top that identify your primary (reference) devices:

```python
PRIMARY_SINK   = "alsa_output.pci-0000_03_00.6.analog-stereo"
PRIMARY_SOURCE = "alsa_input.pci-0000_03_00.6.analog-stereo"
```

Find your device names with `pactl list sinks short` and `pactl list sources short`.

The plasmoid backend is installed to `/usr/local/libexec/audiomux/audiomux-source.py`, and the QML points at that system path:

```qml
readonly property string sourceCmd: "/usr/local/libexec/audiomux/audiomux-source.py"
```

If you want a different prefix, update both `install.sh` and `plasmoid/contents/ui/main.qml`.

## Measuring output lag

Play audio through `audiomux_virtual` (anything with transients works), then run:

```bash
python3 bin/audiomux-measure.py
```

It records 2 seconds from both output monitors simultaneously, downsamples, and cross-correlates to find the lag in ms. Adjust the offset buttons in the applet until the two outputs align.
