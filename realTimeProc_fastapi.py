import argparse
import json
import select
import socket
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple


def _send_json_line(sock: socket.socket, obj: Dict[str, object]) -> None:
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    sock.sendall(data)


def _to_builtin(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:
            pass
    return str(value)


def _recv_control_messages(sock: socket.socket, buffer: bytes) -> Tuple[bytes, List[Dict[str, object]]]:
    msgs: List[Dict[str, object]] = []
    try:
        chunk = sock.recv(4096)
    except BlockingIOError:
        return buffer, msgs

    if not chunk:
        raise ConnectionError("control socket closed")

    buffer += chunk
    while b"\n" in buffer:
        line, buffer = buffer.split(b"\n", 1)
        payload = line.strip()
        if not payload:
            continue
        try:
            msgs.append(json.loads(payload.decode("utf-8", errors="replace")))
        except Exception:
            msgs.append({"cmd": "unknown", "raw": payload.decode("utf-8", errors="replace")})

    return buffer, msgs


def _resolve_cfg_path(cfg_path_raw: str) -> str:
    # Keep this worker permissive: accept absolute, or relative from repo root.
    cfg_path_raw = (cfg_path_raw or "").strip().strip("\"'")
    if not cfg_path_raw:
        return ""

    path = Path(cfg_path_raw)
    if path.is_absolute():
        return str(path)

    repo_root = Path(__file__).resolve().parent

    # Prefer resolving under the default config folder (matches FastAPI config resolver).
    candidate = (repo_root / "configFiles" / path).resolve()
    if candidate.exists():
        return str(candidate)

    # Fallback: resolve relative paths from repo root.
    return str((repo_root / path).resolve())


def run_hardware(
    sock: socket.socket,
    com_port: str,
    cfg_path: str,
    cli_baud: int,
    dca_json_path: str,
    numframes: int = 10,
    frame_num_in_buf: int = 128,
    interval: float = 0.0,
) -> None:
    """Capture raw ADC via DCA1000 UDP and stream JSON summaries back to FastAPI.

    NOTE: This emits capture summaries only; DSP/AI integration can be added later.
    """

    recv_buffer = b""

    try:
        import numpy as np
        from mmwave.dataloader import DCA1000
        from mmwave.dataloader.radars import TI
    except Exception as exc:
        _send_json_line(
            sock,
            {
                "type": "status",
                "event": "hardware_import_error",
                "ts": time.time(),
                "error": str(exc),
            },
        )
        raise

    dca = None
    radar = None

    _send_json_line(
        sock,
        {
            "type": "status",
            "event": "hardware_starting",
            "ts": time.time(),
            "com_port": com_port,
            "cfg_path": cfg_path,
            "dca_json_path": dca_json_path,
            "numframes": int(numframes),
            "frame_num_in_buf": int(frame_num_in_buf),
        },
    )

    try:
        dca = DCA1000()
        try:
            dca.reset_radar()
            dca.reset_fpga()
            time.sleep(5)
        except Exception as exc:
            _send_json_line(
                sock,
                {
                    "type": "status",
                    "event": "hardware_reset_failed",
                    "ts": time.time(),
                    "error": str(exc),
                },
            )
            raise

        radar = TI(
            cli_loc=com_port,
            cli_baud=cli_baud,
            data_loc=com_port,
            data_baud=cli_baud,
            config_file=cfg_path,
            verbose=False,
        )

        # 0 means infinite frames (continuous)
        try:
            radar.setFrameCfg(0)
        except Exception:
            pass

        adc_params_l, cfg_params_l = dca.configure(dca_json_path, cfg_path)
        adc_params_json = {str(k): _to_builtin(v) for k, v in dict(adc_params_l).items()}
        cfg_params_json = {str(k): _to_builtin(v) for k, v in dict(cfg_params_l).items()}

        _send_json_line(
            sock,
            {
                "type": "status",
                "event": "hardware_configured",
                "ts": time.time(),
                "adc_params": adc_params_json,
                "cfg_params": cfg_params_json,
            },
        )

        dca.stream_start()
        dca.fastRead_in_Cpp_thread_start(max(int(frame_num_in_buf), int(numframes)))
        radar.startSensor()

        _send_json_line(
            sock,
            {
                "type": "status",
                "event": "hardware_streaming",
                "ts": time.time(),
            },
        )

        seq = 0
        while True:
            # best-effort stop check
            readable, _, _ = select.select([sock], [], [], 0)
            if readable:
                recv_buffer, msgs = _recv_control_messages(sock, recv_buffer)
                for msg in msgs:
                    if str(msg.get("cmd", "")).lower() == "stop":
                        _send_json_line(sock, {"type": "status", "event": "stopping", "ts": time.time()})
                        return

            t0 = time.time()
            data_buf = dca.fastRead_in_Cpp_thread_get(
                numframes=int(numframes),
                timeOut=2,
                verbose=False,
                sortInC=True,
            )
            elapsed = time.time() - t0

            # Keep payload small: compute stats on a small slice.
            size = int(getattr(data_buf, "size", 0) or 0)
            head_n = min(64, size)
            stat_n = min(8192, size)

            head = data_buf[:head_n].tolist() if head_n else []
            sample = data_buf[:stat_n] if stat_n else data_buf

            try:
                sample_mean = float(np.mean(sample)) if stat_n else 0.0
                sample_std = float(np.std(sample)) if stat_n else 0.0
                sample_min = int(np.min(sample)) if stat_n else 0
                sample_max = int(np.max(sample)) if stat_n else 0
            except Exception:
                sample_mean = 0.0
                sample_std = 0.0
                sample_min = 0
                sample_max = 0

            payload = {
                "type": "inference",
                "mode": "hardware",
                "seq": seq,
                "ts": time.time(),
                "com_port": com_port,
                "cfg_path": cfg_path,
                "capture": {
                    "numframes": int(numframes),
                    "elapsed_s": round(float(elapsed), 6),
                    "size": size,
                    "dtype": str(getattr(data_buf, "dtype", "")),
                    "sample_n": int(stat_n),
                    "sample_mean": sample_mean,
                    "sample_std": sample_std,
                    "sample_min": sample_min,
                    "sample_max": sample_max,
                },
                "result": {
                    "label": "hardware-capture",
                    "score": None,
                },
                "preview": head,
                "note": "Hardware capture OK; inference pipeline not yet applied",
            }
            _send_json_line(sock, payload)
            seq += 1

            if interval and interval > 0:
                time.sleep(float(interval))

    finally:
        # Best-effort cleanup
        try:
            if radar is not None:
                radar.stopSensor()
        except Exception:
            pass

        try:
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
        except Exception:
            pass


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="pyRadar real-time worker for FastAPI")
    parser.add_argument("--server-host", default="127.0.0.1")
    parser.add_argument("--server-port", type=int, required=True)
    parser.add_argument("--com-port", required=True)
    parser.add_argument("--cli-baud", type=int, default=921600)
    parser.add_argument("--cfg-path", required=True)
    parser.add_argument("--dca-cfg", default="cf.json", help="DCA1000 json (default: cf.json under configFiles/)")
    parser.add_argument("--numframes", type=int, default=2, help="Frames per fetch in hardware mode")
    parser.add_argument("--frame-num-in-buf", type=int, default=128, help="UDP ring buffer size (frames)")
    parser.add_argument("--interval", type=float, default=0.5)
    args = parser.parse_args(argv)

    com_port = (args.com_port or "").strip()
    cfg_path = _resolve_cfg_path(args.cfg_path)
    dca_json_path = _resolve_cfg_path(args.dca_cfg)

    try:
        sock = socket.create_connection((args.server_host, args.server_port), timeout=10)

        run_hardware(
            sock,
            com_port=com_port,
            cfg_path=cfg_path,
            cli_baud=int(args.cli_baud),
            dca_json_path=dca_json_path,
            numframes=int(args.numframes),
            frame_num_in_buf=int(args.frame_num_in_buf),
            interval=float(args.interval),
        )

        _send_json_line(sock, {"type": "status", "event": "stopped", "ts": time.time()})
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        # Best-effort: print to stdout so FastAPI can capture logs.
        print(f"worker error: {exc}", file=sys.stdout)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
