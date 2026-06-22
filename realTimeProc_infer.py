import argparse
import json
import multiprocessing
import select
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

# Number of int16 values per radar frame (must match RadarConfig in insect_radar_processor.py)
# 1 TX × 4 RX × 128 ADC samples × 128 loops × 2 (I+Q) = 131 072
_INT16_PER_FRAME = 131_072


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


def _recv_control_messages(
    sock: socket.socket, buffer: bytes
) -> tuple[bytes, List[Dict[str, Any]]]:
    """Read newline-delimited control JSON messages from the FastAPI socket."""
    messages: List[Dict[str, Any]] = []
    chunk = sock.recv(4096)
    if not chunk:
        raise ConnectionError("control socket closed")

    buffer += chunk
    while b"\n" in buffer:
        line, buffer = buffer.split(b"\n", 1)
        payload = line.strip()
        if not payload:
            continue

        try:
            messages.append(json.loads(payload.decode("utf-8", errors="replace")))
        except Exception:
            messages.append(
                {
                    "cmd": "unknown",
                    "raw": payload.decode("utf-8", errors="replace"),
                }
            )

    return buffer, messages


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


def _frame_int16_from_cfg(cfg_path: str) -> Optional[int]:
    """Derive int16 values per radar frame from an xWR .cfg file.

    frame_int16 = numLoops * chirpsPerLoop * numRx * numAdcSamples * 2 (I+Q)

    Returns None when the cfg cannot be parsed, so the caller can fall back to
    the model's expected geometry instead of failing on a parsing hiccup.
    """
    path = Path(_resolve_repo_path(cfg_path, default_dir="configFiles"))
    if not path.exists():
        return None

    rx_mask = tx_mask = samples = None
    chirp_start = chirp_end = loops = None
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as cfg_file:
            for raw_line in cfg_file:
                line = raw_line.strip()
                if not line or line.startswith(("%", "#")):
                    continue
                parts = line.split()
                cmd = parts[0]
                if cmd == "channelCfg" and len(parts) >= 3:
                    rx_mask = int(float(parts[1]))
                    tx_mask = int(float(parts[2]))
                elif cmd == "profileCfg" and len(parts) >= 11:
                    samples = int(float(parts[10]))
                elif cmd == "frameCfg" and len(parts) >= 4:
                    chirp_start = int(float(parts[1]))
                    chirp_end = int(float(parts[2]))
                    loops = int(float(parts[3]))
    except Exception:
        return None

    if None in (rx_mask, samples, chirp_start, chirp_end, loops):
        return None

    num_rx = bin(int(rx_mask)).count("1")
    chirps_per_loop = int(chirp_end) - int(chirp_start) + 1
    if num_rx <= 0 or chirps_per_loop <= 0 or samples <= 0 or loops <= 0:
        return None
    return int(loops) * chirps_per_loop * num_rx * int(samples) * 2


def _build_range_plot(result: Dict[str, Any], range_resolution_m: float) -> Optional[Dict[str, Any]]:
    """Build the web-compatible range_plot dict from InsectRadarProcessor viz.

    Reuses the Range-Time Map (rtm_db, shape [n_frames, n_range_bins]) the
    processor already computed. The 1-D range profile is the mean over the
    frames in the batch. Schema matches what app.js / the PNG endpoints expect:
    range_profile (1-D), range_time (2-D), min_db/max_db, frame_count, range_bins.
    """
    viz = result.get("viz") or {}
    rtm = viz.get("rtm_db")
    if rtm is None:
        return None

    rtm = np.asarray(rtm, dtype=np.float32)
    if rtm.ndim != 2 or rtm.size == 0:
        return None

    n_frames, n_bins = rtm.shape
    profile = rtm.mean(axis=0)
    finite = rtm[np.isfinite(rtm)]
    min_db = float(np.min(finite)) if finite.size else 0.0
    max_db = float(np.max(finite)) if finite.size else 1.0

    def _round_row(row: Any) -> List[float]:
        return [round(float(v), 3) for v in np.asarray(row).ravel().tolist()]

    bin_min = viz.get("range_bin_min")
    bin_max = viz.get("range_bin_max")
    return {
        "ready": True,
        "frame_count": int(n_frames),
        "range_bins": int(n_bins),
        "bin": list(range(int(n_bins))),
        "range_profile": _round_row(profile),
        "range_time": [_round_row(row) for row in rtm.tolist()],
        "min_db": round(min_db, 3),
        "max_db": round(max_db, 3),
        "range_resolution_m": round(float(range_resolution_m), 6),
        "range_bin_min": int(bin_min) if bin_min is not None else None,
        "range_bin_max": int(bin_max) if bin_max is not None else None,
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
                preprocess_queue.put((seq, time.time(), raw_adc_data))
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
    plot_queue: Optional["DropOldestQueue"] = None,
) -> None:
    """Process 2: DSP pipeline — Range-FFT, STFT, feature extraction via InsectRadarProcessor."""
    import_dependencies()
    from insect_radar_processor.insect_radar_processor import InsectRadarProcessor

    print("[+] Preprocessing Process initiated.", flush=True)

    n_frames = int(args_dict["numframes"])
    expected_int16 = n_frames * _INT16_PER_FRAME
    if n_frames != 30:
        print(
            f"[!] WARNING: numframes={n_frames}. InsectRadarProcessor expects 30 frames. "
            "Pass --numframes 30 for correct results.",
            flush=True,
        )

    # F2: fail fast with a clear message when the selected cfg does not match the
    # geometry the model was trained on, instead of silently skipping every batch.
    cfg_frame = _frame_int16_from_cfg(str(args_dict.get("cfg_path", "")))
    if cfg_frame is not None and cfg_frame != _INT16_PER_FRAME:
        print(
            f"[-] FATAL: cfg implies {cfg_frame} int16/frame, but the trained "
            f"InsectRadarProcessor model requires {_INT16_PER_FRAME} "
            "(128 ADC samples x 128 loops x 1 TX x 4 RX). "
            "Select a matching cfg. Stopping pipeline.",
            flush=True,
        )
        exit_event.set()
        return

    # The live DCA1000 stream delivers samples in QQII order, whereas the model
    # was trained on IIQQ-ordered files. Decoding the realtime bytes as QQII
    # recovers the same physical I+jQ signal the IIQQ-trained model expects.
    processor = InsectRadarProcessor(
        range_bin_min=int(args_dict.get("range_bin_min", 15)),
        range_bin_max=int(args_dict.get("range_bin_max", 20)),
        iq_order="QQII",
    )

    try:
        while not exit_event.is_set():
            try:
                seq, ts, raw_data = preprocess_queue.get(block=True, timeout=1.0)
            except Empty:
                continue
            except Exception as exc:
                if not exit_event.is_set():
                    print(f"[-] Preprocessing queue read failure: {exc}", flush=True)
                continue

            try:
                # Validate size before processing.
                actual = int(getattr(raw_data, "size", 0) or 0)
                if actual != expected_int16:
                    print(
                        f"[!] seq={seq}: expected {expected_int16} int16 values, "
                        f"got {actual}. Skipping.",
                        flush=True,
                    )
                    continue

                # F5: feed the raw int16 array straight into the DSP pipeline,
                # no temp-file round-trip on disk.
                result = processor.process_array(raw_data)

                # Build the compact range_plot for the UI from the processor's viz,
                # then drop the heavy viz arrays (spectrogram etc.) before queueing.
                range_plot = _build_range_plot(result, processor.cfg.range_resolution)
                proc_result = {
                    "is_insect":       result["is_insect"],
                    "power_threshold": result["power_threshold"],
                    "features":        result["features"],   # None when background
                    "reason":          result.get("reason"), # None when insect
                    "range_plot":      range_plot,
                }

                ai_queue.put((seq, ts, proc_result))

                # Feed the local matplotlib viewer with the raw Range-Time Map
                # (same data as the web), if the local view is enabled.
                if plot_queue is not None:
                    try:
                        rtm = np.asarray(result["viz"]["rtm_db"], dtype=np.float32)
                        plot_queue.put((int(seq), rtm, bool(result["is_insect"]),
                                        float(result["power_threshold"])))
                    except Exception:
                        pass

            except Exception as exc:
                print(f"[-] Preprocessing failure at seq={seq}: {exc}", flush=True)
                traceback.print_exc()

    finally:
        print("[+] Preprocessing Process terminated.", flush=True)


def ai_worker_process(
    exit_event: multiprocessing.Event,
    ai_queue: DropOldestQueue,
    args_dict: Dict[str, Any],
) -> None:
    """Process 3: run model inference on extracted features and stream JSON Lines to FastAPI."""
    import_dependencies()
    import joblib

    print("[+] AI Worker Process initiated.", flush=True)

    # Load model and feature list once — never reload inside the loop.
    models_dir = Path(__file__).resolve().parent / "insect_radar_processor" / "models"
    model_name = str(args_dict.get("model", "svm"))
    model_files = {"svm": "svm_pipeline.pkl", "rf": "randomforest.pkl", "xgb": "xgboost.pkl"}

    try:
        model = joblib.load(models_dir / model_files[model_name])
        feature_names = joblib.load(models_dir / "feature_names.pkl")
        print(f"[+] Model '{model_name}' loaded ({len(feature_names)} features).", flush=True)
    except Exception as exc:
        print(f"[-] Failed to load model '{model_name}': {exc}", flush=True)
        exit_event.set()
        return

    sock = None
    recv_buffer = b""
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
            readable, _, _ = select.select([sock], [], [], 0)
            if readable:
                try:
                    recv_buffer, messages = _recv_control_messages(sock, recv_buffer)
                except Exception as exc:
                    print(
                        f"[!] Control socket closed or invalid: {exc}. Initiating teardown...",
                        flush=True,
                    )
                    exit_event.set()
                    break

                for message in messages:
                    if str(message.get("cmd", "")).lower() == "stop":
                        print("[*] Stop command received from FastAPI controller.", flush=True)
                        try:
                            _send_json_line(
                                sock,
                                {"type": "status", "event": "stopping", "ts": time.time()},
                            )
                        except Exception:
                            pass
                        exit_event.set()
                        break

            if exit_event.is_set():
                break

            try:
                seq, ts, proc_result = ai_queue.get(block=True, timeout=1.0)
            except Empty:
                continue
            except Exception as exc:
                if not exit_event.is_set():
                    print(f"[-] AI queue read failure: {exc}", flush=True)
                continue

            # ── Inference ────────────────────────────────────────────────────
            try:
                if not proc_result["is_insect"]:
                    ai_result = {
                        "label":  "background",
                        "score":  None,
                        "proba":  None,
                        "reason": proc_result["reason"],
                    }
                else:
                    features = proc_result["features"]
                    X = np.array(
                        [features[n] for n in feature_names],
                        dtype=np.float64,
                    ).reshape(1, -1)

                    raw_pred = model.predict(X)[0]

                    if hasattr(model, "_label_encoder"):
                        pred = str(model._label_encoder.inverse_transform([raw_pred])[0])
                        classes = model._label_encoder.classes_
                    else:
                        pred = str(raw_pred)
                        classes = model.classes_

                    proba = None
                    if hasattr(model, "predict_proba"):
                        p = model.predict_proba(X)[0]
                        proba = {str(c): float(v) for c, v in zip(classes, p)}

                    ai_result = {
                        "label": pred,
                        "score": float(max(proba.values())) if proba else None,
                        "proba": proba,
                    }

            except Exception as exc:
                print(f"[-] Inference failure at seq={seq}: {exc}", flush=True)
                traceback.print_exc()
                ai_result = {"label": "error", "score": None, "proba": None}

            # ── Build and send payload ────────────────────────────────────────
            payload = {
                "type":            "inference",
                "mode":            "hardware",
                "seq":             int(seq),
                "ts":              float(ts),
                "com_port":        args_dict["com_port"],
                "cfg_path":        args_dict["cfg_path"],
                "is_insect":       proc_result["is_insect"],
                "power_threshold": proc_result["power_threshold"],
                "result":          ai_result,
                "range_plot":      proc_result.get("range_plot"),
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


def plot_worker_process(
    exit_event: multiprocessing.Event,
    plot_queue: "DropOldestQueue",
    args_dict: Dict[str, Any],
) -> None:
    """Optional Process 4: live local matplotlib view (range-time waterfall +
    range-profile) drawn from the SAME Range-Time Map the web stream uses.

    Runs in its own process so the GUI event loop never blocks the DSP/AI path.
    Any failure (e.g. no display) is non-fatal: it just disables the local view
    without tearing down the pipeline.
    """
    try:
        import numpy as np_local
        import matplotlib
        import matplotlib.pyplot as plt
        from matplotlib.animation import FuncAnimation
        from insect_radar_processor.insect_radar_processor import RadarConfig
    except Exception as exc:
        print(f"[!] Local view disabled (import failed: {exc}).", flush=True)
        return

    try:
        cfg = RadarConfig()
        range_res = float(cfg.range_resolution)
        n_bins = int(cfg.n_range_bins_keep)
        history = max(int(args_dict.get("view_history", 300)), n_bins)
        max_range = n_bins * range_res

        buf = np_local.full((n_bins, history), np_local.nan, dtype=np_local.float32)
        range_axis = np_local.arange(n_bins) * range_res

        plt.style.use("dark_background")
        fig, (ax_wf, ax_prof) = plt.subplots(2, 1, figsize=(9, 6))
        try:
            fig.canvas.manager.set_window_title("pyRadar — live range-time / range-profile")
        except Exception:
            pass
        im = ax_wf.imshow(buf, aspect="auto", origin="lower", cmap="viridis",
                          extent=[0, history, 0, max_range], vmin=0, vmax=1,
                          interpolation="nearest")
        ax_wf.set_ylabel("Range (m)")
        ax_wf.set_xlabel("Time (frames)")
        fig.colorbar(im, ax=ax_wf, label="dB")
        (line,) = ax_prof.plot(range_axis, np_local.zeros(n_bins), color="#38bdf8", lw=1.8)
        (peak_pt,) = ax_prof.plot([], [], "o", color="#fde047", ms=7)
        ax_prof.set_xlim(0, max_range)
        ax_prof.set_xlabel("Range (m)")
        ax_prof.set_ylabel("Amplitude (dB)")
        ax_prof.grid(True, alpha=0.3)
    except Exception as exc:
        print(f"[!] Local view disabled (figure init failed: {exc}).", flush=True)
        return

    print("[+] Local view window opened.", flush=True)

    def update(_frame):
        if exit_event.is_set():
            plt.close("all")
            return
        latest = None
        while True:
            try:
                latest = plot_queue.get(block=False)
            except Empty:
                break
            except Exception:
                break
        if latest is None:
            return
        seq, rtm, is_insect, power = latest
        rtm = np_local.asarray(rtm, dtype=np_local.float32)
        k = rtm.shape[0]
        nonlocal buf
        buf = np_local.roll(buf, -k, axis=1)
        buf[:, -k:] = rtm.T
        finite = buf[np_local.isfinite(buf)]
        if finite.size >= 16:
            vmin = float(np_local.percentile(finite, 5))
            vmax = float(np_local.percentile(finite, 99))
            im.set_clim(vmin, vmax)
        im.set_data(buf)
        profile = np_local.nanmean(rtm, axis=0)
        line.set_ydata(profile)
        ax_prof.set_ylim(float(np_local.min(profile)) - 1, float(np_local.max(profile)) + 1)
        pk = int(np_local.argmax(profile))
        peak_pt.set_data([range_axis[pk]], [profile[pk]])
        label = "insect" if is_insect else "background"
        ax_wf.set_title(f"seq={seq}  {label}  power={power:,.0f}  peak={range_axis[pk]:.2f} m",
                        fontsize=10)

    try:
        _anim = FuncAnimation(fig, update, interval=50, blit=False, cache_frame_data=False)
        plt.show()
    except Exception as exc:
        print(f"[!] Local view closed: {exc}", flush=True)
    finally:
        print("[+] Local view process terminated.", flush=True)


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
    # 0 = no artificial pacing; the client coalesces draws via requestAnimationFrame,
    # so a server-side sleep only adds latency and backlog.
    parser.add_argument("--interval", type=float, default=0.0)
    parser.add_argument("--preprocess-queue-size", type=int, default=10)
    parser.add_argument("--ai-queue-size", type=int, default=10)
    parser.add_argument("--model", type=str, default="svm",
                        choices=["svm", "rf", "xgb"],
                        help="Inference model: svm | rf | xgb  (default: svm)")
    parser.add_argument("--range-bin-min", type=int, default=15,
                        help="range_bin_min for InsectRadarProcessor (default: 15)")
    parser.add_argument("--range-bin-max", type=int, default=20,
                        help="range_bin_max for InsectRadarProcessor (default: 20)")
    parser.add_argument("--local-view", action=argparse.BooleanOptionalAction, default=True,
                        help="open a local matplotlib range-time/range-profile window alongside the web stream")
    parser.add_argument("--view-history", type=int, default=300,
                        help="local waterfall width in frames (default: 300)")

    args = parser.parse_args(argv)
    args_dict = vars(args)
    args_dict["com_port"] = (args_dict["com_port"] or "").strip()
    args_dict["cfg_path"] = _resolve_repo_path(str(args_dict["cfg_path"]), default_dir="configFiles")
    args_dict["dca_cfg"] = _resolve_repo_path(str(args_dict["dca_cfg"]), default_dir="configFiles")

    multiprocessing.freeze_support()

    exit_event = multiprocessing.Event()
    preprocess_queue = DropOldestQueue(maxsize=max(1, int(args.preprocess_queue_size)))
    ai_queue = DropOldestQueue(maxsize=max(1, int(args.ai_queue_size)))
    plot_queue = DropOldestQueue(maxsize=2) if args.local_view else None

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
        args=(exit_event, preprocess_queue, ai_queue, args_dict, plot_queue),
        name="DSPPreprocessingProcess",
    )
    ai_proc = multiprocessing.Process(
        target=ai_worker_process,
        args=(exit_event, ai_queue, args_dict),
        name="InferenceProcess",
    )
    processes = [capture_proc, preprocess_proc, ai_proc]

    if args.local_view and plot_queue is not None:
        plot_proc = multiprocessing.Process(
            target=plot_worker_process,
            args=(exit_event, plot_queue, args_dict),
            name="LocalViewProcess",
        )
        processes.append(plot_proc)

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
    if plot_queue is not None:
        plot_queue.close()

    print("[*] Pipeline system fully stopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
