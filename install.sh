#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLASMOID_ID="org.kde.plasma.audiomux"
LIBEXEC_DIR="/usr/local/libexec/audiomux"

log() { printf '==> %s\n' "$*"; }

TARGET_USER="${SUDO_USER:-}"
if [[ -z "$TARGET_USER" && -n "${PKEXEC_UID:-}" ]]; then
    TARGET_USER="$(id -nu "$PKEXEC_UID")"
fi
if [[ -z "$TARGET_USER" ]]; then
    TARGET_USER="$(id -un)"
fi

TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
if [[ -z "$TARGET_HOME" ]]; then
    printf 'Unable to resolve home directory for user %s\n' "$TARGET_USER" >&2
    exit 1
fi

user_run() {
    if [[ "$(id -u)" -eq 0 && "$TARGET_USER" != "root" ]]; then
        runuser -u "$TARGET_USER" -- "$@"
    else
        "$@"
    fi
}

# ── helper scripts ────────────────────────────────────────────────────────────

log "Installing helper scripts to $LIBEXEC_DIR"
install -d -m 755 "$LIBEXEC_DIR"
install -m 755 "$SCRIPT_DIR/bin/audiomux-source.py"  "$LIBEXEC_DIR/audiomux-source.py"
install -m 755 "$SCRIPT_DIR/bin/audiomux-measure.py" "$LIBEXEC_DIR/audiomux-measure.py"

# ── icon ──────────────────────────────────────────────────────────────────────

log "Installing icon to hicolor theme"
ICON_DIR="$TARGET_HOME/.local/share/icons/hicolor/scalable/actions"
user_run mkdir -p "$ICON_DIR"
user_run install -m 644 "$SCRIPT_DIR/plasmoid/contents/icons/mixer-three-slider-symbolic.svg" "$ICON_DIR/"
user_run kbuildsycoca6 2>/dev/null || true

# ── plasmoid ──────────────────────────────────────────────────────────────────

log "Installing plasmoid $PLASMOID_ID"
if user_run kpackagetool6 --type Plasma/Applet --show "$PLASMOID_ID" &>/dev/null; then
    user_run kpackagetool6 --type Plasma/Applet --upgrade "$SCRIPT_DIR/plasmoid"
else
    user_run kpackagetool6 --type Plasma/Applet --install "$SCRIPT_DIR/plasmoid"
fi

# ── python dep ────────────────────────────────────────────────────────────────

if ! user_run python3 -c "import pulsectl" 2>/dev/null; then
    log "pulsectl is missing in $TARGET_USER's Python environment"
    log "Install it for that user, e.g. 'python3 -m pip install --user pulsectl'"
fi

log "Done — add the AudioMux widget to your panel if this is a fresh install."
log "Run 'update-all.sh' (or restart plasmashell manually) to reload the applet."
