#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIBEXEC_DIR="/usr/local/libexec/audiomux"

# Extract plasmoid id from metadata.json so this script can't drift from the
# packaged plasmoid. python3 is already a hard dep of audiomux (the helpers
# in bin/ are Python), so no extra tooling is required.
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

# If a dev symlink at ~/.local/share/plasma/plasmoids/<id> points back at *this*
# checkout, remove the symlink itself before invoking kpackagetool6. Otherwise
# `kpackagetool6 --upgrade` follows the symlink and rm -rf's the source repo.
# Unrelated symlinked installs (e.g. another working tree) are left alone and
# the script bails out so the user can resolve them manually.
USER_PLASMOID_DIR="$TARGET_HOME/.local/share/plasma/plasmoids/$PLASMOID_ID"
PLASMOID_CANONICAL_DIR="$(realpath "$SCRIPT_DIR/plasmoid")"
if [[ -L "$USER_PLASMOID_DIR" ]]; then
    if ! INSTALLED_TARGET="$(realpath "$USER_PLASMOID_DIR" 2>/dev/null)"; then
        log "Removing broken symlink $USER_PLASMOID_DIR -> $(readlink "$USER_PLASMOID_DIR")"
        user_run rm -f -- "$USER_PLASMOID_DIR"
    elif [[ "$INSTALLED_TARGET" == "$PLASMOID_CANONICAL_DIR" ]]; then
        log "Removing dev symlink $USER_PLASMOID_DIR -> $(readlink "$USER_PLASMOID_DIR")"
        user_run rm -f -- "$USER_PLASMOID_DIR"
    else
        printf 'Refusing to remove unrelated symlink %s -> %s\n' \
            "$USER_PLASMOID_DIR" "$(readlink "$USER_PLASMOID_DIR")" >&2
        printf 'Resolved target (%s) does not match this checkout (%s).\n' \
            "$INSTALLED_TARGET" "$PLASMOID_CANONICAL_DIR" >&2
        exit 1
    fi
fi

if [[ -d "$USER_PLASMOID_DIR" ]]; then
    user_run kpackagetool6 --type Plasma/Applet --upgrade "$PLASMOID_CANONICAL_DIR"
else
    user_run kpackagetool6 --type Plasma/Applet --install "$PLASMOID_CANONICAL_DIR"
fi

# ── python dep ────────────────────────────────────────────────────────────────

if ! user_run python3 -c "import pulsectl" 2>/dev/null; then
    log "pulsectl is missing in $TARGET_USER's Python environment"
    log "Install it for that user, e.g. 'python3 -m pip install --user pulsectl'"
fi

log "Done — add the AudioMux widget to your panel if this is a fresh install."

# ── reload plasmashell ───────────────────────────────────────────────────────

if [[ "${SKIP_PLASMA_RELOAD:-0}" == "1" ]]; then
    log "Skipping plasmashell reload (SKIP_PLASMA_RELOAD=1)"
else
    log "Reloading plasmashell"
    user_run kquitapp6 plasmashell >/dev/null 2>&1 || true
    sleep 2
    user_run systemctl --user start plasma-plasmashell.service >/dev/null 2>&1 \
        || user_run kstart plasmashell
fi
