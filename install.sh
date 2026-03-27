#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLASMOID_ID="org.kde.plasma.audiomux"

log() { printf '==> %s\n' "$*"; }

# ── bin scripts ───────────────────────────────────────────────────────────────

log "Installing bin scripts to ~/bin"
mkdir -p "$HOME/bin"
install -m 755 "$SCRIPT_DIR/bin/audiomux-source.py"  "$HOME/bin/audiomux-source.py"
install -m 755 "$SCRIPT_DIR/bin/audiomux-measure.py" "$HOME/bin/audiomux-measure.py"

# ── icon ──────────────────────────────────────────────────────────────────────

log "Installing icon to hicolor theme"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/actions"
mkdir -p "$ICON_DIR"
install -m 644 "$SCRIPT_DIR/plasmoid/contents/icons/mixer-three-slider-symbolic.svg" "$ICON_DIR/"
kbuildsycoca6 2>/dev/null || true

# ── plasmoid ──────────────────────────────────────────────────────────────────

log "Installing plasmoid $PLASMOID_ID"
if kpackagetool6 --type Plasma/Applet --show "$PLASMOID_ID" &>/dev/null; then
    kpackagetool6 --type Plasma/Applet --upgrade "$SCRIPT_DIR/plasmoid"
else
    kpackagetool6 --type Plasma/Applet --install "$SCRIPT_DIR/plasmoid"
fi

# ── python dep ────────────────────────────────────────────────────────────────

if ! python3 -c "import pulsectl" 2>/dev/null; then
    log "Installing pulsectl (pip --user)"
    pip install --user pulsectl
fi

log "Done — add the AudioMux widget to your panel if this is a fresh install."
log "Run 'update-all.sh' (or restart plasmashell manually) to reload the applet."
