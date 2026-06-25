"""
realtime_gui.py
===============
GUI điều khiển + giám sát luồng phân loại côn trùng thời gian thực, thay cho
web server FastAPI. Dùng cho 1 người vận hành ngay trên máy chạy realtime.

Kiến trúc:
    GUI (Tkinter)  ──launch──>  realTimeProc_infer.py  (4 process: capture/DSP/AI)
       ^  (TCP server localhost)        │  (--no-local-view: KHÔNG mở cửa sổ plot riêng)
       └──────── JSON Lines ────────────┘
    GUI nhận kết quả qua socket, plot Range Profile + Range-Time (waterfall) ngay
    trong cửa sổ, hiển thị nhãn/độ tin cậy và log worker. Lệnh dừng gửi qua socket.

Pipeline (capture/DSP/AI/ThingsBoard) GIỮ NGUYÊN — GUI chỉ thay lớp UI của web.

Chạy:
    .venv\\Scripts\\python.exe realtime_gui.py
"""

import importlib.util
import json
import os
import queue
import socket
import subprocess
import sys
import threading
from pathlib import Path
from tkinter import (
    BooleanVar, StringVar, Tk, Toplevel, messagebox,
    END, DISABLED, NORMAL, W, EW, N, S,
)
from tkinter import ttk, scrolledtext

import numpy as np
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

HERE = Path(__file__).resolve().parent
WORKER = HERE / "realTimeProc_infer.py"
CONFIG_DIR = HERE / "configFiles"
IQ_HISTORY = 4096
WATERFALL_HISTORY = 300   # số frame hiển thị bề ngang waterfall


# ---------------------------------------------------------------------------
# Tiện ích: liệt kê COM, liệt kê cfg, tính radar metrics (tái dùng module web).
# ---------------------------------------------------------------------------
def list_com_ports():
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    out = []
    for p in list_ports.comports():
        out.append((p.device, p.description or ""))
    return out


def list_cfg_files():
    if not CONFIG_DIR.is_dir():
        return []
    return sorted(p.name for p in CONFIG_DIR.glob("*.cfg"))


def _load_metrics_fn():
    """Nạp calculate_radar_metrics từ module web (nếu có), tránh trùng lặp logic."""
    path = HERE / "pyradar_web" / "app" / "services" / "radar_metrics.py"
    if not path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("radar_metrics_gui", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.calculate_radar_metrics
    except Exception:
        return None


_METRICS_FN = _load_metrics_fn()


# ---------------------------------------------------------------------------
# Controller: launch worker + nhận kết quả (thay RealTimeController của web).
# ---------------------------------------------------------------------------
class RealtimeController:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.server_sock: socket.socket | None = None
        self.conn: socket.socket | None = None
        self.log_q: "queue.Queue[str]" = queue.Queue()
        self.result_q: "queue.Queue[dict]" = queue.Queue()
        self._stop = threading.Event()

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, com: str, cfg: str, baud: int, numframes: int, model: str) -> None:
        if self.is_running():
            return
        if not WORKER.exists():
            raise FileNotFoundError(f"Không tìm thấy worker: {WORKER}")
        self._stop.clear()

        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.bind(("127.0.0.1", 0))
        self.server_sock.listen(4)
        host, port = self.server_sock.getsockname()

        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        cmd = [
            sys.executable, str(WORKER),
            "--server-host", str(host), "--server-port", str(port),
            "--com-port", com, "--cli-baud", str(baud),
            "--cfg-path", cfg, "--numframes", str(numframes),
            "--model", model, "--no-local-view", "--no-profile",
        ]
        self.log_q.put(">>> " + " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd, cwd=str(HERE),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._accept_and_read, daemon=True).start()

    def stop(self) -> None:
        if self.conn is not None:
            try:
                self.conn.sendall((json.dumps({"cmd": "stop"}) + "\n").encode("utf-8"))
            except Exception:
                pass
        if self.proc is not None:
            try:
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
                try:
                    self.proc.wait(timeout=3)
                except Exception:
                    pass
        self._cleanup()

    def _read_stdout(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            text = line.rstrip("\r\n")
            if text:
                self.log_q.put(text)
        self.log_q.put(f"[worker thoát, code {proc.poll()}]")
        self.result_q.put({"__worker_exited__": True})

    def _accept_and_read(self) -> None:
        server = self.server_sock
        if server is None:
            return
        server.settimeout(0.5)
        while self.is_running() and not self._stop.is_set():
            try:
                conn, _addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.conn = conn
            self.log_q.put("[worker đã kết nối socket kết quả]")
            buffer = b""
            try:
                while not self._stop.is_set():
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            self.result_q.put(json.loads(line.decode("utf-8", "replace")))
                        except Exception:
                            pass
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def _cleanup(self) -> None:
        self._stop.set()
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
        if self.server_sock is not None:
            try:
                self.server_sock.close()
            except Exception:
                pass
            self.server_sock = None
        self.proc = None


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class App:
    def __init__(self, root: Tk):
        self.root = root
        root.title("Insect Radar — Realtime (GUI)")
        root.geometry("1180x760")

        self.ctrl = RealtimeController()
        self.latest_range_plot: dict | None = None
        self.latest_iq_plot: dict | None = None
        self.range_dirty = False
        self.iq_dirty = False

        # waterfall state
        self.n_bins: int | None = None
        self.wf_buf: np.ndarray | None = None
        self.wf_im = None
        self.range_res = 1.0
        self.iq_i_buf = np.full(IQ_HISTORY, np.nan, dtype=np.float32)
        self.iq_q_buf = np.full(IQ_HISTORY, np.nan, dtype=np.float32)

        self.com_var = StringVar()
        self.cfg_var = StringVar()
        self.baud_var = StringVar(value="921600")
        self.numframes_var = StringVar(value="30")
        self.model_var = StringVar(value="svm")
        self.status_var = StringVar(value="Idle")
        self.result_var = StringVar(value="—")
        self.label_var = StringVar(value="—")
        self.latency_var = StringVar(value="— ms")
        self.dsp_var = StringVar(value="— ms")

        self._build_ui()
        self._refresh_com()
        self._refresh_cfg()
        self.root.after(80, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(side="top", fill="x")

        # Hàng 1: COM + cfg
        ttk.Label(top, text="COM").grid(row=0, column=0, sticky=W)
        self.com_box = ttk.Combobox(top, textvariable=self.com_var, width=28, state="readonly")
        self.com_box.grid(row=0, column=1, padx=4)
        ttk.Button(top, text="Quét COM", command=self._refresh_com).grid(row=0, column=2, padx=2)

        ttk.Label(top, text="Cfg").grid(row=0, column=3, sticky=W, padx=(16, 0))
        self.cfg_box = ttk.Combobox(top, textvariable=self.cfg_var, width=28, state="readonly")
        self.cfg_box.grid(row=0, column=4, padx=4)
        ttk.Button(top, text="Tải lại", command=self._refresh_cfg).grid(row=0, column=5, padx=2)
        ttk.Button(top, text="Metrics", command=self._show_metrics).grid(row=0, column=6, padx=2)

        # Hàng 2: baud, numframes, model, nút
        ttk.Label(top, text="Baud").grid(row=1, column=0, sticky=W, pady=(6, 0))
        ttk.Entry(top, textvariable=self.baud_var, width=10).grid(row=1, column=1, sticky=W, padx=4, pady=(6, 0))
        ttk.Label(top, text="numframes").grid(row=1, column=3, sticky=W, pady=(6, 0), padx=(16, 0))
        ttk.Entry(top, textvariable=self.numframes_var, width=8).grid(row=1, column=4, sticky=W, padx=4, pady=(6, 0))
        ttk.Label(top, text="Model").grid(row=1, column=5, sticky=W, pady=(6, 0))
        ttk.Combobox(top, textvariable=self.model_var, values=["svm", "rf", "xgb"], width=6,
                     state="readonly").grid(row=1, column=6, sticky=W, padx=4, pady=(6, 0))

        btns = ttk.Frame(top)
        btns.grid(row=2, column=0, columnspan=7, sticky=W, pady=(8, 0))
        self.start_btn = ttk.Button(btns, text="Start", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="Stop", command=self._stop, state=DISABLED)
        self.stop_btn.pack(side="left", padx=8)
        ttk.Label(btns, text="Trạng thái:").pack(side="left", padx=(16, 4))
        ttk.Label(btns, textvariable=self.status_var, foreground="#0a7").pack(side="left")

        # Kết quả nhãn lớn
        res = ttk.Frame(self.root, padding=(8, 2))
        res.pack(side="top", fill="x")
        ttk.Label(res, text="Nhãn:").pack(side="left")
        ttk.Label(res, textvariable=self.label_var, font=("Segoe UI", 16, "bold"),
                  foreground="#c0392b").pack(side="left", padx=8)
        # Ô thời gian: từ lúc gom đủ 30 frame (trước DSP) → lúc infer xong.
        ttk.Label(res, text="Trễ xử lý:").pack(side="left", padx=(16, 0))
        ttk.Label(res, textvariable=self.latency_var, font=("Segoe UI", 16, "bold"),
                  foreground="#1565c0").pack(side="left", padx=8)
        ttk.Label(res, text="DSP:").pack(side="left", padx=(16, 0))
        ttk.Label(res, textvariable=self.dsp_var, font=("Segoe UI", 16, "bold"),
                  foreground="#2e7d32").pack(side="left", padx=8)
        ttk.Label(res, textvariable=self.result_var, foreground="#444").pack(side="left", padx=8)

        # Biểu đồ matplotlib
        grid = ttk.Frame(self.root, padding=8)
        grid.pack(side="top", fill="both", expand=True)
        grid.columnconfigure(0, weight=1, uniform="plot")
        grid.columnconfigure(1, weight=1, uniform="plot")
        grid.rowconfigure(0, weight=1, uniform="plot")
        grid.rowconfigure(1, weight=1, uniform="plot")

        wf_box = ttk.LabelFrame(grid, text="Range-Time")
        wf_box.grid(row=0, column=0, sticky=N + S + EW, padx=(0, 4), pady=(0, 4))
        self.fig_wf = Figure(figsize=(5, 3), dpi=100)
        self.ax_wf = self.fig_wf.add_subplot(1, 1, 1)
        self.ax_wf.set_xlabel("Time (frames)")
        self.ax_wf.set_ylabel("Range (m)")
        self.fig_wf.tight_layout()
        self.canvas_wf = FigureCanvasTkAgg(self.fig_wf, master=wf_box)
        self.canvas_wf.get_tk_widget().pack(fill="both", expand=True)

        pr_box = ttk.LabelFrame(grid, text="Range Profile")
        pr_box.grid(row=0, column=1, sticky=N + S + EW, padx=(4, 0), pady=(0, 4))
        self.fig_pr = Figure(figsize=(5, 3), dpi=100)
        self.ax_pr = self.fig_pr.add_subplot(1, 1, 1)
        self.ax_pr.set_xlabel("Range bin")
        self.ax_pr.set_ylabel("Amplitude (dB)")
        (self.pr_line,) = self.ax_pr.plot([], [], color="#1f77b4", lw=1.6)
        self.fig_pr.tight_layout()
        self.canvas_pr = FigureCanvasTkAgg(self.fig_pr, master=pr_box)
        self.canvas_pr.get_tk_widget().pack(fill="both", expand=True)

        iq_box = ttk.LabelFrame(grid, text="I/Q Amplitude")
        iq_box.grid(row=1, column=0, sticky=N + S + EW, padx=(0, 4), pady=(4, 0))
        self.fig_iq = Figure(figsize=(5, 3), dpi=100)
        self.ax_iq = self.fig_iq.add_subplot(1, 1, 1)
        self.ax_iq.set_xlabel("Recent samples")
        self.ax_iq.set_ylabel("ADC")
        x_iq = np.arange(IQ_HISTORY)
        (self.i_line,) = self.ax_iq.plot(x_iq, self.iq_i_buf, color="#d62728", lw=1.0, label="I")
        (self.q_line,) = self.ax_iq.plot(x_iq, self.iq_q_buf, color="#1f77b4", lw=1.0, label="Q")
        self.ax_iq.set_xlim(0, IQ_HISTORY - 1)
        self.ax_iq.legend(loc="upper right")
        self.fig_iq.tight_layout()
        self.canvas_iq = FigureCanvasTkAgg(self.fig_iq, master=iq_box)
        self.canvas_iq.get_tk_widget().pack(fill="both", expand=True)

        log_box = ttk.LabelFrame(grid, text="Worker Log")
        log_box.grid(row=1, column=1, sticky=N + S + EW, padx=(4, 0), pady=(4, 0))
        self.log = scrolledtext.ScrolledText(log_box, height=7, state=DISABLED, wrap="word")
        self.log.pack(fill="both", expand=True)

    # ------------------------------------------------------------- actions
    def _refresh_com(self):
        ports = list_com_ports()
        values = [f"{dev}  -  {desc}" if desc else dev for dev, desc in ports]
        self._com_devices = [dev for dev, _ in ports]
        self.com_box["values"] = values
        if values and not self.com_var.get():
            self.com_box.current(0)

    def _selected_com(self) -> str:
        idx = self.com_box.current()
        if 0 <= idx < len(getattr(self, "_com_devices", [])):
            return self._com_devices[idx]
        return self.com_var.get().split()[0] if self.com_var.get() else ""

    def _refresh_cfg(self):
        files = list_cfg_files()
        self.cfg_box["values"] = files
        if files and not self.cfg_var.get():
            self.cfg_box.current(0)

    def _show_metrics(self):
        cfg = self.cfg_var.get().strip()
        if not cfg:
            messagebox.showinfo("Metrics", "Hãy chọn file .cfg.")
            return
        if _METRICS_FN is None:
            messagebox.showwarning("Metrics", "Không nạp được module radar_metrics (pyradar_web).")
            return
        try:
            metrics = _METRICS_FN(CONFIG_DIR / cfg)
            text = json.dumps(metrics, indent=2, ensure_ascii=False)
        except Exception as exc:
            text = f"Lỗi tính metrics: {exc}"
        win = Toplevel(self.root)
        win.title(f"Radar metrics — {cfg}")
        win.geometry("440x520")
        box = scrolledtext.ScrolledText(win, wrap="word")
        box.pack(fill="both", expand=True)
        box.insert(END, text)
        box.config(state=DISABLED)

    def _start(self):
        com = self._selected_com()
        cfg = self.cfg_var.get().strip()
        if not com:
            messagebox.showerror("Thiếu COM", "Hãy chọn cổng COM.")
            return
        if not cfg:
            messagebox.showerror("Thiếu cfg", "Hãy chọn file .cfg.")
            return
        try:
            baud = int(self.baud_var.get())
            numframes = int(self.numframes_var.get())
        except ValueError:
            messagebox.showerror("Sai tham số", "Baud và numframes phải là số.")
            return

        cfg_path = str((CONFIG_DIR / cfg).resolve())
        self._reset_plots()
        try:
            self.ctrl.start(com, cfg_path, baud, numframes, self.model_var.get())
        except Exception as exc:
            messagebox.showerror("Không chạy được", str(exc))
            return
        self.status_var.set("running")
        self.start_btn.config(state=DISABLED)
        self.stop_btn.config(state=NORMAL)

    def _stop(self):
        self._append_log("[Stopping...]")
        self.ctrl.stop()
        self.status_var.set("stopped")
        self.start_btn.config(state=NORMAL)
        self.stop_btn.config(state=DISABLED)

    # --------------------------------------------------------- timer pump
    def _tick(self):
        # log
        try:
            while True:
                self._append_log(self.ctrl.log_q.get_nowait())
        except queue.Empty:
            pass

        # results (coalesce: chỉ giữ range_plot mới nhất để vẽ 1 lần)
        worker_exited = False
        try:
            while True:
                obj = self.ctrl.result_q.get_nowait()
                if obj.get("__worker_exited__"):
                    worker_exited = True
                    continue
                self._ingest_result(obj)
        except queue.Empty:
            pass

        if self.iq_dirty and self.latest_iq_plot is not None:
            self._update_iq_plot(self.latest_iq_plot)
            self.iq_dirty = False

        if self.range_dirty and self.latest_range_plot is not None:
            self._update_plots(self.latest_range_plot)
            self.range_dirty = False

        if worker_exited and self.start_btn["state"] == DISABLED:
            self.status_var.set("idle")
            self.start_btn.config(state=NORMAL)
            self.stop_btn.config(state=DISABLED)

        self.root.after(80, self._tick)

    def _ingest_result(self, data: dict):
        if data.get("type") == "status":
            self._append_log(f"[status] {data.get('event')}")
            return
        # nhãn + dòng tóm tắt
        if data.get("type") == "iq_preview":
            iq_plot = data.get("iq_plot") or {}
            if isinstance(iq_plot, dict) and iq_plot.get("ready"):
                self.latest_iq_plot = iq_plot
                self.iq_dirty = True
            return
        result = data.get("result") or {}
        label = result.get("label", "—")
        score = result.get("score")
        self.label_var.set(str(label))
        proc_ms = data.get("proc_ms")
        if isinstance(proc_ms, (int, float)):
            self.latency_var.set(f"{float(proc_ms):.0f} ms")
        dsp_ms = data.get("dsp_ms")
        if isinstance(dsp_ms, (int, float)):
            self.dsp_var.set(f"{float(dsp_ms):.0f} ms")
        rp = data.get("range_plot") or {}
        proba = result.get("proba") or {}
        proba_str = ",".join(f"{k}:{float(v):.2f}" for k, v in proba.items()) if proba else "-"
        infer_ms = data.get("infer_ms")
        parts = [
            f"seq={data.get('seq', '-')}",
            f"insect={data.get('is_insect')}",
            f"power={data.get('power_threshold', 0):.0f}" if isinstance(data.get("power_threshold"), (int, float)) else "power=-",
            f"score={score:.3f}" if isinstance(score, (int, float)) else "score=-",
            f"infer={float(infer_ms):.1f}ms" if isinstance(infer_ms, (int, float)) else "infer=-",
            f"proba=[{proba_str}]",
            f"bins={rp.get('range_bins', '-')}",
            f"frames={rp.get('frame_count', '-')}",
        ]
        self.result_var.set(" | ".join(parts))

        if isinstance(rp, dict) and rp.get("ready"):
            self.latest_range_plot = rp
            self.range_dirty = True

    # ------------------------------------------------------------- plots
    def _reset_plots(self):
        self.latest_range_plot = None
        self.latest_iq_plot = None
        self.range_dirty = False
        self.iq_dirty = False
        self.n_bins = None
        self.wf_buf = None
        self.wf_im = None
        self.iq_i_buf[:] = np.nan
        self.iq_q_buf[:] = np.nan
        self.ax_iq.clear()
        self.ax_iq.set_xlabel("Recent samples")
        self.ax_iq.set_ylabel("ADC")
        x_iq = np.arange(IQ_HISTORY)
        (self.i_line,) = self.ax_iq.plot(x_iq, self.iq_i_buf, color="#d62728", lw=1.0, label="I")
        (self.q_line,) = self.ax_iq.plot(x_iq, self.iq_q_buf, color="#1f77b4", lw=1.0, label="Q")
        self.ax_iq.set_xlim(0, IQ_HISTORY - 1)
        self.ax_iq.legend(loc="upper right")
        self.ax_wf.clear()
        self.ax_wf.set_xlabel("Time (frames)")
        self.ax_wf.set_ylabel("Range (m)")
        self.ax_pr.clear()
        self.ax_pr.set_xlabel("Range bin")
        self.ax_pr.set_ylabel("Amplitude (dB)")
        (self.pr_line,) = self.ax_pr.plot([], [], color="#1f77b4", lw=1.6)
        self.label_var.set("—")
        self.result_var.set("—")
        try:
            self.canvas_iq.draw_idle()
            self.canvas_wf.draw_idle()
            self.canvas_pr.draw_idle()
        except Exception:
            pass

    def _update_iq_plot(self, iq: dict):
        i_vals = np.asarray(iq.get("i") or [], dtype=np.float32)
        q_vals = np.asarray(iq.get("q") or [], dtype=np.float32)
        n = min(i_vals.size, q_vals.size, IQ_HISTORY)
        if n <= 0:
            return

        i_vals = i_vals[-n:]
        q_vals = q_vals[-n:]
        self.iq_i_buf = np.roll(self.iq_i_buf, -n)
        self.iq_q_buf = np.roll(self.iq_q_buf, -n)
        self.iq_i_buf[-n:] = i_vals
        self.iq_q_buf[-n:] = q_vals

        self.i_line.set_ydata(self.iq_i_buf)
        self.q_line.set_ydata(self.iq_q_buf)

        combined = np.concatenate([
            self.iq_i_buf[np.isfinite(self.iq_i_buf)],
            self.iq_q_buf[np.isfinite(self.iq_q_buf)],
        ])
        if combined.size >= 16:
            ymin = float(np.percentile(combined, 1))
            ymax = float(np.percentile(combined, 99))
            if ymin == ymax:
                ymin, ymax = ymin - 1.0, ymax + 1.0
            pad = max((ymax - ymin) * 0.08, 1.0)
            self.ax_iq.set_ylim(ymin - pad, ymax + pad)

        try:
            self.canvas_iq.draw_idle()
        except Exception:
            pass

    def _update_plots(self, rp: dict):
        profile = np.asarray(rp.get("range_profile") or [], dtype=float)
        matrix = np.asarray(rp.get("range_time") or [], dtype=float)  # [frames, bins]
        if profile.size == 0 or matrix.ndim != 2 or matrix.size == 0:
            return

        n_bins = matrix.shape[1]
        self.range_res = float(rp.get("range_resolution_m") or 1.0)

        # init/realloc waterfall buffer + imshow khi biết số bin
        if self.n_bins != n_bins or self.wf_buf is None or self.wf_im is None:
            self.n_bins = n_bins
            self.wf_buf = np.full((n_bins, WATERFALL_HISTORY), np.nan, dtype=np.float32)
            self.ax_wf.clear()
            self.ax_wf.set_xlabel("Time (frames)")
            self.ax_wf.set_ylabel("Range (m)")
            max_range = n_bins * self.range_res
            self.wf_im = self.ax_wf.imshow(
                self.wf_buf, aspect="auto", origin="lower", cmap="viridis",
                extent=[0, WATERFALL_HISTORY, 0, max_range], interpolation="nearest",
            )

        # cuộn waterfall, nạp các cột mới (mỗi cột = 1 frame trong lô)
        k = matrix.shape[0]
        self.wf_buf = np.roll(self.wf_buf, -k, axis=1)
        self.wf_buf[:, -k:] = matrix.T
        finite = self.wf_buf[np.isfinite(self.wf_buf)]
        if finite.size >= 16:
            vmin = float(np.percentile(finite, 5))
            vmax = float(np.percentile(finite, 99))
            if vmin < vmax:
                self.wf_im.set_clim(vmin, vmax)
        self.wf_im.set_data(self.wf_buf)

        # range profile
        x = np.arange(profile.size)
        self.pr_line.set_data(x, profile)
        self.ax_pr.set_xlim(0, max(profile.size - 1, 1))
        pmin, pmax = float(np.min(profile)), float(np.max(profile))
        if pmin == pmax:
            pmin, pmax = pmin - 1, pmax + 1
        self.ax_pr.set_ylim(pmin - 1, pmax + 1)

        try:
            self.canvas_wf.draw_idle()
            self.canvas_pr.draw_idle()
        except Exception:
            pass

    # -------------------------------------------------------------- misc
    def _append_log(self, text: str):
        if text.startswith("[T "):
            return
        self.log.config(state=NORMAL)
        self.log.insert(END, text + "\n")
        # giới hạn ~500 dòng
        if int(self.log.index("end-1c").split(".")[0]) > 500:
            self.log.delete("1.0", "100.end")
        self.log.see(END)
        self.log.config(state=DISABLED)

    def _on_close(self):
        try:
            if self.ctrl.is_running():
                self.ctrl.stop()
        except Exception:
            pass
        self.root.destroy()


if __name__ == "__main__":
    root = Tk()
    App(root)
    root.mainloop()
