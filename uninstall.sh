#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIBEXEC_DIR="/usr/local/libexec/audiomux"

PLASMOID_METADATA="$SCRIPT_DIR/plasmoid/metadata.json"
PLASMOID_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["KPlugin"]["Id"])' "$PLASMOID_METADATA" 2>/dev/null)" || {
    printf 'Unable to read KPlugin.Id from %s\n' "$PLASMOID_METADATA" >&2
    exit 1
}
if [[ -z "$PLASMOID_ID" ]]; then
    printf 'KPlugin.Id is empty in %s\n' "$PLASMOID_METADATA" >&2
    exit 1
fi

log() { printf '==> %s\n' "$*"; }
TARGET_HOME="$HOME"

# ── stop daemon and clean PipeWire state ─────────────────────────────────────

log "Stopping audiomux processes"
# SIGTERM first, then SIGKILL after 2s if still alive
pkill -f 'audiomux-syncd' 2>/dev/null || true
pkill -f 'pw-loopback.*audiomux' 2>/dev/null || true
sleep 2
pkill -9 -f 'audiomux-syncd' 2>/dev/null || true
pkill -9 -f 'pw-loopback.*audiomux' 2>/dev/null || true

log "Unloading PipeWire audiomux modules"
for mid in $(pactl list modules short 2>/dev/null \
    | grep -E 'audiomux_virtual|audiomux_combined' \
    | awk '{print $1}'); do
    pactl unload-module "$mid" 2>/dev/null || true
done

RUN_DIR="/run/user/$(id -u)"
rm -f -- "$RUN_DIR/audiomux.json" "$RUN_DIR/audiomux-syncd.json" \
    "$RUN_DIR/audiomux-syncd.sock" "$RUN_DIR/audiomux.lock" 2>/dev/null || true

# ── plasmoid ──────────────────────────────────────────────────────────────────

if kpackagetool6 --type Plasma/Applet --show "$PLASMOID_ID" &>/dev/null; then
    log "Removing plasmoid $PLASMOID_ID"
    kpackagetool6 --type Plasma/Applet --remove "$PLASMOID_ID"
else
    log "Plasmoid $PLASMOID_ID is not installed"
fi

# Also remove the physical directory in case kpackagetool6 left it behind
USER_PLASMOID_DIR="$TARGET_HOME/.local/share/plasma/plasmoids/$PLASMOID_ID"
if [[ -e "$USER_PLASMOID_DIR" ]]; then
    log "Removing leftover plasmoid directory $USER_PLASMOID_DIR"
    rm -rf -- "$USER_PLASMOID_DIR"
fi

# ── remove applet from panel config ──────────────────────────────────────────

PANEL_RC="$TARGET_HOME/.config/plasma-org.kde.plasma.desktop-appletsrc"
if [[ -f "$PANEL_RC" ]] && grep -q "plugin=$PLASMOID_ID" "$PANEL_RC"; then
    log "Removing applet entries from panel config"
    python3 - "$PANEL_RC" "$PLASMOID_ID" <<'PYSCRIPT'
import sys, re, pathlib

rc_path, applet_id = sys.argv[1], sys.argv[2]
text = pathlib.Path(rc_path).read_text()
plugin_line = f"plugin={applet_id}"
lines = text.splitlines(keepends=True)

# Pass 1: find applet section keys
stale_keys = set()
cur_section = None
for line in lines:
    m = re.match(r'^\[(.+)\]\s*$', line)
    if m:
        cur_section = m.group(1)
    elif cur_section and line.strip() == plugin_line:
        stale_keys.add(cur_section)

if not stale_keys:
    sys.exit(0)

# Extract numeric applet IDs being removed
stale_ids = set()
for k in stale_keys:
    m = re.search(r'\[Applets\]\[(\d+)$', k)
    if m:
        stale_ids.add(m.group(1))

# Expand to child sections (e.g. [key][Configuration])
prefixes = tuple(k + "]" for k in stale_keys)

# Pass 2: remove matching sections and clean AppletOrder
out, skip = [], False
for line in lines:
    m = re.match(r'^\[(.+)\]\s*$', line)
    if m:
        key = m.group(1)
        skip = key in stale_keys or key.startswith(prefixes)
    if not skip:
        if line.startswith('AppletOrder=') and stale_ids:
            ids = [i for i in line.strip().split('=', 1)[1].split(';')
                   if i and i not in stale_ids]
            line = 'AppletOrder=' + ';'.join(ids) + '\n'
        out.append(line)

cleaned = re.sub(r'\n{3,}', '\n\n', ''.join(out))
pathlib.Path(rc_path).write_text(cleaned)
PYSCRIPT
fi

# ── icon ──────────────────────────────────────────────────────────────────────

ICON_FILE="$TARGET_HOME/.local/share/icons/hicolor/scalable/actions/mixer-three-slider-symbolic.svg"
if [[ -f "$ICON_FILE" ]]; then
    log "Removing icon $ICON_FILE"
    rm -f -- "$ICON_FILE"
else
    log "Icon already absent"
fi

# ── systemd user unit ────────────────────────────────────────────────────────

SYSTEMD_USER_DIR="$TARGET_HOME/.config/systemd/user"
UNIT_FILE="$SYSTEMD_USER_DIR/audiomux-syncd.service"
if [[ -f "$UNIT_FILE" ]]; then
    log "Stopping and disabling audiomux-syncd"
    systemctl --user disable --now audiomux-syncd.service 2>/dev/null || true
    rm -f -- "$UNIT_FILE"
    systemctl --user daemon-reload
else
    log "Systemd unit already absent"
fi

# ── helper scripts (needs root for /usr/local) ──────────────────────────────

if [[ -d "$LIBEXEC_DIR" ]]; then
    log "Removing helper scripts from $LIBEXEC_DIR"
    if [[ -w "$LIBEXEC_DIR" ]]; then
        rm -rf -- "$LIBEXEC_DIR"
    else
        sudo rm -rf -- "$LIBEXEC_DIR"
    fi
else
    log "Helper directory $LIBEXEC_DIR already absent"
fi

# ── config ────────────────────────────────────────────────────────────────────

OFFSETS_FILE="$TARGET_HOME/.config/audiomux-offsets.json"
if [[ -f "$OFFSETS_FILE" ]]; then
    log "Removing offset config $OFFSETS_FILE"
    rm -f -- "$OFFSETS_FILE"
fi

# ── reload plasmashell ───────────────────────────────────────────────────────

if [[ "${SKIP_PLASMA_RELOAD:-0}" == "1" ]]; then
    log "Skipping plasmashell reload (SKIP_PLASMA_RELOAD=1)"
else
    log "Reloading plasmashell"
    kquitapp6 plasmashell >/dev/null 2>&1 || true
    for _ in {1..20}; do
        pgrep -x plasmashell >/dev/null 2>&1 || break
        sleep 0.5
    done
    rm -rf "$TARGET_HOME"/.cache/plasmashell* \
           "$TARGET_HOME"/.cache/plasma_theme_* \
           "$TARGET_HOME"/.cache/ksycoca6_* 2>/dev/null || true
    kbuildsycoca6 2>/dev/null || true
    systemctl --user start plasma-plasmashell.service >/dev/null 2>&1 \
        || kstart plasmashell
fi

log "AudioMux uninstalled."
