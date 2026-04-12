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

# ── plasmoid ──────────────────────────────────────────────────────────────────

if user_run kpackagetool6 --type Plasma/Applet --show "$PLASMOID_ID" &>/dev/null; then
    log "Removing plasmoid $PLASMOID_ID"
    user_run kpackagetool6 --type Plasma/Applet --remove "$PLASMOID_ID"
else
    log "Plasmoid $PLASMOID_ID is not installed"
fi

# ── icon ──────────────────────────────────────────────────────────────────────

ICON_FILE="$TARGET_HOME/.local/share/icons/hicolor/scalable/actions/mixer-three-slider-symbolic.svg"
if [[ -f "$ICON_FILE" ]]; then
    log "Removing icon $ICON_FILE"
    user_run rm -f -- "$ICON_FILE"
    user_run kbuildsycoca6 2>/dev/null || true
else
    log "Icon already absent"
fi

# ── helper scripts ────────────────────────────────────────────────────────────

if [[ -d "$LIBEXEC_DIR" ]]; then
    log "Removing helper scripts from $LIBEXEC_DIR"
    rm -rf -- "$LIBEXEC_DIR"
else
    log "Helper directory $LIBEXEC_DIR already absent"
fi

# ── config ────────────────────────────────────────────────────────────────────

OFFSETS_FILE="$TARGET_HOME/.config/audiomux-offsets.json"
if [[ -f "$OFFSETS_FILE" ]]; then
    log "Removing offset config $OFFSETS_FILE"
    user_run rm -f -- "$OFFSETS_FILE"
fi

log "AudioMux uninstalled."

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
