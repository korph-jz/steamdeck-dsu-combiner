#!/usr/bin/env bash
# Removes the DSU combiner. Does NOT touch SteamDeckGyroDSU or
# Steam-Controller-GyroDSU - those are separate projects with their own
# uninstallers.
set -euo pipefail

echo "Stopping and disabling the service ..."
systemctl --user disable --now dsu-combine.service 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user reset-failed 2>/dev/null || true

echo "Removing files ..."
rm -f "$HOME/.config/systemd/user/dsu-combine.service"
rm -rf "$HOME/dsu-combine"

echo "Done. Remember to update your emulators' cemuhook/DSU server settings"
echo "if you were pointing them at the combined port."
