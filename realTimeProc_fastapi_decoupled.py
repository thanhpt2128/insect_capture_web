import argparse
import json
import multiprocessing
import signal
import socket
import sys
import time
import traceback
from pathlib import Path
from queue import Empty, Full
from typing import Any, Dict, List, Optional


# Import these inside worker processes. This keeps Windows spawn from trying to
# serialize hardware/C++ handles created at module import time.
np = None
DCA1000 = None
TI = None


def import_dependencies() -> None:
    """Import scientific and hardware packages within the process context."""
    global np, DCA1000, TI

    import numpy as as_np

    np = as_np
    try:
        from mmwave.dataloader import DCA1000 as DCA_Class
        from mmwave.dataloader.radars import TI as TI_Class
    except ImportError as exc:
        print(
            f"[-] Hardware library import error: {exc}. Check mmwave package installations.",
            flush=True,
        )
        raise

    DCA1000 = DCA_Class
    TI = TI_Class


def _to_builtin(obj: Any) -> Any:
    """Recursively convert numpy values and containers into JSON-safe objects."""
    if obj is None or isinstance(obj, (int, float, str, bool)):
        return obj

    item = getattr(obj, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:
            pass

    if isinstance(obj, dict):
        return {str(k): _to_builtin(v) for k, v in obj.items()}

    if isinstance(obj, (list, tuple)):
        return [_to_builtin(x) for x in obj]

    tolist = getattr(obj, "tolist", None)
    if callable(tolist):
        try:
            return _to_builtin(tolist())
        except Exception:
            pass

    return str(obj)


def _send_json_line(sock: socket.socket, data: Dict[str, Any]) -> None:
    """Serialize and transmit newline-delimited JSON over the socket."""
    line = json.dumps(_to_builtin(data), ensure_ascii=False) + "\n"
    sock.sendall(line.encode("utf-8"))


def _resolve_repo_path(path_raw: str, default_dir: str = "") -> str:
    """Accept absolute paths, repo-relative paths, or names under a default folder."""
    path_raw = (path_raw or "").strip().strip("\"'")
    if not path_raw:
        return ""

    path = Path(path_raw)
    if path.is_absolute():
        return str(path)

    repo_root = Path(__file__).resolve().parent
    candidates = []
    if default_dir:
        candidates.append((repo_root / default_dir / path).resolve())
    candidates.append((repo_root / path).resolve())

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return str(candidates[0])


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _parse_cli_radar_config(path_raw: str) -> Dict[str, Any]:
    """Read the xWR CLI fields needed for range-time DSP metadata."""
    path = Path(_resolve_repo_path(path_raw, default_dir="configFiles"))
    metadata: Dict[str, Any] = {}
    if not path.exists():
        return metadata

    rx_mask: Optional[int] = None
    tx_mask: Optional[int] = None
    samples: Optional[int] = None
    sample_rate_ksps: Optional[int] = None
    freq_slope_mhz_us: Optional[float] = None
    chirp_start: Optional[int] = None
    chirp_end: Optional[int] = None
    frame_loops: Optional[int] = None
    frame_period_ms: Optional[float] = None

    with path.open("r", encoding="utf-8", errors="ignore") as cfg_file:
        for raw_line in cfg_file:
            line = raw_line.strip()
            if not line or line.startswith(("%", "#")):
                continue

            parts = line.split()
            cmd = parts[0]
            try:
                if cmd == "channelCfg" and len(parts) >= 3:
                    rx_mask = int(float(parts[1]))
                    tx_mask = int(float(parts[2]))
                elif cmd == "profileCfg" and len(parts) >= 12:
                    freq_slope_mhz_us = float(parts[8])
                    samples = int(float(parts[10]))
                    sample_rate_ksps = int(float(parts[11]))
                elif cmd == "frameCfg" and len(parts) >= 6:
                    chirp_start = int(float(parts[1]))
                    chirp_end = int(float(parts[2]))
                    frame_loops = int(float(parts[3]))
                    frame_period_ms = float(parts[5])
            except ValueError:
                continue

    if rx_mask is not None:
        metadata["rx"] = int(rx_mask).bit_count()
    if tx_mask is not None:
        metadata["tx"] = int(tx_mask).bit_count()
    if samples is not None:
        metadata["samples"] = samples
    if frame_loops is not None:
        metadata["chirps"] = frame_loops
    if sample_rate_ksps is not None:
        metadata["sample_rate_ksps"] = sample_rate_ksps
    if freq_slope_mhz_us is not None:
        metadata["freq_slope_mhz_us"] = freq_slope_mhz_us
    if frame_period_ms is not None:
        metadata["frame_period_ms"] = frame_period_ms
    if chirp_start is not None and chirp_end is not None:
        metadata["chirps_per_loop"] = int(chirp_end - chirp_start + 1)

    return metadata


def _normalize_adc_params(adc_params: Any, cfg_path: str) -> Dict[str, Any]:
    params = _parse_cli_radar_config(cfg_path)
    if isinstance(adc_params, dict):
        for key, value in adc_params.items():
            params[str(key)] = _to_builtin(value)

    params["chirps"] = _positive_int(params.get("chirps"), 128)
    params["tx"] = _positive_int(params.get("tx"), 1)
    params["rx"] = _positive_int(params.get("rx"), 4)
    params["samples"] = _positive_int(params.get("samples"), 128)
    params["IQ"] = _positive_int(params.get("IQ"), 2)
    params["bytes"] = _positive_int(params.get("bytes"), 2)
    return params


def _range_resolution_m(params: Dict[str, Any]) -> Optional[float]:
    sample_rate_ksps = params.get("sample_rate_ksps")
    freq_slope_mhz_us = params.get("freq_slope_mhz_us")
    samples = params.get("samples")
    if sample_rate_ksps is None or freq_slope_mhz_us is None or samples is None:
        return None

    adc_sample_period_us = 1000.0 / float(sample_rate_ksps) * float(samples)
    bandwidth_hz = float(freq_slope_mhz_us) * adc_sample_period_us * 1e6
    if bandwidth_hz <= 0:
        return None
    return 3e8 / (2.0 * bandwidth_hz)


def _frame_int16_count(params: Dict[str, Any]) -> int:
    return (
        int(params["chirps"])
        * int(params["tx"])
        * int(params["rx"])
        * int(params["samples"])
        * int(params["IQ"])
    )


def _raw_to_complex(raw_data: Any, swap: int = 1) -> Any:
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


def _process_to_cube(
    complex_data: Any,
    num_tx: int,
    num_rx: int,
    num_samples_per_chirp: int,
    num_loops_per_frame: int,
) -> Any:
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


def _raw_frame_to_radar_cube(raw_frame: Any, params: Dict[str, Any]) -> Any:
    expected = _frame_int16_count(params)
    if raw_frame.size != expected:
        raise ValueError(f"expected {expected} int16 values per frame, got {raw_frame.size}")

    complex_data = _raw_to_complex(raw_frame, swap=1)
    cube = _process_to_cube(
        complex_data,
        int(params["tx"]),
        int(params["rx"]),
        int(params["samples"]),
        int(params["chirps"]),
    )
    if cube.shape[0] != 1:
        raise ValueError(f"expected one decoded frame, got cube shape {cube.shape}")
    return cube[0]


def _static_clutter_removal_iir(radar_cube: Any, alpha: float = 0.95) -> Any:
    if not 0.0 <= alpha < 1.0:
        raise ValueError("clutter alpha must be in the range [0, 1)")

    corrected = np.zeros_like(radar_cube)
    clutter = radar_cube[:, :, 0:1]
    for chirp_idx in range(radar_cube.shape[2]):
        current = radar_cube[:, :, chirp_idx : chirp_idx + 1]
        clutter = alpha * clutter + (1.0 - alpha) * current
        corrected[:, :, chirp_idx : chirp_idx + 1] = current - clutter
    return corrected


def _compute_range_profile(
    radar_cube_frame: Any,
    fft_size: int,
    range_bins: int,
    clutter_removal: bool,
    clutter_alpha: float,
) -> Any:
    if fft_size < radar_cube_frame.shape[-1]:
        raise ValueError(
            f"fft_size={fft_size} must be >= numAdcSamples={radar_cube_frame.shape[-1]}"
        )

    radar_cube = radar_cube_frame[np.newaxis, ...]
    if clutter_removal:
        radar_cube = _static_clutter_removal_iir(radar_cube, alpha=clutter_alpha)

    window = np.hanning(radar_cube.shape[3]).astype(np.float32)
    range_fft = np.fft.fft(radar_cube * window, n=fft_size, axis=3)
    range_fft = range_fft[..., :range_bins]
    magnitude_all = np.abs(range_fft)
    rtm = np.mean(magnitude_all, axis=(1, 2))
    profile = 20.0 * np.log10(rtm[0] + 1e-6)
    return profile.astype(np.float32)


def _round_series(values: Any, digits: int = 3) -> List[float]:
    return [round(float(value), digits) for value in values.tolist()]


def _build_range_plot(
    raw_data: Any,
    adc_params: Any,
    args_dict: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    params = _normalize_adc_params(adc_params, str(args_dict.get("cfg_path", "")))
    requested_frames = _positive_int(args_dict.get("numframes"), 30)
    frame_len = _frame_int16_count(params)
    if frame_len <= 0:
        return None

    total_elements = int(getattr(raw_data, "size", 0) or 0)
    complete_frames = min(requested_frames, total_elements // frame_len)
    if complete_frames < requested_frames:
        return {
            "ready": False,
            "requested_frames": requested_frames,
            "complete_frames": complete_frames,
            "frame_int16_count": frame_len,
            "raw_int16_count": total_elements,
            "error": "not enough complete frames for range-time plot",
        }

    fft_size = _positive_int(args_dict.get("range_fft_size"), int(params["samples"]))
    range_bins = _positive_int(args_dict.get("range_bins"), fft_size // 2)
    range_bins = min(range_bins, fft_size)
    clutter_removal = bool(args_dict.get("range_clutter_removal", True))
    clutter_alpha = _positive_float(args_dict.get("range_clutter_alpha"), 0.95)

    frames = raw_data[: complete_frames * frame_len].reshape(complete_frames, frame_len)
    profiles = []
    for raw_frame in frames:
        radar_cube = _raw_frame_to_radar_cube(raw_frame, params)
        profiles.append(
            _compute_range_profile(
                radar_cube,
                fft_size=fft_size,
                range_bins=range_bins,
                clutter_removal=clutter_removal,
                clutter_alpha=clutter_alpha,
            )
        )

    range_time = np.vstack(profiles).astype(np.float32)
    current_profile = range_time[-1]
    finite = range_time[np.isfinite(range_time)]
    min_db = float(np.min(finite)) if finite.size else 0.0
    max_db = float(np.max(finite)) if finite.size else 1.0
    resolution = _range_resolution_m(params)

    return {
        "ready": True,
        "frame_count": int(complete_frames),
        "range_bins": int(range_bins),
        "fft_size": int(fft_size),
        "bin": list(range(int(range_bins))),
        "range_profile": _round_series(current_profile),
        "range_time": [_round_series(row) for row in range_time],
        "min_db": round(min_db, 3),
        "max_db": round(max_db, 3),
        "range_resolution_m": round(float(resolution), 6) if resolution else None,
        "clutter_removal": clutter_removal,
        "clutter_alpha": clutter_alpha,
        "adc_params": _to_builtin(params),
    }


class DropOldestQueue:
    """Bounded multiprocessing queue that drops stale items instead of blocking."""

    def __init__(self, maxsize: int):
        self.queue = multiprocessing.Queue(maxsize=maxsize)
        self.lock = multiprocessing.Lock()

    def put(self, item: Any) -> None:
        with self.lock:
            try:
                self.queue.put(item, block=False)
                return
            except Full:
                pass

            try:
                self.queue.get(block=False)
            except Empty:
                pass

            try:
                self.queue.put(item, block=False)
            except Full:
                pass

    def get(self, block: bool = True, timeout: Optional[float] = None) -> Any:
        return self.queue.get(block=block, timeout=timeout)

    def close(self) -> None:
        try:
            self.queue.close()
            self.queue.join_thread()
        except Exception:
            pass


def capture_worker_process(
    exit_event: multiprocessing.Event,
    preprocess_queue: DropOldestQueue,
    args_dict: Dict[str, Any],
) -> None:
    """Process 1: configure radar/DCA1000 and capture raw ADC frames."""
    import_dependencies()
    print("[+] Capture Process initiated.", flush=True)

    dca = None
    radar = None

    try:
        cfg_path = _resolve_repo_path(str(args_dict["cfg_path"]), default_dir="configFiles")
        dca_json_path = _resolve_repo_path(str(args_dict["dca_cfg"]), default_dir="configFiles")

        print(f"[+] Resolving Configs: Radar={cfg_path}, DCA={dca_json_path}", flush=True)

        dca = DCA1000()
        try:
            dca.reset_radar()
            dca.reset_fpga()
            time.sleep(7.0)
        except Exception as exc:
            print(f"[-] Failed to reset DCA1000: {exc}", flush=True)
            raise

        radar = TI(
            cli_loc=args_dict["com_port"],
            cli_baud=int(args_dict["cli_baud"]),
            data_loc=args_dict["com_port"],
            data_baud=int(args_dict["cli_baud"]),
            config_file=cfg_path,
            verbose=False,
        )

        try:
            radar.setFrameCfg(0)
        except Exception:
            pass

        adc_params, cfg_params = dca.configure(dca_json_path, cfg_path)
        print(
            "[+] DCA Hardware configured. "
            f"ADC params={_to_builtin(dict(adc_params))}; CFG params={_to_builtin(dict(cfg_params))}",
            flush=True,
        )

        dca.stream_start()
        max_buffer_size = max(int(args_dict["frame_num_in_buf"]), int(args_dict["numframes"]))
        dca.fastRead_in_Cpp_thread_start(max_buffer_size)
        radar.startSensor()
        print("[+] Hardware started. Capturing UDP data...", flush=True)

        seq = 0
        while not exit_event.is_set():
            raw_adc_data = dca.fastRead_in_Cpp_thread_get(
                numframes=int(args_dict["numframes"]),
                timeOut=2,
                verbose=False,
                sortInC=True,
            )

            size = int(getattr(raw_adc_data, "size", 0) or 0)
            if raw_adc_data is not None and size > 0:
                preprocess_queue.put((seq, time.time(), raw_adc_data, _to_builtin(dict(adc_params))))
                seq += 1
            else:
                time.sleep(0.001)

    except Exception as exc:
        print(f"[-] Exception occurred in Capture Process: {exc}", flush=True)
        traceback.print_exc()
        exit_event.set()
    finally:
        print("[+] Halting hardware systems and releasing ports...", flush=True)
        if radar is not None:
            try:
                radar.stopSensor()
            except Exception:
                pass
            try:
                radar.close()
            except Exception:
                pass

        if dca is not None:
            try:
                dca.fastRead_in_Cpp_thread_stop()
            except Exception:
                pass
            try:
                dca.stream_stop()
            except Exception:
                pass
            try:
                dca.close()
            except Exception:
                pass

        print("[+] Capture Process terminated cleanly.", flush=True)


def preprocessing_worker_process(
    exit_event: multiprocessing.Event,
    preprocess_queue: DropOldestQueue,
    ai_queue: DropOldestQueue,
    args_dict: Dict[str, Any],
) -> None:
    """Process 2: compute DSP/statistical features from raw ADC arrays."""
    import_dependencies()
    print("[+] Preprocessing Process initiated.", flush=True)

    while not exit_event.is_set():
        try:
            item = preprocess_queue.get(block=True, timeout=1.0)
            if isinstance(item, tuple) and len(item) == 4:
                seq, ts, raw_data, adc_params = item
            else:
                seq, ts, raw_data = item
                adc_params = {}
        except Empty:
            continue
        except Exception as exc:
            if not exit_event.is_set():
                print(f"[-] Preprocessing queue read failure: {exc}", flush=True)
            continue

        try:
            total_elements = int(getattr(raw_data, "size", 0) or 0)
            preview_slice_size = min(64, total_elements)
            stat_slice_size = min(8192, total_elements)

            preview_slice = raw_data[:preview_slice_size].tolist() if preview_slice_size else []
            stats_subset = raw_data[:stat_slice_size] if stat_slice_size else raw_data

            if stat_slice_size:
                sample_mean = float(np.mean(stats_subset))
                sample_std = float(np.std(stats_subset))
                sample_min = int(np.min(stats_subset))
                sample_max = int(np.max(stats_subset))
            else:
                sample_mean = 0.0
                sample_std = 0.0
                sample_min = 0
                sample_max = 0

            features = {
                "numframes": int(args_dict["numframes"]),
                "size": total_elements,
                "dtype": str(getattr(raw_data, "dtype", "")),
                "sample_n": int(stat_slice_size),
                "sample_mean": sample_mean,
                "sample_std": sample_std,
                "sample_min": sample_min,
                "sample_max": sample_max,
                "preview": preview_slice,
            }

            try:
                features["range_plot"] = _build_range_plot(raw_data, adc_params, args_dict)
            except Exception as exc:
                features["range_plot"] = {
                    "ready": False,
                    "error": str(exc),
                }

            ai_queue.put((seq, ts, features))
        except Exception as exc:
            print(f"[-] Preprocessing failure at sequence {seq}: {exc}", flush=True)
            traceback.print_exc()

    print("[+] Preprocessing Process terminated.", flush=True)


def ai_worker_process(
    exit_event: multiprocessing.Event,
    ai_queue: DropOldestQueue,
    args_dict: Dict[str, Any],
) -> None:
    """Process 3: run inference placeholder and stream JSON Lines to FastAPI."""
    import_dependencies()
    print("[+] AI Worker Process initiated.", flush=True)

    sock = None
    try:
        sock = socket.create_connection(
            (args_dict["server_host"], int(args_dict["server_port"])),
            timeout=10,
        )
        print(
            f"[+] TCP Connection verified with {args_dict['server_host']}:{args_dict['server_port']}",
            flush=True,
        )
        _send_json_line(sock, {"type": "status", "event": "ready", "ts": time.time()})
    except Exception as exc:
        print(f"[-] Connection to FastAPI server failed: {exc}", flush=True)
        exit_event.set()
        return

    try:
        while not exit_event.is_set():
            try:
                seq, ts, features = ai_queue.get(block=True, timeout=1.0)
            except Empty:
                continue
            except Exception as exc:
                if not exit_event.is_set():
                    print(f"[-] AI queue read failure: {exc}", flush=True)
                continue

            ai_inference_result = {
                "label": "hardware-capture",
                "score": None,
            }

            payload = {
                "type": "range_dsp" if features.get("range_plot") else "inference",
                "mode": "hardware",
                "seq": int(seq),
                "ts": float(ts),
                "com_port": args_dict["com_port"],
                "cfg_path": args_dict["cfg_path"],
                "capture": {
                    "numframes": features["numframes"],
                    "elapsed_s": float(args_dict["interval"]),
                    "size": features["size"],
                    "dtype": features["dtype"],
                    "sample_n": features["sample_n"],
                    "sample_mean": features["sample_mean"],
                    "sample_std": features["sample_std"],
                    "sample_min": features["sample_min"],
                    "sample_max": features["sample_max"],
                },
                "result": ai_inference_result,
                "range_plot": features.get("range_plot"),
                "preview": features["preview"],
                "note": "Decoupled multi-process architecture active.",
            }

            try:
                _send_json_line(sock, payload)
            except Exception as exc:
                print(
                    f"[!] Send failed (FastAPI disconnected): {exc}. Initiating teardown...",
                    flush=True,
                )
                exit_event.set()
                break

            if float(args_dict["interval"]) > 0:
                time.sleep(float(args_dict["interval"]))

    except Exception as exc:
        print(f"[-] AI Worker Process execution error: {exc}", flush=True)
        traceback.print_exc()
        exit_event.set()
    finally:
        if sock is not None:
            try:
                _send_json_line(sock, {"type": "status", "event": "stopped", "ts": time.time()})
            except Exception:
                pass
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass

        print("[+] AI Worker Process terminated.", flush=True)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="High-throughput decoupled multi-process radar pipeline."
    )
    parser.add_argument("--server-host", type=str, default="127.0.0.1")
    parser.add_argument("--server-port", type=int, required=True)
    parser.add_argument("--com-port", type=str, required=True)
    parser.add_argument("--cli-baud", type=int, default=921600)
    parser.add_argument("--cfg-path", type=str, required=True)
    parser.add_argument("--dca-cfg", type=str, default="cf.json")
    parser.add_argument("--numframes", type=int, default=30)
    parser.add_argument("--frame-num-in-buf", type=int, default=128)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--preprocess-queue-size", type=int, default=10)
    parser.add_argument("--ai-queue-size", type=int, default=10)
    parser.add_argument("--range-fft-size", type=int, default=0)
    parser.add_argument("--range-bins", type=int, default=0)
    parser.add_argument("--range-clutter-alpha", type=float, default=0.95)
    parser.add_argument(
        "--no-range-clutter-removal",
        dest="range_clutter_removal",
        action="store_false",
        default=True,
    )

    args = parser.parse_args(argv)
    args_dict = vars(args)
    args_dict["com_port"] = (args_dict["com_port"] or "").strip()
    args_dict["cfg_path"] = _resolve_repo_path(str(args_dict["cfg_path"]), default_dir="configFiles")
    args_dict["dca_cfg"] = _resolve_repo_path(str(args_dict["dca_cfg"]), default_dir="configFiles")

    multiprocessing.freeze_support()

    exit_event = multiprocessing.Event()
    preprocess_queue = DropOldestQueue(maxsize=max(1, int(args.preprocess_queue_size)))
    ai_queue = DropOldestQueue(maxsize=max(1, int(args.ai_queue_size)))

    def signal_handler(signum: int, _frame: Any) -> None:
        print(
            f"[!] System Signal {signum} caught in main process. "
            "Stopping all subprocesses cleanly...",
            flush=True,
        )
        exit_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal_handler)

    capture_proc = multiprocessing.Process(
        target=capture_worker_process,
        args=(exit_event, preprocess_queue, args_dict),
        name="RadarCaptureProcess",
    )
    preprocess_proc = multiprocessing.Process(
        target=preprocessing_worker_process,
        args=(exit_event, preprocess_queue, ai_queue, args_dict),
        name="DSPPreprocessingProcess",
    )
    ai_proc = multiprocessing.Process(
        target=ai_worker_process,
        args=(exit_event, ai_queue, args_dict),
        name="InferenceProcess",
    )
    processes = [capture_proc, preprocess_proc, ai_proc]

    print("[*] Decoupled multi-process radar pipeline initializing...", flush=True)
    for proc in processes:
        proc.start()

    try:
        while not exit_event.is_set():
            if any(proc.exitcode not in (None, 0) for proc in processes):
                exit_event.set()
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        exit_event.set()

    print("[*] Exit flag set. Synchronizing teardown across all processes...", flush=True)

    for proc in processes:
        proc.join(timeout=3)

    for proc in processes:
        if proc.is_alive():
            print(
                f"[!] Warning: Process {proc.name} failed to terminate cleanly. "
                "Terminating immediately...",
                flush=True,
            )
            proc.terminate()
            proc.join(timeout=3)

    preprocess_queue.close()
    ai_queue.close()

    print("[*] Pipeline system fully stopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
