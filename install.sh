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

# ── privileged install (needs root for /usr/local) ───────────────────────────

if [[ "$(id -u)" -ne 0 ]]; then
    log "Installing helper scripts to $LIBEXEC_DIR (needs root)"
    if command -v sudo &>/dev/null; then
        sudo install -d -m 755 "$LIBEXEC_DIR"
        sudo install -m 755 "$SCRIPT_DIR/bin/audiomux-source.py"   "$LIBEXEC_DIR/audiomux-source.py"
        sudo install -m 755 "$SCRIPT_DIR/bin/audiomux-measure.py"  "$LIBEXEC_DIR/audiomux-measure.py"
        sudo install -m 755 "$SCRIPT_DIR/bin/audiomux-syncd.py"    "$LIBEXEC_DIR/audiomux-syncd.py"
        sudo install -m 644 "$SCRIPT_DIR/bin/audiomux_sync_lib.py" "$LIBEXEC_DIR/audiomux_sync_lib.py"
        sudo install -m 755 "$SCRIPT_DIR/bin/audiomux_calibrate.py" "$LIBEXEC_DIR/audiomux_calibrate.py"
    else
        printf 'sudo is required to install to %s\n' "$LIBEXEC_DIR" >&2
        exit 1
    fi
else
    log "Installing helper scripts to $LIBEXEC_DIR"
    install -d -m 755 "$LIBEXEC_DIR"
    install -m 755 "$SCRIPT_DIR/bin/audiomux-source.py"   "$LIBEXEC_DIR/audiomux-source.py"
    install -m 755 "$SCRIPT_DIR/bin/audiomux-measure.py"  "$LIBEXEC_DIR/audiomux-measure.py"
    install -m 755 "$SCRIPT_DIR/bin/audiomux-syncd.py"    "$LIBEXEC_DIR/audiomux-syncd.py"
    install -m 644 "$SCRIPT_DIR/bin/audiomux_sync_lib.py" "$LIBEXEC_DIR/audiomux_sync_lib.py"
    install -m 755 "$SCRIPT_DIR/bin/audiomux_calibrate.py" "$LIBEXEC_DIR/audiomux_calibrate.py"
fi

# ── everything below runs as the calling user (session-aware) ────────────────

TARGET_HOME="$HOME"

# ── systemd user unit ────────────────────────────────────────────────────────

SYSTEMD_USER_DIR="$TARGET_HOME/.config/systemd/user"
log "Installing systemd user unit to $SYSTEMD_USER_DIR"
mkdir -p "$SYSTEMD_USER_DIR"
install -m 644 "$SCRIPT_DIR/systemd/audiomux-syncd.service" "$SYSTEMD_USER_DIR/audiomux-syncd.service"
systemctl --user daemon-reload
# Stop old daemon forcefully in case it's stuck in calibration
systemctl --user stop audiomux-syncd.service 2>/dev/null || true
pkill -9 -f 'audiomux-syncd' 2>/dev/null || true
systemctl --user enable --now audiomux-syncd.service || true

# ── icon ──────────────────────────────────────────────────────────────────────

log "Installing icon to hicolor theme"
ICON_DIR="$TARGET_HOME/.local/share/icons/hicolor/scalable/actions"
mkdir -p "$ICON_DIR"
install -m 644 "$SCRIPT_DIR/plasmoid/contents/icons/mixer-three-slider-symbolic.svg" "$ICON_DIR/"

# ── plasmoid ──────────────────────────────────────────────────────────────────

log "Installing plasmoid $PLASMOID_ID"

USER_PLASMOID_DIR="$TARGET_HOME/.local/share/plasma/plasmoids/$PLASMOID_ID"
PLASMOID_CANONICAL_DIR="$(realpath "$SCRIPT_DIR/plasmoid")"
if [[ -L "$USER_PLASMOID_DIR" ]]; then
    if ! INSTALLED_TARGET="$(realpath "$USER_PLASMOID_DIR" 2>/dev/null)"; then
        log "Removing broken symlink $USER_PLASMOID_DIR -> $(readlink "$USER_PLASMOID_DIR")"
        rm -f -- "$USER_PLASMOID_DIR"
    elif [[ "$INSTALLED_TARGET" == "$PLASMOID_CANONICAL_DIR" ]]; then
        log "Removing dev symlink $USER_PLASMOID_DIR -> $(readlink "$USER_PLASMOID_DIR")"
        rm -f -- "$USER_PLASMOID_DIR"
    else
        printf 'Refusing to remove unrelated symlink %s -> %s\n' \
            "$USER_PLASMOID_DIR" "$(readlink "$USER_PLASMOID_DIR")" >&2
        printf 'Resolved target (%s) does not match this checkout (%s).\n' \
            "$INSTALLED_TARGET" "$PLASMOID_CANONICAL_DIR" >&2
        exit 1
    fi
fi

if [[ -d "$USER_PLASMOID_DIR" ]]; then
    kpackagetool6 --type Plasma/Applet --upgrade "$PLASMOID_CANONICAL_DIR"
else
    kpackagetool6 --type Plasma/Applet --install "$PLASMOID_CANONICAL_DIR"
fi

# ── add applet to panel ──────────────────────────────────────────────────────

PANEL_RC="$TARGET_HOME/.config/plasma-org.kde.plasma.desktop-appletsrc"
if [[ -f "$PANEL_RC" ]] && ! grep -q "plugin=$PLASMOID_ID" "$PANEL_RC"; then
    log "Adding audiomux applet to panel"
    python3 - "$PANEL_RC" "$PLASMOID_ID" <<'PYSCRIPT'
import sys, re, pathlib

rc_path, applet_id = sys.argv[1], sys.argv[2]
text = pathlib.Path(rc_path).read_text()
lines = text.splitlines(keepends=True)

# Find the panel containment (plugin=org.kde.panel) and its key
panel_key = None
cur_section = None
for line in lines:
    m = re.match(r'^\[(.+)\]\s*$', line)
    if m:
        cur_section = m.group(1)
    elif cur_section and line.strip() == 'plugin=org.kde.panel':
        panel_key = cur_section
        break

if not panel_key:
    print("  No panel containment found, skipping")
    sys.exit(0)

# Find the highest applet ID in the entire file to pick the next one
all_ids = [int(x) for x in re.findall(r'\[Applets\]\[(\d+)\]', text)]
next_id = max(all_ids) + 1 if all_ids else 100

# Build the new applet section
new_section = (
    f"\n[{panel_key}][Applets][{next_id}]\n"
    f"immutability=1\n"
    f"plugin={applet_id}\n"
    f"\n"
    f"[{panel_key}][Applets][{next_id}][Configuration]\n"
    f"PreloadWeight=100\n"
)

# Find [panel_key][General] and insert before it, then update AppletOrder
general_header = f'[{panel_key}][General]'
out = []
inserted = False
for line in lines:
    if not inserted and line.strip() == general_header:
        out.append(new_section)
        inserted = True
    out.append(line)
    # Insert our ID into AppletOrder — before the margins-separator so we
    # sit next to the other custom applets (dell-fans, wifimimo).
    if inserted and line.startswith('AppletOrder='):
        order = line.strip().split('=', 1)[1].split(';')
        # Build a map of applet-id → plugin for this panel
        id_to_plugin = {}
        cur_id = None
        for tl in lines:
            m2 = re.match(rf'^\[{re.escape(panel_key)}\]\[Applets\]\[(\d+)\]\s*$', tl)
            if m2:
                cur_id = m2.group(1)
            elif cur_id and tl.startswith('plugin='):
                id_to_plugin[cur_id] = tl.strip().split('=', 1)[1]
                cur_id = None
        # Find the separator's position in the order
        sep_pos = None
        for i, aid in enumerate(order):
            if id_to_plugin.get(aid) == 'org.kde.plasma.marginsseparator':
                sep_pos = i
                break
        if sep_pos is not None:
            order.insert(sep_pos, str(next_id))
        else:
            order.append(str(next_id))
        out[-1] = 'AppletOrder=' + ';'.join(order) + '\n'

if not inserted:
    out.append(new_section)

pathlib.Path(rc_path).write_text(''.join(out))
print(f"  Added as applet {next_id}")
PYSCRIPT
else
    log "Audiomux applet already in panel config"
fi

# ── python dep ────────────────────────────────────────────────────────────────

if ! python3 -c "import pulsectl" 2>/dev/null; then
    log "pulsectl is missing — install with: python3 -m pip install --user pulsectl"
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
           "$TARGET_HOME"/.cache/plasma_theme_* 2>/dev/null || true
    kbuildsycoca6 2>/dev/null || true
    systemctl --user start plasma-plasmashell.service >/dev/null 2>&1 \
        || kstart plasmashell
fi

log "Done."
