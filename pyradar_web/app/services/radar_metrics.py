from pathlib import Path
from typing import Dict, List, Optional, Tuple

_LIGHT_SPEED = 299792458


def _next_pow2(value: int) -> int:
    if value <= 0:
        return 1
    return 1 << (value - 1).bit_length()


def _read_cfg_commands(path: Path) -> List[Tuple[str, str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    commands: List[Tuple[str, str]] = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("%", "#")):
            continue

        parts = stripped.split(None, 1)
        cmd = parts[0]
        params = parts[1] if len(parts) > 1 else ""
        commands.append((cmd, params))

    return commands


def _first_command_params(commands: List[Tuple[str, str]], name: str) -> Optional[List[str]]:
    for cmd, params in commands:
        if cmd == name:
            return params.split()
    return None


def _all_command_params(commands: List[Tuple[str, str]], name: str) -> List[List[str]]:
    items: List[List[str]] = []
    for cmd, params in commands:
        if cmd == name:
            items.append(params.split())
    return items


def _require_params(params: Optional[List[str]], name: str, min_len: int) -> List[str]:
    if params is None:
        raise ValueError(f"Missing {name} command in cfg file")
    if len(params) <= min_len:
        raise ValueError(f"{name} command has insufficient parameters")
    return params


def _parse_waveform_multiplexing(value: str) -> str:
    if "7" in value or "5" in value:
        return "Phased-Array"
    return "TDM-MIMO"


def _calculate_radar_cube_size_kb(range_bins: int, doppler_bins: int, angle_bins: int) -> float:
    radar_cube_size_bytes = range_bins * doppler_bins * angle_bins * 2 * 2
    return round(radar_cube_size_bytes / 1024, 3)


def calculate_radar_metrics(cfg_path: Path) -> Dict[str, object]:
    commands = _read_cfg_commands(cfg_path)

    profile = _require_params(_first_command_params(commands, "profileCfg"), "profileCfg", 10)
    channel = _require_params(_first_command_params(commands, "channelCfg"), "channelCfg", 2)
    frame = _require_params(_first_command_params(commands, "frameCfg"), "frameCfg", 4)
    chirp = _first_command_params(commands, "chirpCfg")
    gui_monitor = _first_command_params(commands, "guiMonitor")
    aoa_fov = _first_command_params(commands, "aoaFovCfg")

    profile_indices = [7, 2, 4, 3, 10, 1, 9, 8]
    if len(profile) <= max(profile_indices):
        raise ValueError("profileCfg command has insufficient parameters")

    profile_values = [profile[i] for i in profile_indices]
    profile_values = list(map(float, profile_values[:-2])) + list(map(int, profile_values[-2:]))
    freq_slope, idle_time, ramp_time, adc_start, sample_rate, freq_start, samples_per_chirp, bursts_per_frame = (
        profile_values
    )

    chirp_time = ramp_time - adc_start
    chirp_cycle_time = idle_time + adc_start + chirp_time
    adc_sampling_time_us = samples_per_chirp / sample_rate * 1e3 if sample_rate else 0
    bandwidth = freq_slope * adc_sampling_time_us
    range_resolution = _LIGHT_SPEED / (2 * bandwidth * 1e6) if bandwidth else 0
    beat_freq = round(0.8 * sample_rate * 1e-3, 3) if sample_rate else 0
    unambiguous_range = _LIGHT_SPEED * beat_freq / (2 * (freq_slope * 1e6)) if freq_slope else 0
    maximum_range = round(samples_per_chirp * range_resolution, 1)

    rx_en = int(float(channel[0]))
    tx_en = int(float(channel[1]))
    cascading = int(float(channel[2]))
    num_rx = bin(rx_en).count("1")
    num_tx = bin(tx_en).count("1")

    chirp_repetition_period = round(num_tx * chirp_cycle_time)

    frame_indices = [4, 2, 1]
    if len(frame) <= max(frame_indices):
        raise ValueError("frameCfg command has insufficient parameters")

    frame_values = [frame[i] for i in frame_indices]
    frame_values = list(map(float, frame_values[:-1])) + list(map(int, frame_values[-1:]))
    frame_periodicity, chirps_per_loop, num_loops = frame_values
    chirps_per_loop = int(chirps_per_loop)
    chirps_per_burst = int(chirps_per_loop * (num_loops + 1))
    chirps_per_frame = int(chirps_per_burst * bursts_per_frame)
    burst_periodicity = round((ramp_time + idle_time) * chirps_per_burst, 3)
    active_frame_time = round(chirp_repetition_period * chirps_per_loop / 1000, 3)
    frame_rate_hz = round(1000 / frame_periodicity, 3) if frame_periodicity else 0

    range_cfar = None
    doppler_cfar = None
    for parts in _all_command_params(commands, "cfarCfg"):
        if len(parts) < 2:
            continue
        if parts[1] == "0" and len(parts) >= 2:
            range_cfar = int(float(parts[-2]))
        if parts[1] == "1" and len(parts) >= 2:
            doppler_cfar = int(float(parts[-2]))

    azimuth_min = None
    azimuth_max = None
    elevation_min = None
    elevation_max = None
    if aoa_fov and len(aoa_fov) >= 5:
        azimuth_min, azimuth_max, elevation_min, elevation_max = map(float, aoa_fov[1:5])

    range_limits = None
    doppler_limits = None
    for parts in _all_command_params(commands, "cfarFovCfg"):
        if len(parts) < 2:
            continue
        if parts[1] == "0" and len(parts) >= 4:
            range_limits = [float(parts[-2]), float(parts[-1])]
        if parts[1] == "1" and len(parts) >= 4:
            doppler_limits = [float(parts[-2]), float(parts[-1])]

    waveform_value = ""
    if chirp and len(chirp) > 7:
        waveform_value = chirp[7]
    waveform_multiplexing = _parse_waveform_multiplexing(waveform_value) if waveform_value else "Unknown"

    virtual_array_length = num_rx * (1 if waveform_multiplexing == "Phased-Array" else num_tx)

    lambda_m = _LIGHT_SPEED / (freq_start * 1e9) if freq_start else 0
    if waveform_multiplexing == "Phased-Array":
        ntx = 1
        num_chirps_cpi = chirps_per_frame or 1
    else:
        ntx = num_tx or 1
        num_chirps_cpi = chirps_per_loop or 1

    if chirp_cycle_time:
        unambiguous_velocity = round(lambda_m / (4 * chirp_cycle_time * 1e-6) * 3.6 / ntx, 1)
    else:
        unambiguous_velocity = 0

    velocity_resolution = round(2 * unambiguous_velocity / num_chirps_cpi, 1) if num_chirps_cpi else 0

    clutter_status = None
    clutter = _first_command_params(commands, "clutterRemoval")
    if clutter:
        clutter_status = int(float(clutter[-1]))

    num_range_fft_bins = _next_pow2(int(samples_per_chirp))
    num_doppler_fft_bins = _next_pow2(int(chirps_per_frame))
    radar_cube_size_kb = _calculate_radar_cube_size_kb(
        int(samples_per_chirp),
        int(chirps_per_frame),
        virtual_array_length,
    )

    metrics = {
        "Number of Samples per Chirp": samples_per_chirp,
        "Maximum Range [m]": maximum_range,
        "Unambiguous Range [m]": unambiguous_range,
        "Unambiguous Velocity [km/h]": unambiguous_velocity,
        "Velocity Resolution [km/h]": velocity_resolution,
        "Frame Rate [Hz]": frame_rate_hz,
        "Frame Periodicity [ms]": frame_periodicity,
        "Number of Chirps in Frame": chirps_per_frame,
        "Number of Range FFT Bins": int(num_range_fft_bins),
        "Number of Doppler FFT Bins": int(num_doppler_fft_bins),
        "Range Resolution [m]": range_resolution,
        "Radar Cube Size [KB]": radar_cube_size_kb,
        "Number of TX Antennas": num_tx,
        "Number of RX Antennas": num_rx,
        "GUI Monitor": gui_monitor,
        "Waveform Multiplexing": waveform_multiplexing,
        "Clutter Removal": clutter_status,
        "Azimuth Min [deg.]": azimuth_min,
        "Azimuth Max [deg.]": azimuth_max,
        "Elevation Min [deg.]": elevation_min,
        "Elevation Max [deg.]": elevation_max,
        "Active Frame Time [ms]": active_frame_time,
        "Burst Periodicity [us]": burst_periodicity,
        "Range CFAR": range_cfar,
        "Doppler CFAR": doppler_cfar,
        "Range Limits": range_limits,
        "Doppler Limits": doppler_limits,
        "Cascading": cascading,
    }

    # remove Nones/empties for cleaner UI
    return {k: v for k, v in metrics.items() if v is not None and v != []}
