import argparse
import json
import multiprocessing
import os
import select
import signal
import socket
import sys
import tempfile
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
) -> None:
    """Process 2: DSP pipeline — Range-FFT, STFT, feature extraction via InsectRadarProcessor."""
    import_dependencies()
    from insect_radar_processor import InsectRadarProcessor

    print("[+] Preprocessing Process initiated.", flush=True)

    n_frames = int(args_dict["numframes"])
    expected_int16 = n_frames * _INT16_PER_FRAME
    if n_frames != 30:
        print(
            f"[!] WARNING: numframes={n_frames}. InsectRadarProcessor expects 30 frames. "
            "Pass --numframes 30 for correct results.",
            flush=True,
        )

    processor = InsectRadarProcessor(
        range_bin_min=int(args_dict.get("range_bin_min", 15)),
        range_bin_max=int(args_dict.get("range_bin_max", 20)),
    )

    # Reuse one temp file — overwrite each iteration to avoid repeated OS allocation.
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".raw")
    os.close(tmp_fd)

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

                # Write to temp file (InsectRadarProcessor reads from disk).
                np.asarray(raw_data, dtype=np.int16).tofile(tmp_path)

                # Full DSP pipeline: Range-FFT → STFT → power check → features.
                result = processor.process(tmp_path)

                # Strip viz (heavy numpy arrays) — not needed downstream.
                proc_result = {
                    "is_insect":       result["is_insect"],
                    "power_threshold": result["power_threshold"],
                    "features":        result["features"],   # None when background
                    "reason":          result.get("reason"), # None when insect
                }

                ai_queue.put((seq, ts, proc_result))

            except Exception as exc:
                print(f"[-] Preprocessing failure at seq={seq}: {exc}", flush=True)
                traceback.print_exc()

    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
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
    models_dir = Path(__file__).resolve().parent / "models"
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
    parser.add_argument("--numframes", type=int, default=2)
    parser.add_argument("--frame-num-in-buf", type=int, default=128)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--preprocess-queue-size", type=int, default=10)
    parser.add_argument("--ai-queue-size", type=int, default=10)
    parser.add_argument("--model", type=str, default="svm",
                        choices=["svm", "rf", "xgb"],
                        help="Inference model: svm | rf | xgb  (default: svm)")
    parser.add_argument("--range-bin-min", type=int, default=15,
                        help="range_bin_min for InsectRadarProcessor (default: 15)")
    parser.add_argument("--range-bin-max", type=int, default=20,
                        help="range_bin_max for InsectRadarProcessor (default: 20)")

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
