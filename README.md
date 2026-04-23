# DAQ on Pi

A data acquisition application for Raspberry Pi using the [MCC 128](https://www.mccdaq.com/DAQ-HAT/MCC-128.aspx) analog input HAT. Provides a real-time GUI for monitoring, filtering, and recording multi-channel voltage signals with optional linear calibration.

## Features

- Real-time oscilloscope-style display of up to 4 analog channels
- Configurable single-ended (SE) or differential (DIFF) input mode
- Per-channel first-order low-pass filter (configurable cutoff frequency)
- Linear scaling/calibration (scale + offset) per channel for engineering unit conversion (e.g. Newtons)
- Auto-zero on startup
- Recording to binary (`.dat`) and Excel (`.xlsx`) formats
- Simulation mode when MCC 128 hardware is not present
- System-wide deployment with a desktop launcher and `daq-monitor` CLI command
- Alternative browser-based interface via Dash/Plotly (`web_server.py`)

## Hardware Requirements

- Raspberry Pi (any model with 40-pin GPIO)
- [MCC 128 DAQ HAT](https://www.mccdaq.com/DAQ-HAT/MCC-128.aspx)
- MCC daqhats driver installed — see the [daqhats repository](https://github.com/mccdaq/daqhats) for installation instructions

## Software Requirements

- Python 3.9+
- Python packages (install into a `venv`):

```
matplotlib
pandas
openpyxl
tomli          # only required on Python < 3.11
daqhats
dash           # only required for web_server.py
plotly         # only required for web_server.py
```

## Project Structure

```
daq_on_pi/
├── src/
│   ├── config.toml          # All runtime configuration
│   ├── gui.py               # Main Tkinter GUI application
│   ├── web_server.py        # Alternative Dash/Plotly web interface
│   ├── continuous_read.py   # Headless CLI acquisition script
│   └── daqhats_utils.py     # MCC HAT discovery helpers
├── scripts/
│   ├── deploy_systemwide.sh   # System-wide installation script
│   └── uninstall_systemwide.sh
└── data.dat.meta            # Channel metadata for recorded data files
```

## Configuration

All settings are in `src/config.toml`:

| Section | Key | Description |
|---|---|---|
| `[daq]` | `phys_channel` | Physical channel (1–4 DIFF, 1–8 SE) |
| `[daq]` | `sample_rate` | Acquisition rate in Hz |
| `[daq]` | `cutoff_frequency_hz` | Low-pass filter cutoff in Hz |
| `[daq]` | `read_update_period` | Internal read poll interval in seconds |
| `[daq]` | `input_mode` | `"SE"` (single-ended) or `"DIFF"` (differential) |
| `[daq]` | `input_range` | `"BIP_10V"`, `"BIP_5V"`, `"BIP_2V"`, or `"BIP_1V"` |
| `[display]` | `scope_time_base` | X-axis time window in seconds |
| `[display]` | `refresh_rate` | Display update rate in Hz |
| `[display]` | `y_min` / `y_max` | Y-axis voltage limits |
| `[writer]` | `write_rate` | Data write rate in Hz |
| `[files]` | `output_data_file` | Binary output filename |
| `[files]` | `output_xlsx_file` | Excel output filename |
| `[conversion]` | `load_scale` | Per-channel linear scale factors |
| `[conversion]` | `load_offset` | Per-channel linear offsets |
| `[gui]` | `initial_checked_channels` | Channels enabled at startup (1-based list) |
| `[gui]` | `initial_auto_zero_samples` | Samples to average for startup auto-zero |

### Calibration

Converted (load) values are computed as:

$$\text{load} = \text{scale} \times \text{voltage} + \text{offset}$$

Edit `load_scale` and `load_offset` in `config.toml` for each channel.

## Running

### GUI (recommended)

```bash
cd daq_on_pi
source venv/bin/activate
python src/gui.py
```

### Headless CLI

```bash
python src/continuous_read.py
```

### Web interface

```bash
python src/web_server.py
# Then open http://<hostname>:8080 in a browser
```

## System-wide Deployment

The deploy script installs the app to `/opt/daq_on_pi`, creates a `daq-monitor` wrapper command, and adds a desktop launcher entry.

**Prerequisites:** a virtual environment must exist at `./venv/` before deploying.

```bash
# Basic install
sudo scripts/deploy_systemwide.sh

# Also grant hardware group access to a user
sudo scripts/deploy_systemwide.sh --add-user <username>
```

After deployment, launch the app from the application menu or run:

```bash
daq-monitor
```

### Hardware group access

The MCC 128 uses SPI/GPIO/I2C. Any user running the app must be a member of the `spi`, `gpio`, and `i2c` groups:

```bash
sudo usermod -aG spi,gpio,i2c <username>
# Log out and back in for changes to take effect
```

Without group membership the app falls back to simulated (random-walk) data silently.

### Uninstall

```bash
sudo scripts/uninstall_systemwide.sh
```

## Data File Format

Recorded `.dat` files use a packed binary format. Each record is a little-endian struct:

```
[timestamp (f64), raw_ch1, filtered_ch1, load_ch1,
                  raw_ch2, filtered_ch2, load_ch2, ...]
```

The accompanying `.dat.meta` file lists the active channel indices (comma-separated, 1-based) at the time of recording.
