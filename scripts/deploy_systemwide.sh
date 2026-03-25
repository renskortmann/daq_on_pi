#!/usr/bin/env bash
set -euo pipefail

APP_NAME="DAQ GUI"
APP_ID="daq-gui"
INSTALL_ROOT="/opt/daq_on_pi"
WRAPPER_PATH="/usr/local/bin/daq-gui"
DESKTOP_FILE="/usr/share/applications/daq-gui.desktop"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SOURCE_DIR="${1:-$REPO_ROOT}"

if [[ "${SOURCE_DIR}" != /* ]]; then
  SOURCE_DIR="$(cd "${SOURCE_DIR}" && pwd)"
fi

if [[ ! -d "${SOURCE_DIR}" ]]; then
  echo "Error: source directory does not exist: ${SOURCE_DIR}" >&2
  exit 1
fi

if [[ ! -f "${SOURCE_DIR}/src/gui.py" ]]; then
  echo "Error: expected file not found: ${SOURCE_DIR}/src/gui.py" >&2
  exit 1
fi

if [[ ! -x "${SOURCE_DIR}/venv/bin/python" ]]; then
  echo "Error: expected Python interpreter not found: ${SOURCE_DIR}/venv/bin/python" >&2
  echo "Create a virtual environment at ${SOURCE_DIR}/venv first." >&2
  exit 1
fi

if ! command -v rsync >/dev/null 2>&1; then
  echo "Error: rsync is required but not installed." >&2
  exit 1
fi

if ! command -v sudo >/dev/null 2>&1; then
  echo "Error: sudo is required for system-wide deployment." >&2
  exit 1
fi

echo "Deploying from ${SOURCE_DIR}"
echo "Install root: ${INSTALL_ROOT}"

sudo mkdir -p "${INSTALL_ROOT}"
sudo rsync -a --delete \
  --exclude '.git/' \
  --exclude '__pycache__/' \
  "${SOURCE_DIR}/" "${INSTALL_ROOT}/"

sudo install -d -m 755 /usr/local/bin
sudo tee "${WRAPPER_PATH}" >/dev/null <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
cd /opt/daq_on_pi
exec /opt/daq_on_pi/venv/bin/python /opt/daq_on_pi/src/gui.py "$@"
EOF
sudo chmod 755 "${WRAPPER_PATH}"

sudo install -d -m 755 /usr/share/applications
sudo tee "${DESKTOP_FILE}" >/dev/null <<'EOF'
[Desktop Entry]
Version=1.0
Type=Application
Name=DAQ GUI
Comment=Launch DAQ monitor GUI
Exec=/usr/local/bin/daq-gui
Path=/opt/daq_on_pi
Terminal=false
Categories=Utility;Science;Engineering;
StartupNotify=true
EOF
sudo chmod 644 "${DESKTOP_FILE}"

if command -v update-desktop-database >/dev/null 2>&1; then
  sudo update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
fi

echo "Deployment complete."
echo "Users can launch '${APP_NAME}' from the app menu or run: ${WRAPPER_PATH}"
