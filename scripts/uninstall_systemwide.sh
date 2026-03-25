#!/usr/bin/env bash
set -euo pipefail

APP_NAME="DAQ Monitor"
APP_ID="daq-monitor"
INSTALL_ROOT="/opt/daq_on_pi"
WRAPPER_PATH="/usr/local/bin/daq-monitor"
DESKTOP_FILE="/usr/share/applications/daq-monitor.desktop"
MCC_SPI_LOCKFILE="/tmp/.mcc_spi_lockfile"
TMPFILES_CONF="/etc/tmpfiles.d/daq-monitor.conf"

remove_wf_panel_shortcut_for_user() {
  local username="$1"
  local home_dir="$2"
  local uid config_file

  if ! command -v python3 >/dev/null 2>&1; then
    return
  fi

  uid="$(id -u "${username}" 2>/dev/null || true)"
  if [[ -z "${uid}" ]]; then
    return
  fi

  config_file="${home_dir}/.config/wf-panel-pi/wf-panel-pi.ini"
  if [[ ! -f "${config_file}" ]]; then
    return
  fi

  if sudo -u "${username}" python3 - "${config_file}" "${APP_ID}" "${DESKTOP_FILE}" <<'PY'
import pathlib
import shlex
import sys

path = pathlib.Path(sys.argv[1])
app_id = sys.argv[2]
desktop_path = sys.argv[3]

lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
changed = False

def should_remove(token: str) -> bool:
    t = token.strip().strip('"').strip("'")
    if t == app_id:
        return True
    if t == f"{app_id}.desktop":
        return True
    if t == desktop_path:
        return True
    if t.endswith(f"/{app_id}.desktop"):
        return True
    return False

for i, line in enumerate(lines):
    if line.startswith("launchers="):
        current = line[len("launchers="):].strip()
        tokens = shlex.split(current)
        filtered = [t for t in tokens if not should_remove(t)]
        if filtered != tokens:
            lines[i] = "launchers=" + " ".join(filtered) + "\n"
            changed = True

if changed:
    path.write_text("".join(lines), encoding="utf-8")
    print("changed")
PY
  then
    echo "Removed wf-panel launcher shortcut for user: ${username}"
  fi

  if pgrep -u "${username}" -x wf-panel-pi >/dev/null 2>&1; then
    sudo -u "${username}" XDG_RUNTIME_DIR="/run/user/${uid}" pkill -x wf-panel-pi >/dev/null 2>&1 || true
  fi
}

remove_wf_panel_shortcut() {
  local username _ uid _ _ home shell

  while IFS=: read -r username _ uid _ _ home shell; do
    if [[ "${uid}" -lt 1000 || ! -d "${home}" ]]; then
      continue
    fi

    if [[ "${shell}" == "/usr/sbin/nologin" || "${shell}" == "/bin/false" ]]; then
      continue
    fi

    remove_wf_panel_shortcut_for_user "${username}" "${home}"
  done < <(getent passwd)
}

remove_lxpanel_shortcut_for_user() {
  local username="$1"
  local home_dir="$2"
  local uid panel_file

  if ! command -v python3 >/dev/null 2>&1; then
    return
  fi

  uid="$(id -u "${username}" 2>/dev/null || true)"
  if [[ -z "${uid}" ]]; then
    return
  fi

  if [[ ! -d "${home_dir}/.config/lxpanel" ]]; then
    return
  fi

  while IFS= read -r panel_file; do
    if [[ -z "${panel_file}" ]]; then
      continue
    fi

    if sudo -u "${username}" python3 - "${panel_file}" "${APP_ID}.desktop" "${DESKTOP_FILE}" <<'PY'
import pathlib
import sys

panel_path = pathlib.Path(sys.argv[1])
target_desktop = sys.argv[2]
target_path = sys.argv[3]

try:
    lines = panel_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
except Exception:
    raise SystemExit(1)

out = []
changed = False
i = 0
n = len(lines)

while i < n:
    line = lines[i]
    if line.lstrip().startswith("Button") and "{" in line:
        block = []
        depth = 0
        j = i

        while j < n:
            current = lines[j]
            block.append(current)
            depth += current.count("{")
            depth -= current.count("}")
            j += 1
            if depth <= 0:
                break

        block_text = "".join(block)
        if target_desktop in block_text or target_path in block_text:
            changed = True
        else:
            out.extend(block)

        i = j
        continue

    out.append(line)
    i += 1

if changed:
    panel_path.write_text("".join(out), encoding="utf-8")
    print("changed")
PY
    then
      echo "Removed LXPanel launcher shortcut for user: ${username} (${panel_file})"
    fi
  done < <(find "${home_dir}/.config/lxpanel" -type f -path '*/panels/*' 2>/dev/null)

  if pgrep -u "${username}" -x lxpanel >/dev/null 2>&1; then
    sudo -u "${username}" XDG_RUNTIME_DIR="/run/user/${uid}" lxpanelctl restart >/dev/null 2>&1 || true
  fi
}

remove_lxpanel_shortcut() {
  local username _ uid _ _ home shell

  while IFS=: read -r username _ uid _ _ home shell; do
    if [[ "${uid}" -lt 1000 || ! -d "${home}" ]]; then
      continue
    fi

    if [[ "${shell}" == "/usr/sbin/nologin" || "${shell}" == "/bin/false" ]]; then
      continue
    fi

    remove_lxpanel_shortcut_for_user "${username}" "${home}"
  done < <(getent passwd)
}

remove_gnome_taskbar_shortcut_for_user() {
  local username="$1"
  local uid runtime_bus favorites updated

  if ! command -v gsettings >/dev/null 2>&1; then
    return
  fi

  uid="$(id -u "${username}" 2>/dev/null || true)"
  if [[ -z "${uid}" ]]; then
    return
  fi

  runtime_bus="/run/user/${uid}/bus"
  if [[ ! -S "${runtime_bus}" ]]; then
    return
  fi

  favorites="$(sudo -u "${username}" DBUS_SESSION_BUS_ADDRESS="unix:path=${runtime_bus}" gsettings get org.gnome.shell favorite-apps 2>/dev/null || true)"
  if [[ -z "${favorites}" || "${favorites}" != *"${APP_ID}.desktop"* ]]; then
    return
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    return
  fi

  updated="$(python3 - "${favorites}" "${APP_ID}.desktop" <<'PY'
import ast
import sys

value = sys.argv[1]
target = sys.argv[2]

try:
    items = ast.literal_eval(value)
except Exception:
    print("")
    raise SystemExit(0)

if not isinstance(items, list):
    print("")
    raise SystemExit(0)

items = [entry for entry in items if entry != target]
print("[" + ", ".join(repr(entry) for entry in items) + "]")
PY
)"

  if [[ -n "${updated}" && "${updated}" != "${favorites}" ]]; then
    sudo -u "${username}" DBUS_SESSION_BUS_ADDRESS="unix:path=${runtime_bus}" gsettings set org.gnome.shell favorite-apps "${updated}" >/dev/null 2>&1 || true
    echo "Removed taskbar shortcut for user: ${username}"
  fi
}

remove_gnome_taskbar_shortcut() {
  local username _ uid _ _ home shell

  while IFS=: read -r username _ uid _ _ home shell; do
    if [[ "${uid}" -lt 1000 || ! -d "${home}" ]]; then
      continue
    fi

    if [[ "${shell}" == "/usr/sbin/nologin" || "${shell}" == "/bin/false" ]]; then
      continue
    fi

    remove_gnome_taskbar_shortcut_for_user "${username}"
  done < <(getent passwd)
}

if ! command -v sudo >/dev/null 2>&1; then
  echo "Error: sudo is required for system-wide uninstall." >&2
  exit 1
fi

echo "Uninstalling ${APP_NAME}"
echo "Removing: ${DESKTOP_FILE}"
echo "Removing: ${WRAPPER_PATH}"
echo "Removing: ${INSTALL_ROOT}"
echo "Removing: ${TMPFILES_CONF}"

sudo rm -f "${DESKTOP_FILE}"
sudo rm -f "${WRAPPER_PATH}"
sudo rm -rf "${INSTALL_ROOT}"
sudo rm -f "${TMPFILES_CONF}"
sudo rm -f "${MCC_SPI_LOCKFILE}"

remove_wf_panel_shortcut
remove_lxpanel_shortcut
remove_gnome_taskbar_shortcut

if command -v update-desktop-database >/dev/null 2>&1; then
  sudo update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
fi

echo "Uninstall complete."
