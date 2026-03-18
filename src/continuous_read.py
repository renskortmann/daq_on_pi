import struct
import time
import threading
import queue
import math
from collections import deque

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.widgets import Button

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # pip install tomli

from daqhats import (
    AnalogInputMode,
    AnalogInputRange,
    HatError,
    HatIDs,
    OptionFlags,
    mcc128,
)

from daqhats_utils import select_hat_device


def load_config(path='src/config.toml'):
    with open(path, 'rb') as f:
        return tomllib.load(f)


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


def main():
    config = load_config()

    phys_channel = config['daq']['phys_channel']
    software_channel = phys_channel - 1  # zero-based for API calls
    sample_rate = config['daq']['sample_rate']
    read_update_period = config['daq']['read_update_period']
    input_mode = config['daq']['input_mode']
    input_range = config['daq']['input_range']
    cutoff_frequency_hz = config['daq']['cutoff_frequency_hz']

    if sample_rate <= 0:
        raise ValueError('sample_rate must be greater than 0 Hz')
    if cutoff_frequency_hz <= 0:
        raise ValueError('cutoff_frequency_hz must be greater than 0 Hz')

    nyquist_hz = sample_rate / 2.0
    if cutoff_frequency_hz >= nyquist_hz:
        cutoff_coeff = 1.0
    else:
        dt = 1.0 / sample_rate
        cutoff_coeff = 1.0 - math.exp(-2.0 * math.pi * cutoff_frequency_hz * dt)

    scope_time_base = config['display']['scope_time_base']
    display_refresh_rate = config['display']['refresh_rate']
    display_decimation = int(sample_rate / display_refresh_rate)
    display_y_min = config['display']['y_min']
    display_y_max = config['display']['y_max']

    write_rate = config['writer']['write_rate']
    write_interval = 1.0 / write_rate

    output_data_file = config['files']['output_data_file']
    output_xlsx_file = config['files']['output_xlsx_file']

    load_scales = config['conversion']['load_scale']
    load_offsets = config['conversion']['load_offset']

    if len(load_scales) != 4 or len(load_offsets) != 4:
        raise ValueError('conversion.load_scale and conversion.load_offset must each contain 4 values for channels 1-4')
    if not 1 <= phys_channel <= 4:
        raise ValueError('phys_channel must be in the range 1-4 for conversion parameters')

    channel_index = phys_channel - 1
    load_scale = float(load_scales[channel_index])
    load_offset = float(load_offsets[channel_index])

    address = select_hat_device(HatIDs.MCC_128)
    hat = mcc128(address)

    hat.a_in_mode_write(get_input_mode(input_mode))
    hat.a_in_range_write(get_input_range(input_range))

    channel_mask = 0x01 << software_channel

    read_buffer_size = int(sample_rate * read_update_period)

    hat.a_in_scan_start(
        channel_mask,
        samples_per_channel=0,
        sample_rate_per_channel=sample_rate,
        options=OptionFlags.CONTINUOUS,
    )

    stop_event = threading.Event()

    display_queue = queue.Queue()
    write_queue = queue.Queue()
    command_queue = queue.Queue()

    def acquisition_worker():
        sample_count = 0
        filtered_value = None
        try:
            while not stop_event.is_set():
                read_result = hat.a_in_scan_read(read_buffer_size, timeout=1.0)

                if read_result.hardware_overrun or read_result.buffer_overrun:
                    raise HatError(
                        address,
                        'Scan overrun detected. Reduce sample rate or read faster.',
                    )

                data = read_result.data
                if not data:
                    continue

                for value in data:

                    raw_value = value
                    current_time = sample_count / sample_rate
                    # Single-pole IIR low-pass filter.
                    if filtered_value is None:
                        filtered_value = raw_value
                    else:
                        filtered_value += cutoff_coeff * (raw_value - filtered_value)

                    load_value = load_scale * filtered_value + load_offset
                    display_queue.put((current_time, load_value))
                    write_queue.put((current_time, raw_value, filtered_value, load_value))
                    sample_count += 1
        finally:
            hat.a_in_scan_stop()
            hat.a_in_scan_cleanup()

    def writer_worker():
        data_file = None
        recording = False
        last_write_time = 0

        while not stop_event.is_set():
            # handle start/stop commands
            try:
                command = command_queue.get_nowait()
                if command == 'start' and not recording:
                    data_file = open(output_data_file, 'wb')
                    recording = True
                    last_write_time = 0
                elif command == 'stop' and recording:
                    recording = False
                    if data_file is not None:
                        data_file.close()
                        data_file = None
            except queue.Empty:
                pass

            try:
                timestamp, raw_voltage, filtered_voltage, load_value = write_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            # Only write if enough time has passed since last write
            if recording and data_file is not None:
                if timestamp - last_write_time >= write_interval:
                    data_file.write(struct.pack('<dddd', timestamp, raw_voltage, filtered_voltage, load_value))
                    last_write_time = timestamp

        if data_file is not None:
            data_file.close()

    def display_loop():
        times = deque()
        values = deque()

        plt.ion()
        fig, ax = plt.subplots(figsize=(13, 6))
        fig.subplots_adjust(left=0.1, right=0.65, bottom=0.15, top=0.95)
        line, = ax.plot([], [], lw=1)
        ax.set_xlabel('Time (s)')
        ax.set_ylabel('Load')
        ax.set_title(f'MCC 128 Physical Channel {phys_channel} ({input_mode})')
        ax.set_xlim(0, scope_time_base)
        ax.set_ylim(display_y_min, display_y_max)

        def on_close(_event):
            stop_event.set()

        fig.canvas.mpl_connect('close_event', on_close)

        def start_recording(_event):
            command_queue.put('start')

        def stop_recording(_event):
            command_queue.put('stop')

        def convert_to_xlsx(_event):
            try:
                with open(output_data_file, 'rb') as input_file:
                    payload = input_file.read()
            except FileNotFoundError:
                print(f'No {output_data_file} found to convert.')
                return

            record_size = struct.calcsize('<dddd')

            if len(payload) % record_size != 0:
                print('Data file does not contain stored load values (<dddd> format required); conversion aborted.')
                return

            rows = []
            for offset in range(0, len(payload), record_size):
                timestamp, voltage_raw, voltage_filtered, load = struct.unpack_from('<dddd', payload, offset)
                rows.append((timestamp, voltage_raw, voltage_filtered, load))

            if not rows:
                print('No data found to convert.')
                return

            df = pd.DataFrame(rows, columns=['time_s', 'voltage_raw_v', 'voltage_filtered_v', 'load'])
            df.to_excel(output_xlsx_file, index=False)
            print(f'Wrote {output_xlsx_file} with {len(df)} rows.')

            # Display converted data in a separate window
            fig_data, ax_data = plt.subplots(figsize=(10, 6))
            ax_data.plot(df['time_s'], df['load'], lw=1)
            ax_data.set_xlabel('Time (s)')
            ax_data.set_ylabel('Load')
            ax_data.set_title(f'Converted Data - {len(df)} samples')
            ax_data.grid(True, alpha=0.3)
            plt.show()

        # Create button box with labeled frame on the right
        ax_box = plt.axes([0.68, 0.25, 0.28, 0.6])
        ax_box.set_xlim(0, 1)
        ax_box.set_ylim(0, 1)
        ax_box.axis('off')

        # Draw box around buttons
        from matplotlib.patches import Rectangle
        rect = Rectangle((0.05, 0.05), 0.9, 0.9, linewidth=1, edgecolor='black', facecolor='none')
        ax_box.add_patch(rect)

        # Add label
        ax_box.text(0.1, 0.88, 'Record Data', fontsize=10, fontweight='bold')

        button_ax_start = plt.axes([0.72, 0.62, 0.12, 0.05])
        button_ax_stop = plt.axes([0.72, 0.52, 0.12, 0.05])
        button_ax_convert = plt.axes([0.70, 0.40, 0.18, 0.05])
        button_start = Button(button_ax_start, 'Start')
        button_stop = Button(button_ax_stop, 'Stop')
        button_convert = Button(button_ax_convert, 'Convert to XLSX')
        button_start.on_clicked(start_recording)
        button_stop.on_clicked(stop_recording)
        button_convert.on_clicked(convert_to_xlsx)

        # Add info text below the box
        ax_info = plt.axes([0.68, 0.02, 0.28, 0.15])
        ax_info.set_xlim(0, 1)
        ax_info.set_ylim(0, 1)
        ax_info.axis('off')
        ax_info.text(0.05, 1.2, f'Sample Rate: {sample_rate:.0f} Hz', fontsize=9, family='monospace')
        ax_info.text(0.05, 0.8, f'Write Rate: {write_rate} Hz', fontsize=9, family='monospace')
        ax_info.text(0.05, 0.4, f'Load: {values[-1] if values else 0:.4f} N', fontsize=9, family='monospace')

        sample_count = 0
        display_period = 1.0 / display_refresh_rate
        while not stop_event.is_set():
            # Drain queue of all available samples
            try:
                while True:
                    timestamp, voltage = display_queue.get_nowait()
                    times.append(timestamp)
                    values.append(voltage)
                    sample_count += 1
            except queue.Empty:
                pass

            # Remove old data outside window
            while times and (times[-1] - times[0]) > scope_time_base:
                times.popleft()
                values.popleft()

            # Update plot with decimated data
            if sample_count % display_decimation == 0 and times:
                line.set_data(list(times), list(values))
                # Keep initial range of 0-10s, then scroll after data fills window
                if times[-1] < scope_time_base:
                    ax.set_xlim(0, scope_time_base)
                else:
                    ax.set_xlim(max(0.0, times[-1] - scope_time_base), times[-1])

                data_min = min(values)
                data_max = max(values)
                data_span = data_max - data_min
                if data_span > 0:
                    y_padding = data_span * 0.1
                else:
                    y_padding = max(abs(data_max) * 0.1, 0.1)
                ax.set_ylim(data_min - y_padding, data_max + y_padding)

                ax_info.texts[2].set_text(f'Load: {values[-1] if values else 0:.4f} N')
                fig.canvas.draw_idle()

            fig.canvas.flush_events()
            time.sleep(display_period)

        plt.show()

    acq_thread = threading.Thread(target=acquisition_worker, daemon=True)
    writer_thread = threading.Thread(target=writer_worker, daemon=True)

    acq_thread.start()
    writer_thread.start()

    try:
        display_loop()
    except KeyboardInterrupt:
        stop_event.set()

    acq_thread.join()
    writer_thread.join()


if __name__ == '__main__':
    main()

