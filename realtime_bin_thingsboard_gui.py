"""
realtime_bin_thingsboard_gui.py
===============================
GUI nhanh (Tkinter, thư viện chuẩn) để điền tham số rồi bấm Start chạy
realtime_bin_thingsboard.py. Khi pipeline chạy xong (hết file .bin) thì tự thoát.

Chạy:
    .venv\\Scripts\\python.exe realtime_bin_thingsboard_gui.py
(hoặc:  python realtime_bin_thingsboard_gui.py  — miễn là Python đó có numpy,
 scikit-learn, paho-mqtt và Tkinter.)
"""

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from tkinter import (
    BooleanVar, StringVar, Tk, filedialog, messagebox,
    END, DISABLED, NORMAL, W, EW,
)
from tkinter import ttk, scrolledtext

HERE = Path(__file__).resolve().parent
WORKER = HERE / "realtime_bin_thingsboard.py"


class App:
    def __init__(self, root: Tk):
        self.root = root
        root.title("Insect Radar — Bin Replay → ThingsBoard")
        root.geometry("760x560")

        self.proc: subprocess.Popen | None = None
        self.log_q: "queue.Queue[str | None]" = queue.Queue()

        # ── Biến nhập liệu (giá trị mặc định) ────────────────────────────
        self.bin_path  = StringVar()
        self.cfg_path  = StringVar(value="cfg128_128_100fps.cfg")
        self.model     = StringVar(value="svm")
        self.interval  = StringVar(value="0.5")
        self.rb_min    = StringVar(value="15")
        self.rb_max    = StringVar(value="20")
        self.loop_file = BooleanVar(value=False)
        self.auto_exit = BooleanVar(value=True)

        self.tb_host  = StringVar()
        self.tb_port  = StringVar(value="1883")
        self.tb_token = StringVar()
        self.tb_topic = StringVar(value="v1/devices/me/telemetry")
        self.tb_qos   = StringVar(value="1")

        self._build_form()
        self.root.after(100, self._drain_log)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------------------------------------------------------- UI
    def _build_form(self):
        frm = ttk.Frame(self.root, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(frm, text="File .bin (IIQQ) *").grid(row=r, column=0, sticky=W, pady=3)
        ttk.Entry(frm, textvariable=self.bin_path).grid(row=r, column=1, sticky=EW, padx=6)
        ttk.Button(frm, text="Chọn...", command=self._browse).grid(row=r, column=2)

        r += 1
        ttk.Label(frm, text="File .cfg").grid(row=r, column=0, sticky=W, pady=3)
        ttk.Entry(frm, textvariable=self.cfg_path).grid(row=r, column=1, columnspan=2, sticky=EW, padx=6)

        r += 1
        row = ttk.Frame(frm); row.grid(row=r, column=0, columnspan=3, sticky=EW, pady=3)
        ttk.Label(row, text="Model").pack(side="left")
        ttk.Combobox(row, textvariable=self.model, values=["svm", "rf", "xgb"],
                     width=6, state="readonly").pack(side="left", padx=(4, 16))
        ttk.Label(row, text="Chu kỳ (s)").pack(side="left")
        ttk.Entry(row, textvariable=self.interval, width=6).pack(side="left", padx=(4, 16))
        ttk.Label(row, text="Range bin min/max").pack(side="left")
        ttk.Entry(row, textvariable=self.rb_min, width=5).pack(side="left", padx=4)
        ttk.Entry(row, textvariable=self.rb_max, width=5).pack(side="left", padx=4)
        ttk.Checkbutton(row, text="Lặp file", variable=self.loop_file).pack(side="left", padx=12)

        r += 1
        ttk.Separator(frm).grid(row=r, column=0, columnspan=3, sticky=EW, pady=8)

        r += 1
        ttk.Label(frm, text="ThingsBoard host").grid(row=r, column=0, sticky=W, pady=3)
        hp = ttk.Frame(frm); hp.grid(row=r, column=1, columnspan=2, sticky=EW, padx=6)
        ttk.Entry(hp, textvariable=self.tb_host).pack(side="left", fill="x", expand=True)
        ttk.Label(hp, text="Port").pack(side="left", padx=(10, 4))
        ttk.Entry(hp, textvariable=self.tb_port, width=7).pack(side="left")

        r += 1
        ttk.Label(frm, text="Access token").grid(row=r, column=0, sticky=W, pady=3)
        ttk.Entry(frm, textvariable=self.tb_token).grid(row=r, column=1, columnspan=2, sticky=EW, padx=6)

        r += 1
        ttk.Label(frm, text="Topic").grid(row=r, column=0, sticky=W, pady=3)
        tp = ttk.Frame(frm); tp.grid(row=r, column=1, columnspan=2, sticky=EW, padx=6)
        ttk.Entry(tp, textvariable=self.tb_topic).pack(side="left", fill="x", expand=True)
        ttk.Label(tp, text="QoS").pack(side="left", padx=(10, 4))
        ttk.Combobox(tp, textvariable=self.tb_qos, values=["0", "1"], width=4,
                     state="readonly").pack(side="left")

        r += 1
        ttk.Label(frm, text="(Bỏ trống host/token → chạy dry-run, in payload thay vì gửi)",
                  foreground="#666").grid(row=r, column=0, columnspan=3, sticky=W, pady=(2, 6))

        r += 1
        btns = ttk.Frame(frm); btns.grid(row=r, column=0, columnspan=3, sticky=EW)
        self.start_btn = ttk.Button(btns, text="Start", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="Stop", command=self._stop, state=DISABLED)
        self.stop_btn.pack(side="left", padx=8)
        ttk.Checkbutton(btns, text="Tự thoát khi xong", variable=self.auto_exit).pack(side="left", padx=16)

        r += 1
        frm.rowconfigure(r, weight=1)
        self.log = scrolledtext.ScrolledText(frm, height=14, state=DISABLED, wrap="word")
        self.log.grid(row=r, column=0, columnspan=3, sticky="nsew", pady=(8, 0))

    def _browse(self):
        path = filedialog.askopenfilename(
            initialdir=str(HERE / "data_parse"),
            filetypes=[("Raw ADC bin", "*.bin"), ("All files", "*.*")],
        )
        if path:
            self.bin_path.set(path)

    # ------------------------------------------------------------- run
    def _start(self):
        if self.proc is not None:
            return
        if not self.bin_path.get().strip():
            messagebox.showerror("Thiếu thông tin", "Hãy chọn file .bin.")
            return
        if not WORKER.exists():
            messagebox.showerror("Lỗi", f"Không tìm thấy worker: {WORKER}")
            return

        cmd = [
            sys.executable, str(WORKER),
            "--bin-path", self.bin_path.get().strip(),
            "--cfg-path", self.cfg_path.get().strip(),
            "--model", self.model.get(),
            "--numframes", "30",
            "--interval", self.interval.get().strip() or "0.5",
            "--range-bin-min", self.rb_min.get().strip() or "15",
            "--range-bin-max", self.rb_max.get().strip() or "20",
            "--tb-port", self.tb_port.get().strip() or "1883",
            "--tb-topic", self.tb_topic.get().strip() or "v1/devices/me/telemetry",
            "--tb-qos", self.tb_qos.get(),
        ]
        if self.loop_file.get():
            cmd.append("--loop-file")
        if self.tb_host.get().strip():
            cmd += ["--tb-host", self.tb_host.get().strip()]
        if self.tb_token.get().strip():
            cmd += ["--tb-token", self.tb_token.get().strip()]

        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=str(HERE),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
            )
        except Exception as exc:
            messagebox.showerror("Không chạy được", str(exc))
            self.proc = None
            return

        self._append(f">>> {' '.join(cmd)}\n")
        self.start_btn.config(state=DISABLED)
        self.stop_btn.config(state=NORMAL)
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self.log_q.put(line)
        self.proc.wait()
        self.log_q.put(f"\n[process exit code {self.proc.returncode}]\n")
        self.log_q.put(None)  # tín hiệu kết thúc

    def _stop(self):
        if self.proc and self.proc.poll() is None:
            self._append("[Stopping...]\n")
            try:
                self.proc.terminate()
            except Exception:
                pass

    # --------------------------------------------------------- log pump
    def _drain_log(self):
        try:
            while True:
                item = self.log_q.get_nowait()
                if item is None:
                    self._on_finished()
                else:
                    self._append(item)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log)

    def _append(self, text: str):
        self.log.config(state=NORMAL)
        self.log.insert(END, text)
        self.log.see(END)
        self.log.config(state=DISABLED)

    def _on_finished(self):
        self.proc = None
        self.start_btn.config(state=NORMAL)
        self.stop_btn.config(state=DISABLED)
        if self.auto_exit.get():
            self._append("[Hoàn tất — tự thoát sau 2s]\n")
            self.root.after(2000, self.root.destroy)

    def _on_close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self.root.destroy()


if __name__ == "__main__":
    root = Tk()
    App(root)
    root.mainloop()
