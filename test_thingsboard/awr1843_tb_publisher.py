"""AWR1843 DCA1000 raw replay/publisher for ThingsBoard range-time views.

The raw parser and range-time DSP flow match
``../dsp_radar/test_ve_pho_raw_data.ipynb`` up to the range-time map:

    raw_to_complex -> process_to_cube -> IIR static-clutter removal
    -> Hanning window -> Range FFT -> half spectrum
    -> mean over antenna/chirp -> range_profile dB

Each frame is published as one compact telemetry message on the configured
ThingsBoard MQTT telemetry topic. The dashboard/widget should keep its own
rolling 2D buffer for the waterfall/range-time display.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator, Optional

import matplotlib.pyplot as plt
import numpy as np
import paho.mqtt.client as mqtt


DEFAULT_BIN = Path(r"E:\DATN\clone_code\pyRadar - Copy\data_parse\nguoidunglenngoixuong.bin")

# Hard-coded from ../dsp_radar/test_ve_pho_raw_data.ipynb.
NOTEBOOK_NUM_TX = 1
NOTEBOOK_NUM_RX = 4
NOTEBOOK_NUM_ADC_SAMPLES = 128
NOTEBOOK_NUM_LOOPS_PER_FRAME = 128
NOTEBOOK_FRAME_PERIOD_MS = 20.0
NOTEBOOK_SAMPLE_RATE_HZ = 4e6
NOTEBOOK_BANDWIDTH_HZ = 2586.24e6
NOTEBOOK_FREQ_SLOPE_HZ_S = 80.820e12
NOTEBOOK_C_M_S = 3e8


@dataclass(frozen=True)
class RadarConfig:
    chirps: int
    tx: int
    rx: int
    samples: int
    iq: int = 2
    bytes_per_sample: int = 2
    sample_rate_ksps: Optional[int] = None
    freq_slope_mhz_us: Optional[float] = None
    bandwidth_hz: Optional[float] = None
    light_speed_m_s: float = NOTEBOOK_C_M_S
    frame_period_ms: Optional[float] = None

    @property
    def frame_int16_count(self) -> int:
        return self.chirps * self.tx * self.rx * self.samples * self.iq

    @property
    def frame_byte_count(self) -> int:
        return self.frame_int16_count * self.bytes_per_sample


@dataclass(frozen=True)
class PublisherConfig:
    host: str = "127.0.0.1"
    port: int = 1883
    access_token: str = "ZbMYuKno0oEfcf57vQ5m"
    topic: str = "v1/devices/me/telemetry"
    bin_path: Path = DEFAULT_BIN
    fft_size: Optional[int] = None
    range_bins: Optional[int] = None
    fps: Optional[float] = None
    repeat_file: bool = False
    qos: int = 0
    max_frames: Optional[int] = None
    dry_run: bool = False
    publish_config_each_frame: bool = True
    clutter_removal: bool = True
    clutter_alpha: float = 0.95
    offline_history: int = 200


def notebook_radar_config() -> RadarConfig:
    """Fixed radar parameters from dsp_radar/test_ve_pho_raw_data.ipynb."""
    return RadarConfig(
        chirps=NOTEBOOK_NUM_LOOPS_PER_FRAME,
        tx=NOTEBOOK_NUM_TX,
        rx=NOTEBOOK_NUM_RX,
        samples=NOTEBOOK_NUM_ADC_SAMPLES,
        sample_rate_ksps=int(NOTEBOOK_SAMPLE_RATE_HZ / 1e3),
        freq_slope_mhz_us=NOTEBOOK_FREQ_SLOPE_HZ_S / 1e12,
        bandwidth_hz=NOTEBOOK_BANDWIDTH_HZ,
        light_speed_m_s=NOTEBOOK_C_M_S,
        frame_period_ms=NOTEBOOK_FRAME_PERIOD_MS,
    )


def parse_radar_cfg(path: Path) -> RadarConfig:
    """Parse the xWR18xx mmWave CLI config fields needed for raw ADC replay."""
    rx_mask: Optional[int] = None
    tx_mask: Optional[int] = None
    samples: Optional[int] = None
    sample_rate_ksps: Optional[int] = None
    freq_slope_mhz_us: Optional[float] = None
    frame_chirp_start: Optional[int] = None
    frame_chirp_end: Optional[int] = None
    frame_loops: Optional[int] = None
    frame_period_ms: Optional[float] = None
    adc_output_is_complex = True
    adcbuf_is_complex = True

    with path.open("r", encoding="utf-8", errors="ignore") as cfg_file:
        for raw_line in cfg_file:
            line = raw_line.strip()
            if not line or line.startswith("%"):
                continue
            parts = line.split()
            cmd = parts[0]

            if cmd == "channelCfg" and len(parts) >= 3:
                rx_mask = int(parts[1])
                tx_mask = int(parts[2])
            elif cmd == "adcCfg" and len(parts) >= 3:
                # adcCfg <numADCBits> <adcOutputFmt>; fmt 1 is complex 1x.
                adc_output_is_complex = int(parts[2]) == 1
            elif cmd == "adcbufCfg" and len(parts) >= 6:
                # adcbufCfg <subFrameIdx> <adcOutputFmt> <sampleSwap>
                #           <chanInterleave> <chirpThreshold>
                adcbuf_is_complex = int(parts[2]) == 0
            elif cmd == "profileCfg" and len(parts) >= 12:
                freq_slope_mhz_us = float(parts[8])
                samples = int(parts[10])
                sample_rate_ksps = int(parts[11])
            elif cmd == "frameCfg" and len(parts) >= 6:
                frame_chirp_start = int(parts[1])
                frame_chirp_end = int(parts[2])
                frame_loops = int(parts[3])
                frame_period_ms = float(parts[5])

    missing = [
        name
        for name, value in (
            ("channelCfg", rx_mask),
            ("profileCfg numAdcSamples", samples),
            ("frameCfg chirpStartIdx", frame_chirp_start),
            ("frameCfg chirpEndIdx", frame_chirp_end),
            ("frameCfg numLoops", frame_loops),
        )
        if value is None
    ]
    if missing:
        raise ValueError(f"Missing required config field(s) in {path}: {', '.join(missing)}")
    if not adc_output_is_complex or not adcbuf_is_complex:
        raise ValueError(
            "This publisher expects complex ADC data from adcCfg/adcbufCfg, "
            "matching the DCA1000 raw flow used by realTimeProc.py."
        )

    rx = int(rx_mask).bit_count()
    tx = int(tx_mask or 0).bit_count()
    chirps_per_loop = int(frame_chirp_end) - int(frame_chirp_start) + 1

    if rx <= 0 or tx <= 0:
        raise ValueError(f"Invalid channelCfg in {path}: rx={rx}, tx={tx}")
    if chirps_per_loop != tx:
        raise ValueError(
            "frameCfg chirp span does not match enabled TX count. "
            f"chirps_per_loop={chirps_per_loop}, tx={tx}. "
            "Update the parser if this capture uses a non-TDM-MIMO chirp layout."
        )
    if int(samples) % 2 != 0:
        raise ValueError("DCA1000 2-lane reshape expects an even numAdcSamples value.")

    return RadarConfig(
        chirps=int(frame_loops),
        tx=tx,
        rx=rx,
        samples=int(samples),
        sample_rate_ksps=sample_rate_ksps,
        freq_slope_mhz_us=freq_slope_mhz_us,
        frame_period_ms=frame_period_ms,
    )


def range_resolution_m(radar_cfg: RadarConfig) -> Optional[float]:
    """Return notebook-style range resolution in meters."""
    if radar_cfg.bandwidth_hz is not None and radar_cfg.bandwidth_hz > 0:
        return radar_cfg.light_speed_m_s / (2.0 * radar_cfg.bandwidth_hz)

    if radar_cfg.sample_rate_ksps is None or radar_cfg.freq_slope_mhz_us is None:
        return None

    adc_sample_period_us = 1000.0 / radar_cfg.sample_rate_ksps * radar_cfg.samples
    bandwidth_hz = radar_cfg.freq_slope_mhz_us * adc_sample_period_us * 1e6
    if bandwidth_hz <= 0:
        return None
    return radar_cfg.light_speed_m_s / (2.0 * bandwidth_hz)


def max_range_m(radar_cfg: RadarConfig) -> Optional[float]:
    """Return notebook-style maximum range in meters."""
    if radar_cfg.sample_rate_ksps is None or radar_cfg.freq_slope_mhz_us is None:
        return None

    sample_rate_hz = radar_cfg.sample_rate_ksps * 1e3
    slope_hz_s = radar_cfg.freq_slope_mhz_us * 1e12
    if slope_hz_s <= 0:
        return None
    return sample_rate_hz * radar_cfg.light_speed_m_s / (2.0 * slope_hz_s)


def read_raw_frames(
    path: Path,
    radar_cfg: RadarConfig,
    repeat_file: bool,
) -> Iterator[np.ndarray]:
    """Yield one raw int16 DCA1000 frame at a time."""
    frame_bytes = radar_cfg.frame_byte_count

    while True:
        with path.open("rb") as raw_file:
            while True:
                raw = raw_file.read(frame_bytes)
                if len(raw) < frame_bytes:
                    if raw:
                        print(
                            f"Skipping trailing partial frame: {len(raw)} of "
                            f"{frame_bytes} bytes"
                        )
                    break
                yield np.frombuffer(raw, dtype=np.int16)

        if not repeat_file:
            return


def raw_to_complex(raw_data: np.ndarray, swap: int = 1) -> np.ndarray:
    """Notebook raw_to_complex(), adapted to accept an int16 array.

    With the notebook default ``swap = 1``:
    I = raw[2, 3], Q = raw[0, 1].
    """
    if raw_data.size % 4 != 0:
        raise ValueError("raw int16 length must be divisible by 4 for IQ unpacking")

    data = raw_data.reshape(-1, 4)
    if swap == 0:
        raw_i = data[:, [0, 1]].flatten()
        raw_q = data[:, [2, 3]].flatten()
    else:
        raw_i = data[:, [2, 3]].flatten()
        raw_q = data[:, [0, 1]].flatten()

    return (raw_i + 1j * raw_q).astype(np.complex64)


def process_to_cube(
    complex_data: np.ndarray,
    num_tx: int,
    num_rx: int,
    num_samples_per_chirp: int,
    num_loops_per_frame: int,
) -> np.ndarray:
    """Notebook process_to_cube().

    Returns [frames, antennas(TX*RX), loops, samples].
    """
    num_chirps = num_tx * num_loops_per_frame
    samples_per_frame = num_chirps * num_rx * num_samples_per_chirp
    valid_len = (len(complex_data) // samples_per_frame) * samples_per_frame
    complex_data = complex_data[:valid_len]

    cube = complex_data.reshape(-1, num_chirps, num_rx, num_samples_per_chirp)
    cube = cube.reshape(-1, num_loops_per_frame, num_tx, num_rx, num_samples_per_chirp)
    cube = cube.transpose(0, 2, 3, 1, 4)
    return cube.reshape(
        cube.shape[0], num_tx * num_rx, num_loops_per_frame, num_samples_per_chirp
    )


def raw_frame_to_radar_cube(raw_frame: np.ndarray, radar_cfg: RadarConfig) -> np.ndarray:
    """Convert one raw frame to notebook cube shape [antennas, loops, samples]."""
    if raw_frame.size != radar_cfg.frame_int16_count:
        raise ValueError(
            f"Expected {radar_cfg.frame_int16_count} int16 values per frame, "
            f"got {raw_frame.size}"
        )

    complex_data = raw_to_complex(raw_frame, swap=1)
    cube = process_to_cube(
        complex_data,
        radar_cfg.tx,
        radar_cfg.rx,
        radar_cfg.samples,
        radar_cfg.chirps,
    )
    if cube.shape[0] != 1:
        raise ValueError(f"Expected one decoded frame, got cube shape {cube.shape}")
    return cube[0]


def static_clutter_removal_iir(
    radar_cube: np.ndarray, alpha: float = 0.95
) -> np.ndarray:
    """Notebook static_clutter_removal_iir().

    Input shape: [frames, antennas, loops, samples].
    """
    if not 0.0 <= alpha < 1.0:
        raise ValueError("clutter alpha must be in the range [0, 1)")

    corrected = np.zeros_like(radar_cube)
    clutter = radar_cube[:, :, 0:1]
    for chirp_idx in range(radar_cube.shape[2]):
        current = radar_cube[:, :, chirp_idx : chirp_idx + 1]
        clutter = alpha * clutter + (1.0 - alpha) * current
        corrected[:, :, chirp_idx : chirp_idx + 1] = current - clutter
    return corrected


def compute_range_profile(
    radar_cube_frame: np.ndarray,
    fft_size: int,
    range_bins: Optional[int] = None,
    clutter_removal: bool = True,
    clutter_alpha: float = 0.95,
) -> np.ndarray:
    """Notebook range-time flow for one frame, returned as one dB profile."""
    if fft_size < radar_cube_frame.shape[-1]:
        raise ValueError(
            f"fft_size={fft_size} must be >= numAdcSamples={radar_cube_frame.shape[-1]}"
        )

    radar_cube = radar_cube_frame[np.newaxis, ...]
    if clutter_removal:
        radar_cube = static_clutter_removal_iir(radar_cube, alpha=clutter_alpha)

    window = np.hanning(radar_cube.shape[3]).astype(np.float32)
    range_fft = np.fft.fft(radar_cube * window, n=fft_size, axis=3)
    range_fft = range_fft[..., : range_bins or (fft_size // 2)]

    magnitude_all = np.abs(range_fft)
    rtm = np.mean(magnitude_all, axis=(1, 2))
    profile = 20.0 * np.log10(rtm[0] + 1e-6)

    return profile.astype(np.float32)


def build_payload(
    frame_id: int,
    range_profile: np.ndarray,
) -> dict:
    bin_indices = list(range(int(range_profile.size)))
    values = {
        "frame_id": frame_id,
        "bin": bin_indices,
        "range_profile": [round(float(v), 3) for v in range_profile.tolist()],
    }

    return {"ts": int(time.time() * 1000), "values": values}


class OfflineRangeViewer:
    """Lightweight local viewer for dry-run range-profile replay."""

    def __init__(self, num_bins: int, history_size: int) -> None:
        self.num_bins = num_bins
        self.history_size = max(10, history_size)
        self.history = np.full((self.history_size, num_bins), np.nan, dtype=np.float32)

        plt.ion()
        self.fig, (self.ax_profile, self.ax_heatmap) = plt.subplots(
            2,
            1,
            figsize=(10, 7),
            gridspec_kw={"height_ratios": [1, 2]},
        )
        self.fig.canvas.manager.set_window_title("AWR1843 Dry Run Viewer")

        bins = np.arange(num_bins, dtype=np.int32)
        self.profile_line, = self.ax_profile.plot(bins, np.zeros(num_bins, dtype=np.float32))
        self.ax_profile.set_title("Current Range Profile")
        self.ax_profile.set_xlabel("Range Bin")
        self.ax_profile.set_ylabel("Amplitude (dB)")
        self.ax_profile.grid(True, alpha=0.3)

        self.heatmap = self.ax_heatmap.imshow(
            self.history.T,
            aspect="auto",
            origin="lower",
            cmap="viridis",
            interpolation="nearest",
        )
        self.ax_heatmap.set_title("Range-Time Waterfall")
        self.ax_heatmap.set_xlabel("Frame History")
        self.ax_heatmap.set_ylabel("Range Bin")
        self.colorbar = self.fig.colorbar(self.heatmap, ax=self.ax_heatmap)
        self.colorbar.set_label("Amplitude (dB)")

        self.fig.tight_layout()
        self.fig.canvas.draw()
        self.fig.canvas.flush_events()

    def update(self, frame_id: int, range_profile: np.ndarray) -> None:
        self.history = np.roll(self.history, -1, axis=0)
        self.history[-1] = range_profile

        self.profile_line.set_ydata(range_profile)
        self.ax_profile.set_xlim(0, max(self.num_bins - 1, 1))

        profile_min = float(np.min(range_profile))
        profile_max = float(np.max(range_profile))
        if profile_min == profile_max:
            profile_min -= 1.0
            profile_max += 1.0
        self.ax_profile.set_ylim(profile_min - 1.0, profile_max + 1.0)

        valid = self.history[np.isfinite(self.history)]
        if valid.size:
            vmin = float(np.min(valid))
            vmax = float(np.max(valid))
            if vmin == vmax:
                vmin -= 1.0
                vmax += 1.0
            self.heatmap.set_clim(vmin=vmin, vmax=vmax)

        self.heatmap.set_data(self.history.T)
        self.ax_profile.set_title(f"Current Range Profile - Frame {frame_id}")
        self.ax_heatmap.set_title(
            f"Range-Time Waterfall - Last {self.history_size} Frames"
        )

        self.fig.canvas.draw_idle()
        self.fig.canvas.flush_events()
        plt.pause(0.001)

    def close(self) -> None:
        plt.ioff()
        plt.close(self.fig)


def connect_mqtt(cfg: PublisherConfig) -> mqtt.Client:
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"awr1843_replay_{int(time.time())}",
    )
    client.username_pw_set(cfg.access_token)
    client.connect(cfg.host, cfg.port, keepalive=60)
    client.loop_start()
    return client


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def parse_args() -> PublisherConfig:
    defaults = PublisherConfig()
    parser = argparse.ArgumentParser(
        description="Replay AWR1843/DCA1000 raw .bin frames to ThingsBoard."
    )
    parser.add_argument("--host", default=defaults.host)
    parser.add_argument("--port", type=int, default=defaults.port)
    parser.add_argument(
        "--access-token",
        default=defaults.access_token,
    )
    parser.add_argument("--topic", default=defaults.topic)
    parser.add_argument("--bin", dest="bin_path", type=Path, default=defaults.bin_path)
    parser.add_argument(
        "--fft-size",
        type=positive_int,
        default=None,
        help="Range FFT size. Defaults to hard-coded num_adc_samples.",
    )
    parser.add_argument(
        "--range-bins",
        type=positive_int,
        default=None,
        help="Only publish the first N range bins after FFT.",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=None,
        help="Replay rate. Defaults to 1000/frameCfg periodicity when available.",
    )
    parser.add_argument("--qos", type=int, choices=(0, 1), default=defaults.qos)
    parser.add_argument("--max-frames", type=positive_int, default=defaults.max_frames)
    repeat_group = parser.add_mutually_exclusive_group()
    repeat_group.add_argument(
        "--repeat",
        dest="repeat_file",
        action="store_true",
        default=defaults.repeat_file,
        help="Replay the raw file again after reaching EOF.",
    )
    repeat_group.add_argument(
        "--no-repeat",
        dest="repeat_file",
        action="store_false",
        help="Stop after the raw file is consumed. This is the default.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--offline-history",
        type=positive_int,
        default=defaults.offline_history,
        help="Dry-run waterfall history length in frames.",
    )
    parser.add_argument(
        "--no-clutter-removal",
        action="store_true",
        help="Disable the notebook-style IIR static clutter removal.",
    )
    parser.add_argument(
        "--clutter-alpha",
        type=float,
        default=defaults.clutter_alpha,
        help="IIR clutter alpha used by the notebook pipeline.",
    )
    parser.add_argument(
        "--config-once",
        action="store_true",
        help="Only include frame config fields in frame_id=0 payload.",
    )
    args = parser.parse_args()

    return PublisherConfig(
        host=args.host,
        port=args.port,
        access_token=args.access_token,
        topic=args.topic,
        bin_path=args.bin_path,
        fft_size=args.fft_size,
        range_bins=args.range_bins,
        fps=args.fps,
        repeat_file=args.repeat_file,
        qos=args.qos,
        max_frames=args.max_frames,
        dry_run=args.dry_run,
        publish_config_each_frame=not args.config_once,
        clutter_removal=not args.no_clutter_removal,
        clutter_alpha=args.clutter_alpha,
        offline_history=args.offline_history,
    )


def validate_paths(cfg: PublisherConfig) -> None:
    if not cfg.bin_path.exists():
        raise FileNotFoundError(f"Missing input file: {cfg.bin_path}")


def resolved_fps(cfg: PublisherConfig, radar_cfg: RadarConfig) -> float:
    if cfg.fps is not None:
        if cfg.fps <= 0:
            raise ValueError("--fps must be positive")
        return cfg.fps
    if radar_cfg.frame_period_ms and radar_cfg.frame_period_ms > 0:
        return 1000.0 / radar_cfg.frame_period_ms
    return 20.0


def print_startup_summary(
    cfg: PublisherConfig,
    radar_cfg: RadarConfig,
    fft_size: int,
    fps: float,
) -> None:
    file_size = cfg.bin_path.stat().st_size
    complete_frames = file_size // radar_cfg.frame_byte_count
    trailing = file_size % radar_cfg.frame_byte_count
    print(
        "AWR1843 replay config: "
        f"{radar_cfg.chirps} chirps x {radar_cfg.tx} TX x {radar_cfg.rx} RX x "
        f"{radar_cfg.samples} samples, frame={radar_cfg.frame_byte_count} bytes"
    )
    print(
        f"Input file: {cfg.bin_path} ({complete_frames} complete frames"
        f"{', trailing bytes=' + str(trailing) if trailing else ''})"
    )
    print(
        "Range FFT: "
        f"fft_size={fft_size}, publish_bins={cfg.range_bins or (fft_size // 2)}, "
        f"clutter_iir={cfg.clutter_removal}, alpha={cfg.clutter_alpha}"
    )
    print(f"Replay: fps={fps:.3f}, repeat_file={cfg.repeat_file}, dry_run={cfg.dry_run}")
    if cfg.dry_run:
        print(
            f"Offline viewer: history={cfg.offline_history} frames, "
            "showing local realtime waterfall"
        )


def main() -> None:
    cfg = parse_args()
    validate_paths(cfg)

    radar_cfg = notebook_radar_config()
    fft_size = cfg.fft_size or radar_cfg.samples
    cfg = replace(cfg, range_bins=cfg.range_bins or (fft_size // 2))
    if cfg.range_bins > fft_size:
        raise ValueError("--range-bins must be <= --fft-size")
    fps = resolved_fps(cfg, radar_cfg)
    frame_period = 1.0 / max(fps, 0.1)
    print_startup_summary(cfg, radar_cfg, fft_size, fps)

    client: Optional[mqtt.Client] = None
    viewer: Optional[OfflineRangeViewer] = None
    if not cfg.dry_run:
        if cfg.access_token == "PUT_DEVICE_ACCESS_TOKEN_HERE":
            raise ValueError("Set --access-token or TB_ACCESS_TOKEN before publishing.")
        client = connect_mqtt(cfg)

    try:
        for frame_id, raw_frame in enumerate(
            read_raw_frames(cfg.bin_path, radar_cfg, cfg.repeat_file)
        ):
            if cfg.max_frames is not None and frame_id >= cfg.max_frames:
                break

            radar_cube_frame = raw_frame_to_radar_cube(raw_frame, radar_cfg)
            profile = compute_range_profile(
                radar_cube_frame,
                fft_size,
                cfg.range_bins,
                clutter_removal=cfg.clutter_removal,
                clutter_alpha=cfg.clutter_alpha,
            )
            payload = build_payload(
                frame_id=frame_id,
                range_profile=profile,
            )

            encoded = json.dumps(payload, separators=(",", ":"))
            if cfg.dry_run:
                if viewer is None:
                    viewer = OfflineRangeViewer(
                        num_bins=profile.size,
                        history_size=cfg.offline_history,
                    )
                    print(
                        "Dry-run viewer started. Close the window or press Ctrl+C to stop."
                    )
                viewer.update(frame_id=frame_id, range_profile=profile)
            else:
                assert client is not None
                result = client.publish(cfg.topic, encoded, qos=cfg.qos)
                if result.rc != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError(f"MQTT publish failed with rc={result.rc}")

            time.sleep(frame_period)
    except KeyboardInterrupt:
        print("Stopped by user.")
    finally:
        if client is not None:
            client.loop_stop()
            client.disconnect()
        if viewer is not None:
            viewer.close()


if __name__ == "__main__":
    main()
