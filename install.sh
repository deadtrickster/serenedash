#!/usr/bin/env bash
# install.sh — symlink serenedash onto PATH and add a desktop entry.
#
#   ./install.sh            install for the current user
#   ./install.sh --uninstall
#
# Symlink rather than copy, so `git pull` updates the installed command. Nothing is written outside
# ~/.local, and an existing config is never touched.
set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"

if [ "${1:-}" = "--uninstall" ]; then
	rm -f "$BIN/serenedash" "$BIN/serenedash-perf-snap" \
		"$APPS/serenedash.desktop" "$ICONS/serenedash.svg"
	command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" 2>/dev/null
	echo "uninstalled"
	exit 0
fi

mkdir -p "$BIN" "$APPS" "$ICONS"
ln -sf "$HERE/serenedash.py" "$BIN/serenedash"
ln -sf "$HERE/perf-snap.sh" "$BIN/serenedash-perf-snap"
cp "$HERE/icon.svg" "$ICONS/serenedash.svg"

# Pick a terminal that exists. A .desktop entry pointing at a missing terminal fails silently from
# the launcher, which is the least debuggable way for an install to be broken.
TERM_CMD=""
for t in konsole gnome-terminal xfce4-terminal alacritty kitty wezterm xterm; do
	command -v "$t" >/dev/null 2>&1 && {
		TERM_CMD="$t"
		break
	}
done
[ -n "$TERM_CMD" ] || {
	echo "no terminal emulator found; skipping the desktop entry" >&2
	echo "installed: $BIN/serenedash"
	exit 0
}

sed -e "s#@TERMINAL@#$TERM_CMD#" -e "s#@BIN@#$BIN/serenedash#" -e "s#@ICON@#$ICONS/serenedash.svg#" \
	"$HERE/serenedash.desktop.in" >"$APPS/serenedash.desktop"
chmod +x "$APPS/serenedash.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" 2>/dev/null

echo "installed:"
echo "  $BIN/serenedash              (symlink — git pull updates it)"
echo "  $BIN/serenedash-perf-snap    (run under sudo to record)"
echo "  $APPS/serenedash.desktop     (via $TERM_CMD)"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "  note: $BIN is not on PATH" ;; esac
