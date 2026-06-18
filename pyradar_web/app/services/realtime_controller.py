import json
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Deque, Dict, List, Optional

from app.core.settings import DEFAULT_REALTIME_WORKER


class RealTimeController:
    def __init__(self, log_max_lines: int = 1000, results_max_items: int = 1000):
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._log_buffer: Deque[Dict[str, object]] = deque(maxlen=log_max_lines)
        self._results_buffer: Deque[Dict[str, object]] = deque(maxlen=results_max_items)
        self._reader_thread: Optional[threading.Thread] = None
        self._socket_thread: Optional[threading.Thread] = None
        self._server_sock: Optional[socket.socket] = None
        self._conn_sock: Optional[socket.socket] = None
        self._conn_lock = threading.Lock()
        self._start_time: Optional[float] = None
        self._last_exit_code: Optional[int] = None
        self._last_params: Dict[str, object] = {}

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(
        self,
        com_port: str,
        cfg_path: str,
        cli_baud: int = 921600,
        numframes: int = 30,
    ) -> Dict[str, object]:
        com_port = (com_port or "").strip()
        cfg_path = (cfg_path or "").strip()
        if not com_port:
            raise ValueError("com_port is required")
        if not cfg_path:
            raise ValueError("cfg_path is required")
        if cli_baud <= 0:
            raise ValueError("cli_baud must be > 0")
        if numframes <= 0:
            raise ValueError("numframes must be > 0")

        worker = Path(DEFAULT_REALTIME_WORKER)
        if not worker.exists():
            raise FileNotFoundError(f"Worker script not found: {worker}")

        with self._lock:
            if self.is_running():
                return self.status()

            self._results_buffer.clear()
            self._log_buffer.clear()
            self._last_params = {
                "com_port": com_port,
                "cfg_path": cfg_path,
                "cli_baud": cli_baud,
                "numframes": numframes,
            }

            # create localhost TCP server for worker -> API
            self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_sock.bind(("127.0.0.1", 0))
            self._server_sock.listen(1)
            host, port = self._server_sock.getsockname()
            self._append_log(f"result socket listening on {host}:{port}")

            self._process = subprocess.Popen(
                [
                    sys.executable,
                    str(worker),
                    "--server-host",
                    str(host),
                    "--server-port",
                    str(port),
                    "--com-port",
                    com_port,
                    "--cli-baud",
                    str(cli_baud),
                    "--cfg-path",
                    cfg_path,
                    "--numframes",
                    str(numframes),
                ],
                cwd=str(worker.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            self._start_time = time.time()
            self._last_exit_code = None

            self._reader_thread = threading.Thread(
                target=self._read_output,
                args=(self._process,),
                daemon=True,
            )
            self._reader_thread.start()

            self._socket_thread = threading.Thread(
                target=self._accept_and_read_results,
                daemon=True,
            )
            self._socket_thread.start()

            self._append_log(f"worker started pid={self._process.pid}")

        return self.status()

    def stop(self, timeout_seconds: int = 5) -> Dict[str, object]:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                self._cleanup_sockets_locked()
                self._process = None
                return self.status()

        # best-effort graceful stop via socket
        self._send_control({"cmd": "stop"})

        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except Exception:
                pass
            process.wait(timeout=timeout_seconds)

        with self._lock:
            self._last_exit_code = process.poll()
            self._process = None
            self._append_log("worker stopped")
            self._cleanup_sockets_locked()

        return self.status()

    def status(self) -> Dict[str, object]:
        process = self._process
        running = process is not None and process.poll() is None
        return {
            "running": running,
            "pid": process.pid if process else None,
            "start_time": self._start_time,
            "exit_code": self._last_exit_code,
            "log_lines": len(self._log_buffer),
            "result_lines": len(self._results_buffer),
            "params": self._last_params,
        }

    def get_logs(self, tail: int = 200) -> List[Dict[str, object]]:
        if tail <= 0:
            return []
        return list(self._log_buffer)[-tail:]

    def get_results(self, tail: int = 50) -> List[Dict[str, object]]:
        if tail <= 0:
            return []
        return list(self._results_buffer)[-tail:]

    def _read_output(self, process: subprocess.Popen) -> None:
        if process.stdout is None:
            return

        for line in process.stdout:
            text = line.rstrip("\r\n")
            if text:
                self._append_log(text)

        exit_code = process.poll()
        with self._lock:
            if self._process is process:
                self._process = None
            self._last_exit_code = exit_code
            self._append_log(f"worker exited with code {exit_code}")
            self._cleanup_sockets_locked()

    def _accept_and_read_results(self) -> None:
        with self._lock:
            server = self._server_sock

        if server is None:
            return

        try:
            conn, _addr = server.accept()
            with self._conn_lock:
                self._conn_sock = conn
            self._append_log("worker connected to result socket")

            buffer = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    payload = line.strip()
                    if not payload:
                        continue
                    try:
                        obj = json.loads(payload.decode("utf-8", errors="replace"))
                    except Exception:
                        obj = {"type": "raw", "data": payload.decode("utf-8", errors="replace")}

                    self._append_result(obj)
        except Exception as exc:
            self._append_log(f"result socket error: {exc}")
        finally:
            with self._conn_lock:
                try:
                    if self._conn_sock is not None:
                        self._conn_sock.close()
                except Exception:
                    pass
                self._conn_sock = None

    def _send_control(self, obj: Dict[str, object]) -> None:
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self._conn_lock:
            conn = self._conn_sock
            if conn is None:
                return
            try:
                conn.sendall(data)
            except Exception:
                return

    def _cleanup_sockets_locked(self) -> None:
        try:
            with self._conn_lock:
                if self._conn_sock is not None:
                    self._conn_sock.close()
                    self._conn_sock = None
        except Exception:
            pass

        try:
            if self._server_sock is not None:
                self._server_sock.close()
                self._server_sock = None
        except Exception:
            pass

    def _append_log(self, line: str) -> None:
        self._log_buffer.append({"ts": time.time(), "line": line})

    def _append_result(self, obj: object) -> None:
        item = {
            "ts": time.time(),
            "id": str(uuid.uuid4()),
            "data": obj,
        }
        self._results_buffer.append(item)


realtime_controller = RealTimeController()
