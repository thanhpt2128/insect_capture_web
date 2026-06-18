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
    """Process 2: compute DSP/statistical features from raw ADC arrays."""
    import_dependencies()
    print("[+] Preprocessing Process initiated.", flush=True)

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
                "type": "inference",
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
    parser.add_argument("--numframes", type=int, default=2)
    parser.add_argument("--frame-num-in-buf", type=int, default=128)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--preprocess-queue-size", type=int, default=10)
    parser.add_argument("--ai-queue-size", type=int, default=10)

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
