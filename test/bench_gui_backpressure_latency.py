"""
bench_gui_backpressure_latency.py
==================================
Kiểm chứng giả thuyết: phần "Trễ xử lý" (proc_ms) dao động thất thường (vd
280ms rồi lại 190ms) dù DSP luôn ổn định (~150ms) là do TCP socket P3->GUI bị
NGHẼN NGƯỢC (backpressure) khi thread nhận socket của GUI (_accept_and_read)
không được CPU kịp thời vì thread chính Tk đang bận render matplotlib (giữ GIL).

Cơ chế nghi ngờ, tái hiện ĐÚNG cấu trúc vòng lặp thật trong ai_worker_process:
    while chạy:
        _drain_iq_preview()   # sendall() TRƯỚC KHI lấy lô tiếp theo
        seq, ts, proc_result = ai_queue.get(...)
        proc_ms = time.time() - ts   # <- đo TẠI ĐÂY, SAU khi đã vượt qua sendall() ở trên
        _send_json_line(sock, payload)

Nếu sendall() của _drain_iq_preview() (hoặc lần gửi trước) bị OS TCP giữ lại vì
bên nhận (GUI) chưa recv() kịp (đang bận render, giữ GIL), thì vòng lặp P3 bị
CHẶN LẠI TRƯỚC KHI lấy lô mới -> lô đó phải "chờ" lâu hơn trong ai_queue trước
khi proc_ms được tính -> proc_ms tăng dù dsp_ms (đo tại P2, không liên quan
socket) hoàn toàn không đổi.

Thiết kế test:
  - Thread giả lập GUI (_accept_and_read) chạy CÙNG PROCESS với vòng lặp giữ
    GIL (giống Tk main thread) -> đúng kiểu tranh chấp GIL thật.
  - P3 THẬT (import _send_json_line, DropOldestQueue từ realTimeProc_infer.py)
    chạy Ở PROCESS RIÊNG, kết nối tới GUI giả lập qua TCP localhost thật.
  - So 2 kịch bản: (A) GUI rảnh hoàn toàn, (B) GUI có "cơn render" định kỳ
    (mô phỏng draw() matplotlib thật, dùng lại bench_draw.py-style).

Chạy:
    .venv\\Scripts\\python.exe test\\bench_gui_backpressure_latency.py
"""
import sys
import json
import socket
import threading
import time
import statistics
import multiprocessing as mp
from pathlib import Path
from queue import Empty

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from realTimeProc_infer import DropOldestQueue, _send_json_line  # hàm/lớp THẬT

import numpy as np

BATCH = 30
FS_FPS = 60.0
CADENCE_S = BATCH / FS_FPS  # 0.5s @60fps


def pct(xs, p):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100.0
    f = int(k); c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def summ(name, xs, unit="ms"):
    if not xs:
        print(f"  {name:<20} (không có mẫu)"); return
    print(f"  {name:<20} n={len(xs):3d}  mean={statistics.mean(xs):7.1f}  "
          f"med={statistics.median(xs):7.1f}  p95={pct(xs,95):7.1f}  max={max(xs):7.1f} {unit}")


# ── P3 THẬT (chạy ở process riêng): bản rút gọn ĐÚNG cấu trúc vòng lặp thật ──
def p3_worker(ai_queue, iq_preview_queue, exit_event, host, port):
    sock = socket.create_connection((host, port), timeout=10)
    _send_json_line(sock, {"type": "status", "event": "ready", "ts": time.time()})

    def drain_iq_preview():
        latest = None
        while True:
            try:
                latest = iq_preview_queue.get(block=False)
            except Empty:
                break
        if latest is not None:
            try:
                _send_json_line(sock, latest)   # <- sendall() có thể bị OS giữ lại (backpressure)
            except Exception:
                pass

    while not exit_event.is_set():
        drain_iq_preview()
        try:
            seq, ts = ai_queue.get(block=True, timeout=0.05)
        except Empty:
            continue
        proc_ms = (time.time() - float(ts)) * 1000.0   # đo ĐÚNG như ai_worker_process thật
        try:
            _send_json_line(sock, {"type": "inference", "seq": seq, "ts": ts,
                                   "proc_ms": round(proc_ms, 1)})
        except Exception:
            pass
    try:
        sock.close()
    except Exception:
        pass


def producer(ai_queue, iq_preview_queue, n_batches, exit_event):
    for seq in range(n_batches):
        ai_queue.put((seq, time.time()))
        # ~20 iq_preview/s như thật, mỗi cái 256 mẫu I + 256 mẫu Q
        t_end = time.time() + CADENCE_S
        while time.time() < t_end:
            iq_preview_queue.put({
                "type": "iq_preview", "ts": time.time(),
                "iq_plot": {"i": np.random.rand(256).tolist(),
                           "q": np.random.rand(256).tolist()},
            })
            time.sleep(0.05)
    time.sleep(0.3)
    exit_event.set()


# ── GUI giả lập: recv thread CÙNG process với main thread (đúng kiểu Tk) ──
class MimicGui:
    def __init__(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.host, self.port = self.server.getsockname()
        self.recv_log = []   # (recv_wall_time, obj)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept_and_read, daemon=True)

    def start(self):
        self._thread.start()

    def _accept_and_read(self):
        self.server.settimeout(2.0)
        try:
            conn, _ = self.server.accept()
        except socket.timeout:
            return
        conn.settimeout(2.0)
        buffer = b""
        while not self._stop.is_set():
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line.decode("utf-8", "replace"))
                except Exception:
                    continue
                self.recv_log.append((time.time(), obj))
        try:
            conn.close()
        except Exception:
            pass

    def stop(self):
        self._stop.set()
        try:
            self.server.close()
        except Exception:
            pass


def run_scenario(label, render_stalls: bool, n_batches=36):
    mp.set_start_method("spawn", force=True)
    gui = MimicGui()
    gui.start()
    time.sleep(0.1)

    ai_q = DropOldestQueue(maxsize=10)
    iq_q = DropOldestQueue(maxsize=4)
    exit_evt = mp.Event()

    p3 = mp.Process(target=p3_worker, args=(ai_q, iq_q, exit_evt, gui.host, gui.port))
    p3.start()
    time.sleep(0.3)  # cho P3 kết nối xong

    prod_thread = threading.Thread(target=producer, args=(ai_q, iq_q, n_batches, exit_evt))
    prod_thread.start()

    # Main thread: nếu render_stalls, mô phỏng "cơn render matplotlib" định kỳ
    # NGAY TRÊN PROCESS CHỨA recv-thread của GUI (tranh GIL thật, giống Tk).
    if render_stalls:
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        fig = Figure(figsize=(5, 3), dpi=100)
        ax = fig.add_subplot(111)
        (line,) = ax.plot(np.zeros(4096))
        cv = FigureCanvasAgg(fig); cv.draw()
        t_end_all = time.time() + n_batches * CADENCE_S + 1.0
        while time.time() < t_end_all and prod_thread.is_alive():
            line.set_ydata(np.random.rand(4096))
            cv.draw()   # ~85ms giữ GIL / lần (full draw 4096đ, giống GUI TRƯỚC khi tối ưu)
            time.sleep(0.08)  # nhịp ~giống root.after(80ms) của Tk
    else:
        prod_thread.join(timeout=n_batches * CADENCE_S + 5)

    prod_thread.join(timeout=5)
    exit_evt.set()
    p3.join(timeout=5)
    if p3.is_alive():
        p3.terminate(); p3.join()
    time.sleep(0.2)
    gui.stop()

    proc_ms_list = [obj["proc_ms"] for _t, obj in gui.recv_log if obj.get("type") == "inference"]
    print(f"\n[{label}] nhận {len(proc_ms_list)}/{n_batches} bản tin inference qua TCP THẬT")
    summ("proc_ms (qua TCP thật)", proc_ms_list)
    return proc_ms_list


def main():
    print("=" * 78)
    print("KIỂM CHỨNG: backpressure TCP do GUI bận render có làm proc_ms tăng?")
    print("=" * 78)

    baseline = run_scenario("A. GUI RẢNH (không render)", render_stalls=False)
    stressed = run_scenario("B. GUI BẬN RENDER (~85ms/lần, nhịp 80ms, full-draw 4096đ)",
                            render_stalls=True)

    print("\n" + "=" * 78)
    if baseline and stressed:
        print(f"So sánh mean: rảnh={statistics.mean(baseline):.1f}ms  "
              f"bận render={statistics.mean(stressed):.1f}ms  "
              f"chênh={statistics.mean(stressed)-statistics.mean(baseline):+.1f}ms")
        print(f"So sánh max : rảnh={max(baseline):.1f}ms  bận render={max(stressed):.1f}ms")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
