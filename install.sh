#!/bin/sh
# Install the watchers and the xivport command on this Mac. Idempotent; re-run after pulling.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
EXPECTED="$HOME/Library/Application Support/XIV on Mac/wedge-watch"
if [ "$HERE" != "$EXPECTED" ]; then
    echo "clone this repo at: $EXPECTED" >&2
    exit 1
fi
git -C "$HERE" config core.hooksPath .githooks
mkdir -p "$HOME/.local/bin"
ln -sf "$HERE/bin/xivport" "$HOME/.local/bin/xivport"
for plist in "$HERE"/launchd/*.plist; do
    name="$(basename "$plist")"
    target="$HOME/Library/LaunchAgents/$name"
    sed "s|__HOME__|$HOME|g" "$plist" > "$target"
    launchctl bootout "gui/$(id -u)/${name%.plist}" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$target"
    echo "loaded ${name%.plist}"
done
echo "done; run 'xivport' to check"
