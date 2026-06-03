#!/usr/bin/env bash
set -euo pipefail

APP_NAME="DAQ Monitor"
APP_ID="daq-monitor"
INSTALL_ROOT="/opt/daq_on_pi"
WRAPPER_PATH="/usr/local/bin/daq-monitor"
DESKTOP_FILE="/usr/share/applications/daq-monitor.desktop"
APP_ICON_PATH="${INSTALL_ROOT}/src/app_icon.png"
MCC_SPI_LOCKFILE="/tmp/.mcc_spi_lockfile"
TMPFILES_CONF="/etc/tmpfiles.d/daq-monitor.conf"
LOCKFILE_GROUP="root"
LOCKFILE_MODE="0666"

# Groups required on Raspberry Pi OS to access MCC HAT hardware (SPI, GPIO, I2C).
HW_GROUPS=(spi gpio i2c)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Parse arguments: optional --add-user <username> flags before the source dir.
ADD_USERS=()
POSITIONAL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --add-user)
      if [[ -z "${2:-}" ]]; then
        echo "Error: --add-user requires a username argument." >&2
        exit 1
      fi
      ADD_USERS+=("$2")
      shift 2
      ;;
    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done

SOURCE_DIR="${POSITIONAL[0]:-$REPO_ROOT}"

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

if getent group spi >/dev/null 2>&1; then
  LOCKFILE_GROUP="spi"
  LOCKFILE_MODE="0660"
fi

echo "Deploying from ${SOURCE_DIR}"
echo "Install root: ${INSTALL_ROOT}"

sudo mkdir -p "${INSTALL_ROOT}"
sudo rsync -a --delete \
  --exclude '.git/' \
  --exclude '__pycache__/' \
  "${SOURCE_DIR}/" "${INSTALL_ROOT}/"
sudo chown -R root:root "${INSTALL_ROOT}"
sudo chmod -R go-w "${INSTALL_ROOT}"

if [[ ! -f "${APP_ICON_PATH}" ]]; then
  FALLBACK_ICON_PATH="$("${SOURCE_DIR}/venv/bin/python" -c 'import os, matplotlib; print(os.path.join(matplotlib.get_data_path(), "images", "matplotlib.png"))' 2>/dev/null || true)"
  if [[ -n "${FALLBACK_ICON_PATH}" && -f "${FALLBACK_ICON_PATH}" ]]; then
    sudo install -m 644 "${FALLBACK_ICON_PATH}" "${APP_ICON_PATH}"
    echo "No src/app_icon.png found; using matplotlib fallback icon."
  fi
fi

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
Name=DAQ Monitor
Comment=Launch DAQ monitor GUI
Exec=/usr/local/bin/daq-monitor
Icon=/opt/daq_on_pi/src/app_icon.png
Path=/opt/daq_on_pi
Terminal=false
Categories=Utility;Science;Engineering;
StartupNotify=true
EOF
sudo chmod 644 "${DESKTOP_FILE}"

if [[ ! -f "${APP_ICON_PATH}" ]]; then
  echo "Warning: icon file not found at ${APP_ICON_PATH}."
  echo "The main menu may show a generic icon until src/app_icon.png exists."
fi

if command -v update-desktop-database >/dev/null 2>&1; then
  sudo update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
fi

ensure_shared_lockfile() {
  if command -v python3 >/dev/null 2>&1; then
    sudo python3 - "${MCC_SPI_LOCKFILE}" "${LOCKFILE_GROUP}" "${LOCKFILE_MODE}" <<'PY'
import os
import grp
import stat
import sys

path = sys.argv[1]
group_name = sys.argv[2]
file_mode = int(sys.argv[3], 8)
flags = os.O_WRONLY | os.O_CREAT
if hasattr(os, 'O_NOFOLLOW'):
    flags |= os.O_NOFOLLOW

try:
    st = os.lstat(path)
except FileNotFoundError:
    st = None

if st is not None:
    if stat.S_ISLNK(st.st_mode):
        raise SystemExit(f"Refusing to operate on symlink: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise SystemExit(f"Refusing to operate on non-regular file: {path}")

try:
    gid = grp.getgrnam(group_name).gr_gid
except KeyError as exc:
    raise SystemExit(f"Required group not found: {group_name}") from exc

fd = os.open(path, flags, 0o666)
os.close(fd)
os.chown(path, 0, gid)
os.chmod(path, file_mode)
PY
  else
    echo "Warning: python3 not found; skipping immediate lockfile creation." >&2
    echo "The tmpfiles rule at ${TMPFILES_CONF} will create ${MCC_SPI_LOCKFILE} on systems with systemd-tmpfiles." >&2
  fi
}

# Ensure MCC daqhats lock file works across all users.
# daqhats uses /tmp/.mcc_spi_lockfile; if this file is created by a regular
# user, other users can fail with "Board not responding." due to /tmp sticky
# directory semantics. Keep it root-owned and writable only by the hardware
# access group when available.
sudo tee "${TMPFILES_CONF}" >/dev/null <<EOF
# DAQ Monitor: keep MCC SPI lock file shared across users
f /tmp/.mcc_spi_lockfile ${LOCKFILE_MODE} root ${LOCKFILE_GROUP} -
EOF
if command -v systemd-tmpfiles >/dev/null 2>&1; then
  sudo systemd-tmpfiles --create "${TMPFILES_CONF}" >/dev/null 2>&1 || true
fi
ensure_shared_lockfile

# ── Hardware group membership ────────────────────────────────────────────────
# The MCC 128 HAT uses SPI/GPIO/I2C.  On Raspberry Pi OS those devices are
# owned by the spi/gpio/i2c groups.  Any user who is NOT a member of those
# groups will trigger a permission error at runtime, and the app will silently
# fall back to random-walk simulation instead of real sensor data.
#
# Use --add-user <username> (repeatable) to add users to the required groups.

for username in "${ADD_USERS[@]}"; do
  if ! id -u "${username}" >/dev/null 2>&1; then
    echo "Warning: user '${username}' does not exist — skipping group assignment." >&2
    continue
  fi
  for grp in "${HW_GROUPS[@]}"; do
    if getent group "${grp}" >/dev/null 2>&1; then
      if ! id -nG "${username}" | tr ' ' '\n' | grep -qx "${grp}"; then
        sudo usermod -aG "${grp}" "${username}"
        echo "Added '${username}' to group '${grp}'."
      else
        echo "User '${username}' is already in group '${grp}'."
      fi
    fi
  done
  echo "Note: group changes for '${username}' take effect on next login."
done

# Always remind the admin about the hardware group requirement.
echo ""
echo "Hardware access reminder:"
echo "  Any user who runs ${APP_NAME} must be a member of the spi, gpio, and i2c groups."
echo "  The MCC SPI lock file is managed at ${MCC_SPI_LOCKFILE} with root:${LOCKFILE_GROUP} ${LOCKFILE_MODE} permissions."
echo "  Without group membership the app falls back to simulated data silently."
echo "  To grant access to a user:  sudo usermod -aG spi,gpio,i2c <username>"
echo "  (The user must log out and back in for group changes to take effect.)"

echo ""
echo "Deployment complete."
echo "Users can launch '${APP_NAME}' from the app menu or run: ${WRAPPER_PATH}"
