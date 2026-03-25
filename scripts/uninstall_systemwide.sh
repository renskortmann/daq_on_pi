#!/usr/bin/env bash
set -euo pipefail

APP_NAME="DAQ GUI"
INSTALL_ROOT="/opt/daq_on_pi"
WRAPPER_PATH="/usr/local/bin/daq-gui"
DESKTOP_FILE="/usr/share/applications/daq-gui.desktop"

if ! command -v sudo >/dev/null 2>&1; then
  echo "Error: sudo is required for system-wide uninstall." >&2
  exit 1
fi

echo "Uninstalling ${APP_NAME}"
echo "Removing: ${DESKTOP_FILE}"
echo "Removing: ${WRAPPER_PATH}"
echo "Removing: ${INSTALL_ROOT}"

sudo rm -f "${DESKTOP_FILE}"
sudo rm -f "${WRAPPER_PATH}"
sudo rm -rf "${INSTALL_ROOT}"

if command -v update-desktop-database >/dev/null 2>&1; then
  sudo update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
fi

echo "Uninstall complete."
