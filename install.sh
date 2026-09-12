#!/usr/bin/env bash
# Installs the DSU combiner as a systemd --user service.
# Safe to re-run - it just overwrites its own files and restarts the service.
set -euo pipefail

INSTALL_DIR="$HOME/dsu-combine"
SERVICE_DIR="$HOME/.config/systemd/user"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing to $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/dsu_combine.py" "$INSTALL_DIR/dsu_combine.py"

echo "Installing systemd user service ..."
mkdir -p "$SERVICE_DIR"
cp "$SCRIPT_DIR/dsu-combine.service" "$SERVICE_DIR/dsu-combine.service"

echo "Checking for the two required backend services ..."
missing=0
for unit in sdgyrodsu.service SteamControllerGyroDSU.service; do
  if ! systemctl --user list-unit-files "$unit" >/dev/null 2>&1 || \
     ! systemctl --user is-enabled "$unit" >/dev/null 2>&1; then
    echo "  WARNING: $unit not found or not enabled."
    echo "  Install it first from its own project before this will do anything useful:"
    case "$unit" in
      sdgyrodsu.service) echo "    https://github.com/kmicki/SteamDeckGyroDSU" ;;
      SteamControllerGyroDSU.service) echo "    https://github.com/dylangmarinus-stack/Steam-Controller-GyroDSU" ;;
    esac
    missing=1
  fi
done
if [ "$missing" -eq 0 ]; then
  echo "  Both backend services found."
fi

echo "Enabling and starting the combiner ..."
systemctl --user daemon-reload
systemctl --user enable --now dsu-combine.service

echo ""
echo "Done. Point your emulator's cemuhook/DSU server setting at:"
echo "  127.0.0.1:26759"
echo ""
echo "Check status any time with:"
echo "  systemctl --user status dsu-combine.service"
echo "  journalctl --user -u dsu-combine.service -f"
