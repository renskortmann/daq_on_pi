import tkinter as tk
from tkinter import ttk, messagebox
from collections import deque
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import threading
import queue
import math
import struct
import os

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # pip install tomli

try:
    from daqhats import (
        AnalogInputMode,
        AnalogInputRange,
        HatError,
        HatIDs,
        OptionFlags,
        mcc128,
    )
    from daqhats_utils import select_hat_device
    HAS_DAQHATS = True
except ImportError:
    HAS_DAQHATS = False


def load_config(path='src/config.toml'):
    """Load configuration from TOML file."""
    with open(path, 'rb') as f:
        return tomllib.load(f)


def resolve_initial_checked_channels(raw_channels, num_channels):
    """Resolve startup-checked channels from config (1-based), with safe fallback."""
    default_channels = list(range(1, num_channels + 1))
    if raw_channels is None:
        return default_channels

    if not isinstance(raw_channels, list):
        print("Warning: gui.initial_checked_channels must be a list. Using all channels.")
        return default_channels

    valid = []
    for ch in raw_channels:
        if isinstance(ch, bool) or not isinstance(ch, int):
            print(f"Warning: Ignoring invalid channel entry '{ch}' in gui.initial_checked_channels.")
            continue
        if 1 <= ch <= num_channels and ch not in valid:
            valid.append(ch)
        else:
            print(f"Warning: Ignoring out-of-range channel '{ch}' in gui.initial_checked_channels.")

    if not valid:
        print("Warning: gui.initial_checked_channels had no valid entries. Using all channels.")
        return default_channels

    return valid


def resolve_initial_auto_zero_samples(raw_value):
    """Resolve startup auto-zero sample count from config, with safe fallback."""
    default_value = 50
    if raw_value is None:
        return default_value

    if isinstance(raw_value, bool) or not isinstance(raw_value, int):
        print("Warning: gui.initial_auto_zero_samples must be an integer. Using 50.")
        return default_value

    if raw_value < 1:
        print("Warning: gui.initial_auto_zero_samples must be >= 1. Using 50.")
        return default_value

    return raw_value


class DAQGUIApp:
    def __init__(self, root):
        """Initialize the DAQ GUI application."""
        self.root = root
        self.root.title("DAQ Monitor")
        self.root.geometry("1200x800")
        self.num_channels = 4
        
        # Load configuration
        try:
            self.config = load_config()
            self.input_mode = self.config['daq']['input_mode']
            self.sample_rate = self.config['daq']['sample_rate']
            self.read_update_period = self.config['daq']['read_update_period']
            self.input_range = self.config['daq']['input_range']
            self.cutoff_frequency_hz = self.config['daq']['cutoff_frequency_hz']
            self.scope_time_base = self.config['display']['scope_time_base']
            self.display_refresh_rate = self.config['display']['refresh_rate']
            self.display_decimation = int(self.sample_rate / self.display_refresh_rate)
            self.display_y_min = self.config['display']['y_min']
            self.display_y_max = self.config['display']['y_max']
            self.write_rate = self.config['writer']['write_rate']
            self.output_data_file = self.config['files']['output_data_file']
            self.output_xlsx_file = self.config['files']['output_xlsx_file']
            self.load_scales = self.config['conversion']['load_scale']
            self.load_offsets = self.config['conversion']['load_offset']
            gui_config = self.config.get('gui', {})
            raw_initial_channels = gui_config.get('initial_checked_channels')
            self.initial_checked_channels = resolve_initial_checked_channels(raw_initial_channels, self.num_channels)
            self.initial_auto_zero_samples = resolve_initial_auto_zero_samples(
                gui_config.get('initial_auto_zero_samples')
            )
            if len(self.load_scales) < self.num_channels or len(self.load_offsets) < self.num_channels:
                raise ValueError('conversion.load_scale and conversion.load_offset must contain 4 values')
            self.load_scales = [float(v) for v in self.load_scales[:self.num_channels]]
            self.load_offsets = [float(v) for v in self.load_offsets[:self.num_channels]]
        except Exception as e:
            print(f"Warning: Could not load config: {e}")
            # Set reasonable defaults
            self.input_mode = 'SE'
            self.input_range = 'BIP_10V'
            self.sample_rate = 1000
            self.read_update_period = 0.1
            self.cutoff_frequency_hz = 10
            self.scope_time_base = 10
            self.display_refresh_rate = 10
            self.display_decimation = 100
            self.display_y_min = 0
            self.display_y_max = 10
            self.write_rate = 10
            self.output_data_file = 'output.bin'
            self.output_xlsx_file = 'output.xlsx'
            self.load_scales = [1.0] * self.num_channels
            self.load_offsets = [0.0] * self.num_channels
            self.initial_checked_channels = list(range(1, self.num_channels + 1))
            self.initial_auto_zero_samples = 50

        self.display_decimation = max(1, int(self.sample_rate / self.display_refresh_rate))

        if self.write_rate <= 0:
            self.write_rate = 1
        self.write_interval = 1.0 / self.write_rate
        self.record_struct_fmt = '<' + ('d' * (1 + 3 * self.num_channels))
        
        # Calculate cutoff coefficient for low-pass filter
        nyquist_hz = self.sample_rate / 2.0
        if self.cutoff_frequency_hz >= nyquist_hz:
            self.cutoff_coeff = 1.0
        else:
            dt = 1.0 / self.sample_rate
            self.cutoff_coeff = 1.0 - math.exp(-2.0 * math.pi * self.cutoff_frequency_hz * dt)
        
        # Initialize plot data buffers
        self.times = deque()
        self.channel_values = [deque() for _ in range(self.num_channels)]
        self.sample_count = 0
        self.time_zero_offset = 0.0
        self.pending_time_reset = False
        
        # Threading and queue infrastructure
        self.stop_event = threading.Event()
        self.display_queue = queue.Queue()
        self.write_queue = queue.Queue()
        self.command_queue = queue.Queue()
        
        # DAQ hardware
        self.hat = None
        self.acquisition_thread = None
        self.writer_thread = None
        self.is_recording = False
        
        # Channel selection
        initial_checked_zero_based = {ch - 1 for ch in self.initial_checked_channels}
        self.channel_enabled = [
            tk.BooleanVar(value=(i in initial_checked_zero_based))
            for i in range(self.num_channels)
        ]
        self.active_channels = [i for i in range(self.num_channels) if i in initial_checked_zero_based]
        self.selected_channel_count = len(self.active_channels)

        # Latest per-channel values for dashboard table
        self.latest_raw = [None] * self.num_channels
        self.latest_filtered = [None] * self.num_channels
        self.latest_load = [None] * self.num_channels
        self.pending_startup_zero = bool(self.active_channels)
        self.startup_zero_min_samples = self.initial_auto_zero_samples
        self.startup_zero_sample_count = 0
        self.values_visible = not self.pending_startup_zero
        
        # Create menu bar
        self.create_menu_bar()
        
        # Create toolbar
        self.create_toolbar()
        
        # Create main content area (plot + dashboard)
        self.create_content_area()
        
        # Initialize hardware and start acquisition
        self.initialize_hardware()
        self.start_acquisition()
        self.start_writer()
        
        # Start periodic display update
        self.update_display()
        
        # Handle window close
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
    
    def create_menu_bar(self):
        """Create the menu bar (empty for now)."""
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)
        
        # Add placeholder menus
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)
        
        edit_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Edit", menu=edit_menu)
        
        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Help", menu=help_menu)
    
    def create_toolbar(self):
        """Create the toolbar and wire up recording actions."""
        self.toolbar = ttk.Frame(self.root, height=50, relief=tk.RAISED, borderwidth=1)
        self.toolbar.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)
        
        # Create a labeled group for data recording buttons
        small_font = ('TkDefaultFont', 8)
        recording_group = ttk.LabelFrame(self.toolbar, text="Data Recording", padding=5)
        recording_group.pack(side=tk.LEFT, fill=tk.X, padx=5)
        
        # Style the label with small font
        style = ttk.Style()
        style.configure('TLabelframe.Label', font=small_font)
        style.configure('StartBlack.TButton', foreground='black')
        style.configure('StartRecording.TButton', foreground='red')
        
        self.start_button = ttk.Button(
            recording_group,
            text="Start",
            style='StartBlack.TButton',
            command=self.on_start_recording,
        )
        self.stop_button = ttk.Button(recording_group, text="Stop", command=self.on_stop_recording)
        self.convert_button = ttk.Button(recording_group, text="Convert to XLSX", command=self.on_convert_to_xlsx)

        channels_group = ttk.LabelFrame(self.toolbar, text="Channels", padding=5)
        channels_group.pack(side=tk.LEFT, fill=tk.X, padx=5)
        self.zero_button = ttk.Button(channels_group, text="zero channels", command=self.on_zero_channels)

        self.start_button.pack(side=tk.LEFT, padx=5)
        self.stop_button.pack(side=tk.LEFT, padx=5)
        self.convert_button.pack(side=tk.LEFT, padx=5)
        self.zero_button.pack(side=tk.LEFT, padx=5)
    
    def create_content_area(self):
        """Create the main content area with DAQ plot on left and dashboard on right."""
        # Main container
        content_frame = ttk.Frame(self.root)
        content_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # Left side - matplotlib plot
        left_frame = ttk.Frame(content_frame)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))
        
        # Channel selection checkboxes
        checkbox_frame = ttk.LabelFrame(left_frame, text="Channels", padding=5)
        checkbox_frame.pack(fill=tk.X, padx=0, pady=(0, 5))
        
        self.checkbox_widgets = []
        for channel_idx in range(self.num_channels):
            var = self.channel_enabled[channel_idx]
            chk = ttk.Checkbutton(
                checkbox_frame,
                text=f'Channel {channel_idx + 1}',
                variable=var,
                command=self.on_channel_selection_changed
            )
            chk.pack(side=tk.LEFT, padx=5)
            self.checkbox_widgets.append(chk)
        
        # Create matplotlib figure and axis
        self.fig = Figure(figsize=(7, 6), dpi=100)
        self.ax = self.fig.add_subplot(111)
        
        # Set up multi-channel lines.
        self.lines = []
        for channel_idx in range(self.num_channels):
            line, = self.ax.plot([], [], lw=1, label=f'Channel {channel_idx + 1}')
            self.lines.append(line)
        self.ax.set_xlabel('Time (s)')
        self.ax.set_ylabel('Load (N)')
        #self.ax.set_title(f'MCC 128 Physical Channels 1-4 ({self.input_mode})')
        self.ax.set_xlim(0, self.scope_time_base)
        self.ax.set_ylim(self.display_y_min, self.display_y_max)
        self.ax.grid(True, alpha=0.3)
        self.ax.legend(loc='upper right')
        
        # Embed matplotlib in tkinter
        self.canvas = FigureCanvasTkAgg(self.fig, master=left_frame)
        self.canvas.draw()
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        
        # Right side - dashboard grid
        right_frame = ttk.LabelFrame(content_frame, text="Dashboard", padding=10)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)
        
        # Create a grid layout for the dashboard
        self.dashboard_frame = ttk.Frame(right_frame)
        self.dashboard_frame.pack(fill=tk.BOTH, expand=True)
        
        # Configure grid columns and rows
        self.dashboard_frame.grid_rowconfigure(0, weight=0)
        self.dashboard_frame.grid_rowconfigure(1, weight=0)
        self.dashboard_frame.grid_rowconfigure(2, weight=0)
        self.dashboard_frame.grid_rowconfigure(3, weight=1)
        self.dashboard_frame.grid_columnconfigure(0, weight=1)
        
        # Channel values table
        table_frame = ttk.Frame(self.dashboard_frame)
        table_frame.grid(row=0, column=0, sticky="nsew")

        columns = ('channel', 'raw', 'filtered', 'load')
        self.table = ttk.Treeview(table_frame, columns=columns, show='headings', height=self.num_channels)
        self.table.heading('channel', text='Channel')
        self.table.heading('raw',     text='Raw (V)')
        self.table.heading('filtered', text='Filtered (V)')
        self.table.heading('load',    text='Load (N)')
        for col in columns:
            self.table.column(col, anchor=tk.CENTER, width=90)
        self.table.pack(fill=tk.X, expand=False)

        # Pre-populate one row per channel; rows are shown/hidden via update_table
        for i in range(self.num_channels):
            self.table.insert('', tk.END, iid=str(i), values=(f'Channel {i + 1}', '—', '—', '—'))

        # Secondary table for manual/derived values
        table_gap_px = int(round(self.root.winfo_fpixels('2m')))
        derived_table_frame = ttk.Frame(self.dashboard_frame)
        derived_table_frame.grid(row=1, column=0, sticky="ew", pady=(table_gap_px, 0))

        # Editable secondary table (5 rows x 4 columns)
        headers = ('Weight (g)', 'Position (mm)', 'Load top (N)', 'Load bottom (N)')
        border_color = '#d3d3d3'

        for col_idx, header_text in enumerate(headers):
            header = tk.Label(
                derived_table_frame,
                text=header_text,
                anchor='center',
                relief='solid',
                borderwidth=1,
                highlightthickness=1,
                highlightbackground=border_color,
                bg='white',
            )
            header.grid(row=0, column=col_idx, sticky='nsew')
            derived_table_frame.grid_columnconfigure(col_idx, weight=1)

        preset_weights = (50, 100, 150, 200, 250)
        self.derived_entries = []
        for row_idx in range(1, 6):
            row_entries = []
            for col_idx in range(len(headers)):
                entry = tk.Entry(
                    derived_table_frame,
                    justify='center',
                    relief='solid',
                    borderwidth=1,
                    highlightthickness=1,
                    highlightbackground=border_color,
                    highlightcolor=border_color,
                )
                entry.grid(row=row_idx, column=col_idx, sticky='nsew')
                if col_idx == 0:
                    entry.insert(0, str(preset_weights[row_idx - 1]))
                row_entries.append(entry)
            self.derived_entries.append(row_entries)

        # Draw button below the secondary table
        self.draw_button = ttk.Button(self.dashboard_frame, text='Draw', command=self.on_draw_dashboard_plot)
        self.draw_button.grid(row=2, column=0, sticky='w', pady=(8, 0))

        # Plot area under the secondary table and Draw button
        self.dashboard_plot_frame = ttk.Frame(self.dashboard_frame)
        self.dashboard_plot_frame.grid(row=3, column=0, sticky='nsew', pady=(8, 0))

        self.dashboard_plot_fig = Figure(figsize=(5, 3), dpi=100)
        self.dashboard_plot_ax_left = self.dashboard_plot_fig.add_subplot(111)
        self.dashboard_plot_ax_right = self.dashboard_plot_ax_left.twinx()
        self.dashboard_plot_canvas = FigureCanvasTkAgg(self.dashboard_plot_fig, master=self.dashboard_plot_frame)
        self.dashboard_plot_canvas.draw()
        self.dashboard_plot_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # Initialise row visibility
        self.update_table()
    
    def initialize_hardware(self):
        """Initialize DAQ hardware if available."""
        if not HAS_DAQHATS:
            print("Warning: daqhats module not available. Running in simulation mode.")
            return
        
        try:
            def get_input_mode(mode_str):
                """Convert config string to AnalogInputMode"""
                if mode_str == 'SE':
                    return AnalogInputMode.SE
                elif mode_str == 'DIFF':
                    return AnalogInputMode.DIFF
                else:
                    raise ValueError(f"Unknown input mode: {mode_str}")

            def get_input_range(range_str):
                """Convert config string to AnalogInputRange"""
                ranges = {
                    'BIP_10V': AnalogInputRange.BIP_10V,
                    'BIP_5V': AnalogInputRange.BIP_5V,
                    'BIP_2V': AnalogInputRange.BIP_2V,
                    'BIP_1V': AnalogInputRange.BIP_1V,
                }
                if range_str not in ranges:
                    raise ValueError(f"Unknown input range: {range_str}")
                return ranges[range_str]

            address = select_hat_device(HatIDs.MCC_128)
            self.hat = mcc128(address)
            
            self.hat.a_in_mode_write(get_input_mode(self.input_mode))
            self.hat.a_in_range_write(get_input_range(self.input_range))
            
            # Always acquire all 4 physical channels from hardware, but only process selected ones.
            self.channel_mask = 0x0F
            self.read_buffer_size = int(self.sample_rate * self.read_update_period)
            
            self.hat.a_in_scan_start(
                self.channel_mask,
                samples_per_channel=0,
                sample_rate_per_channel=self.sample_rate,
                options=OptionFlags.CONTINUOUS,
            )
            
            print(f"DAQ hardware initialized: MCC 128 at address {address}")
        except Exception as e:
            print(f"Warning: Could not initialize hardware: {e}")
            print("Running in simulation mode.")
            self.hat = None
    
    def start_acquisition(self):
        """Start the data acquisition thread."""
        # Simulate some random walk data if no hat
        if self.hat is None:
            import random

            def simulate_data():
                values = [float(2 * (i + 1)) for i in range(self.num_channels)]
                sample_count = 0
                while not self.stop_event.is_set():
                    for idx in range(self.num_channels):
                        values[idx] += random.gauss(0, 0.5)
                        values[idx] = max(self.display_y_min, min(self.display_y_max, values[idx]))
                    current_time = sample_count / self.sample_rate
                    raw_values = tuple(values)
                    filtered_values = tuple(values)
                    load_values = tuple(values)
                    self.display_queue.put((current_time, raw_values, filtered_values, load_values))
                    self.write_queue.put((current_time, raw_values, filtered_values, load_values))
                    sample_count += 1
                    if sample_count % max(1, int(self.sample_rate / 100)) == 0:
                        threading.Event().wait(0.01)
            
            self.acquisition_thread = threading.Thread(target=simulate_data, daemon=True)
        else:
            self.acquisition_thread = threading.Thread(target=self.acquisition_worker, daemon=True)
        
        self.acquisition_thread.start()
        print("Data acquisition started")

    def start_writer(self):
        """Start the file writer worker thread."""
        self.writer_thread = threading.Thread(target=self.writer_worker, daemon=True)
        self.writer_thread.start()

    def writer_worker(self):
        """Worker thread that handles start/stop recording and binary file writes."""
        data_file = None
        recording = False
        last_write_time = 0.0
        recording_time_offset = None
        active_channels_at_start = None
        record_struct_fmt = None

        try:
            while not self.stop_event.is_set():
                try:
                    while True:
                        command = self.command_queue.get_nowait()
                        if command == 'start' and not recording:
                            active_channels_at_start = self.active_channels.copy()
                            record_struct_fmt = '<' + ('d' * (1 + 3 * len(active_channels_at_start)))
                            data_file = open(self.output_data_file, 'wb')
                            recording = True
                            self.is_recording = True
                            last_write_time = 0.0
                            recording_time_offset = None
                            print(f"Recording started: {self.output_data_file} (channels: {[ch+1 for ch in active_channels_at_start]})")
                        elif command == 'stop' and recording:
                            recording = False
                            self.is_recording = False
                            if data_file is not None:
                                data_file.close()
                                data_file = None
                            # Store metadata about which channels were recorded
                            if active_channels_at_start is not None:
                                with open(self.output_data_file + '.meta', 'w') as meta_file:
                                    meta_file.write(','.join(str(ch + 1) for ch in active_channels_at_start))
                            print("Recording stopped")
                except queue.Empty:
                    pass

                try:
                    timestamp, raw_values, filtered_values, load_values = self.write_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                if recording and data_file is not None and (timestamp - last_write_time) >= self.write_interval:
                    if active_channels_at_start is not None:
                        if recording_time_offset is None:
                            recording_time_offset = timestamp

                        # Only write data from the selected channels
                        selected_raw = tuple(raw_values[ch] for ch in active_channels_at_start)
                        selected_filtered = tuple(filtered_values[ch] for ch in active_channels_at_start)
                        selected_load = tuple(load_values[ch] for ch in active_channels_at_start)
                        packed = [timestamp - recording_time_offset]
                        packed.extend(selected_raw)
                        packed.extend(selected_filtered)
                        packed.extend(selected_load)
                        data_file.write(struct.pack(record_struct_fmt, *packed))
                    last_write_time = timestamp
        finally:
            self.is_recording = False
            if data_file is not None:
                data_file.close()

    def on_start_recording(self):
        """Start binary recording to the configured data file."""
        self.update_active_channels()
        if not self.active_channels:
            messagebox.showwarning("No Channels Selected", "Please select at least one channel to record.")
            return

        # Reset displayed timeline so recording starts at t=0 on the plot.
        self.times.clear()
        for channel_series in self.channel_values:
            channel_series.clear()
        self.sample_count = 0
        self.pending_time_reset = True

        for line in self.lines:
            line.set_data([], [])
        self.ax.set_xlim(0, self.scope_time_base)
        self.canvas.draw_idle()

        self.convert_button.config(state='disabled')
        self.start_button.config(text='Recording', style='StartRecording.TButton')
        self.disable_channel_checkboxes()
        self.command_queue.put('start')

    def on_stop_recording(self):
        """Stop binary recording if active."""
        self.convert_button.config(state='normal')
        self.start_button.config(text='Start', style='StartBlack.TButton')
        self.enable_channel_checkboxes()
        self.command_queue.put('stop')

    def on_zero_channels(self):
        """Zero selected channels by subtracting each channel's latest load from its offset."""
        self.update_active_channels()
        self.zero_selected_channels(show_warnings=True)

    def reset_display_state(self):
        """Clear displayed samples and restart the visible timeline from the next sample."""
        self.times.clear()
        for channel_series in self.channel_values:
            channel_series.clear()
        self.sample_count = 0
        self.pending_time_reset = True

        try:
            while True:
                self.display_queue.get_nowait()
        except queue.Empty:
            pass

        for line in self.lines:
            line.set_data([], [])
        self.ax.set_xlim(0, self.scope_time_base)
        self.canvas.draw_idle()

    def zero_selected_channels(self, show_warnings=True):
        """Apply zeroing to all currently selected channels using latest load values."""
        if not self.active_channels:
            if show_warnings:
                messagebox.showwarning("No Channels Selected", "Please select at least one channel to zero.")
            return False

        zeroed_count = 0
        for channel_idx in self.active_channels:
            last_load = self.latest_load[channel_idx]
            if last_load is None:
                continue

            self.load_offsets[channel_idx] -= last_load
            zeroed_count += 1

            if self.latest_filtered[channel_idx] is not None:
                self.latest_load[channel_idx] = (
                    self.load_scales[channel_idx] * self.latest_filtered[channel_idx]
                ) + self.load_offsets[channel_idx]

        if zeroed_count == 0:
            if show_warnings:
                messagebox.showwarning(
                    "No Data Available",
                    "No calculated load values are available yet for the selected channels.",
                )
            return False

        # First successful zeroing after startup enables table/plot value display.
        self.pending_startup_zero = False
        self.startup_zero_sample_count = 0
        self.values_visible = True
        self.reset_display_state()

        self.update_table()
        return True

    def on_channel_selection_changed(self):
        """Called when user changes channel selection."""
        if not self.is_recording:
            self.update_active_channels()
            self.update_legend()
            self.update_table()

    def on_draw_dashboard_plot(self):
        """Draw position/load curves from the editable derived table."""
        weights = []
        positions = []
        loads_top = []
        loads_bottom = []

        for row_idx, row_entries in enumerate(self.derived_entries, start=1):
            row_values = [entry.get().strip() for entry in row_entries]

            # Ignore rows where position/load fields are all blank.
            if not any(row_values[1:]):
                continue

            if any(v == '' for v in row_values):
                messagebox.showwarning(
                    "Incomplete Row",
                    f"Row {row_idx} is incomplete. Fill all values or leave position/load fields blank.",
                )
                return

            try:
                weight = float(row_values[0])
                position = float(row_values[1])
                load_top = float(row_values[2])
                load_bottom = float(row_values[3])
            except ValueError:
                messagebox.showwarning(
                    "Invalid Number",
                    f"Row {row_idx} contains a non-numeric value.",
                )
                return

            weights.append(weight)
            positions.append(position)
            loads_top.append(load_top)
            loads_bottom.append(load_bottom)

        if not weights:
            messagebox.showwarning(
                "No Data",
                "Enter at least one complete row in the second table before drawing.",
            )
            return

        self.dashboard_plot_fig.clear()
        self.dashboard_plot_ax_left = self.dashboard_plot_fig.add_subplot(111)
        self.dashboard_plot_ax_right = self.dashboard_plot_ax_left.twinx()

        line_pos, = self.dashboard_plot_ax_left.plot(weights, positions, marker='o', lw=1.5, label='Position')
        line_top, = self.dashboard_plot_ax_right.plot(weights, loads_top, marker='s', lw=1.5, label='Load top')
        line_bottom, = self.dashboard_plot_ax_right.plot(weights, loads_bottom, marker='^', lw=1.5, label='Load bottom')

        self.dashboard_plot_ax_left.set_xlabel('Weight (g)')
        self.dashboard_plot_ax_left.set_ylabel('Position (mm)')
        self.dashboard_plot_ax_right.set_ylabel('Load (N)')
        self.dashboard_plot_ax_left.grid(True, alpha=0.3)

        all_lines = [line_pos, line_top, line_bottom]
        self.dashboard_plot_ax_left.legend(all_lines, [line.get_label() for line in all_lines], loc='best')

        self.dashboard_plot_fig.tight_layout()
        self.dashboard_plot_canvas.draw_idle()

    def update_active_channels(self):
        """Update the list of active channels based on checkbox state."""
        self.active_channels = [i for i in range(self.num_channels) if self.channel_enabled[i].get()]
        self.selected_channel_count = len(self.active_channels)

    def update_legend(self):
        """Update the plot legend to only show visible (active) channels."""
        visible_lines = [self.lines[i] for i in self.active_channels]
        if visible_lines:
            self.ax.legend(handles=visible_lines, loc='upper right')
        else:
            self.ax.legend([], loc='upper right')
        self.canvas.draw_idle()

    def update_table(self):
        """Refresh the dashboard table rows to show only active channels with latest values."""
        for i in range(self.num_channels):
            if i in self.active_channels:
                if self.values_visible:
                    raw = self.latest_raw[i]
                    flt = self.latest_filtered[i]
                    load = self.latest_load[i]
                else:
                    raw = None
                    flt = None
                    load = None
                self.table.item(str(i), values=(
                    f'Channel {i + 1}',
                    f'{raw:.4f}' if raw is not None else '—',
                    f'{flt:.4f}' if flt is not None else '—',
                    f'{load:.4f}' if load is not None else '—',
                ))
                # Ensure row is visible (move to end if it was detached)
                if not self.table.exists(str(i)) or str(i) not in self.table.get_children():
                    self.table.reattach(str(i), '', tk.END)
            else:
                # Hide the row by detaching it
                if self.table.exists(str(i)) and str(i) in self.table.get_children():
                    self.table.detach(str(i))

    def disable_channel_checkboxes(self):
        """Disable all channel checkboxes during recording."""
        for chk in self.checkbox_widgets:
            chk.config(state='disabled')

    def enable_channel_checkboxes(self):
        """Enable all channel checkboxes after recording stops."""
        for chk in self.checkbox_widgets:
            chk.config(state='normal')

    def on_convert_to_xlsx(self):
        """Convert recorded binary data to XLSX and plot all channel loads."""
        if self.is_recording:
            messagebox.showwarning("Recording Active", "Stop recording before converting to XLSX.")
            return

        if not os.path.exists(self.output_data_file):
            messagebox.showwarning("No Data File", f"No {self.output_data_file} found to convert.")
            return

        # Read metadata to determine which channels were recorded
        recorded_channels = list(range(1, self.num_channels + 1))
        meta_file = self.output_data_file + '.meta'
        if os.path.exists(meta_file):
            try:
                with open(meta_file, 'r') as f:
                    recorded_channels = [int(ch.strip()) for ch in f.read().strip().split(',')]
            except Exception as e:
                print(f"Warning: Could not read channel metadata: {e}")

        try:
            import pandas as pd
        except ImportError:
            messagebox.showerror("Missing Dependency", "pandas is required for XLSX conversion.")
            return

        try:
            with open(self.output_data_file, 'rb') as input_file:
                payload = input_file.read()

            # Calculate record size based on number of recorded channels
            record_struct_fmt = '<' + ('d' * (1 + 3 * len(recorded_channels)))
            record_size = struct.calcsize(record_struct_fmt)
            
            if len(payload) % record_size != 0:
                messagebox.showerror(
                    "Invalid Data Format",
                    f"Data file size ({len(payload)} bytes) does not match expected size for {len(recorded_channels)} channel(s). "
                    f"Expected record size: {record_size} bytes.",
                )
                return

            rows = []
            for offset in range(0, len(payload), record_size):
                unpacked = struct.unpack_from(record_struct_fmt, payload, offset)
                row = {'time_s': unpacked[0]}
                raw_start = 1
                filtered_start = 1 + len(recorded_channels)
                load_start = 1 + (2 * len(recorded_channels))
                for idx, channel_label in enumerate(recorded_channels):
                    row[f'ch{channel_label}_raw_v'] = unpacked[raw_start + idx]
                    row[f'ch{channel_label}_filtered_v'] = unpacked[filtered_start + idx]
                    row[f'ch{channel_label}_load'] = unpacked[load_start + idx]
                rows.append(row)

            if not rows:
                messagebox.showinfo("No Data", "No data found to convert.")
                return

            df = pd.DataFrame(rows)
            df.to_excel(self.output_xlsx_file, index=False)
            df_from_xlsx = pd.read_excel(self.output_xlsx_file)

            required_load_columns = [f'ch{ch}_load' for ch in recorded_channels]
            if 'time_s' not in df_from_xlsx.columns or any(col not in df_from_xlsx.columns for col in required_load_columns):
                messagebox.showerror(
                    "Invalid XLSX Format",
                    f"XLSX file must contain 'time_s' and channel {recorded_channels} load columns.",
                )
                return

            fig_data, ax_data = plt.subplots(figsize=(10, 6))
            for channel_label in recorded_channels:
                load_column = f'ch{channel_label}_load'
                ax_data.plot(df_from_xlsx['time_s'], df_from_xlsx[load_column], lw=1, label=f'Channel {channel_label}')
            ax_data.set_xlabel('Time (s)')
            ax_data.set_ylabel('Load (N)')
            ax_data.set_title(f'Converted Data (Channels {recorded_channels}) - {len(df_from_xlsx)} samples')
            ax_data.grid(True, alpha=0.3)
            ax_data.legend(loc='upper right')
            fig_data.tight_layout()
            plt.show(block=False)

            messagebox.showinfo("Conversion Complete", f"Wrote {self.output_xlsx_file} with {len(df)} rows.")
        except Exception as exc:
            messagebox.showerror("Conversion Failed", str(exc))
    
    def acquisition_worker(self):
        """Worker thread that reads from DAQ hardware."""
        sample_count = 0
        filtered_values = [None] * self.num_channels
        
        try:
            while not self.stop_event.is_set():
                read_result = self.hat.a_in_scan_read(self.read_buffer_size, timeout=1.0)
                
                if read_result.hardware_overrun or read_result.buffer_overrun:
                    raise HatError(
                        0,
                        'Scan overrun detected. Reduce sample rate or read faster.',
                    )
                
                data = read_result.data
                if not data:
                    continue

                usable_count = len(data) - (len(data) % self.num_channels)
                for offset in range(0, usable_count, self.num_channels):
                    raw_values = data[offset: offset + self.num_channels]
                    current_time = sample_count / self.sample_rate

                    filtered_frame = []
                    load_frame = []
                    for channel_idx, raw_value in enumerate(raw_values):
                        if filtered_values[channel_idx] is None:
                            filtered_values[channel_idx] = raw_value
                        else:
                            filtered_values[channel_idx] += self.cutoff_coeff * (raw_value - filtered_values[channel_idx])
                        load_value = (self.load_scales[channel_idx] * filtered_values[channel_idx]) + self.load_offsets[channel_idx]
                        filtered_frame.append(filtered_values[channel_idx])
                        load_frame.append(load_value)

                    self.display_queue.put((current_time, tuple(raw_values), tuple(filtered_frame), tuple(load_frame)))
                    self.write_queue.put((current_time, tuple(raw_values), tuple(filtered_frame), tuple(load_frame)))
                    sample_count += 1
        except Exception as e:
            print(f"Error in acquisition worker: {e}")
        finally:
            if self.hat is not None:
                try:
                    self.hat.a_in_scan_stop()
                    self.hat.a_in_scan_cleanup()
                except:
                    pass
    
    def update_display(self):
        """Periodically update the plot with new data from the display queue."""
        if not self.stop_event.is_set():
            # Drain queue of all available samples
            try:
                while True:
                    timestamp, raw_vals, filtered_vals, load_vals = self.display_queue.get_nowait()
                    if len(load_vals) != self.num_channels:
                        continue

                    current_load_vals = [
                        (self.load_scales[channel_idx] * filtered_vals[channel_idx]) + self.load_offsets[channel_idx]
                        for channel_idx in range(self.num_channels)
                    ]

                    for channel_idx in range(self.num_channels):
                        self.latest_raw[channel_idx] = raw_vals[channel_idx]
                        self.latest_filtered[channel_idx] = filtered_vals[channel_idx]
                        self.latest_load[channel_idx] = current_load_vals[channel_idx]

                    if self.pending_startup_zero:
                        self.startup_zero_sample_count += 1
                        if self.startup_zero_sample_count >= self.startup_zero_min_samples:
                            self.zero_selected_channels(show_warnings=False)
                            break
                        continue

                    if self.pending_time_reset:
                        self.time_zero_offset = timestamp
                        self.pending_time_reset = False

                    display_time = timestamp - self.time_zero_offset
                    if display_time < 0:
                        display_time = 0.0

                    self.times.append(display_time)
                    for channel_idx in range(self.num_channels):
                        self.channel_values[channel_idx].append(current_load_vals[channel_idx])
                    self.sample_count += 1
            except queue.Empty:
                pass
            
            # Remove old data outside window
            while self.times and (self.times[-1] - self.times[0]) > self.scope_time_base:
                self.times.popleft()
                for channel_idx in range(self.num_channels):
                    if self.channel_values[channel_idx]:
                        self.channel_values[channel_idx].popleft()
            
            # Update plot with decimated data
            if self.sample_count % self.display_decimation == 0 and self.times:
                times_list = list(self.times)
                for channel_idx, line in enumerate(self.lines):
                    if channel_idx in self.active_channels:
                        line.set_data(times_list, list(self.channel_values[channel_idx]))
                        line.set_visible(True)
                    else:
                        line.set_visible(False)
                
                # Update legend and dashboard table
                self.update_legend()
                self.update_table()
                
                # Keep initial range of 0-scope_time_base, then scroll after data fills window
                if self.times[-1] < self.scope_time_base:
                    self.ax.set_xlim(0, self.scope_time_base)
                else:
                    self.ax.set_xlim(max(0.0, self.times[-1] - self.scope_time_base), self.times[-1])

                mins = [min(self.channel_values[ch]) for ch in self.active_channels if self.channel_values[ch]]
                maxs = [max(self.channel_values[ch]) for ch in self.active_channels if self.channel_values[ch]]
                if not mins or not maxs:
                    self.root.after(100, self.update_display)
                    return

                data_min = min(mins)
                data_max = max(maxs)
                data_span = data_max - data_min
                if data_span > 0:
                    y_padding = data_span * 0.1
                else:
                    y_padding = max(abs(data_max) * 0.1, 0.1)
                self.ax.set_ylim(data_min - y_padding, data_max + y_padding)
                
                self.canvas.draw_idle()
            
            # Schedule next update (10 Hz)
            self.root.after(100, self.update_display)
    
    def on_closing(self):
        """Handle window close event."""
        self.command_queue.put('stop')
        self.stop_event.set()
        if self.acquisition_thread is not None:
            self.acquisition_thread.join(timeout=2.0)
        if self.writer_thread is not None:
            self.writer_thread.join(timeout=2.0)
        self.root.destroy()


def main():
    """Main entry point for the GUI application."""
    root = tk.Tk()
    app = DAQGUIApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
