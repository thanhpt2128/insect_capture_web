"""
bench_iq_fps.py
===============
Đo FPS thực tế của panel I/Q trong realtime_gui.py sau khi áp blitting +
downsample, dùng ĐÚNG đường render TkAgg và đúng logic update mới.

So 3 cấu hình trên cùng figure/khung như GUI (5x3in @100dpi, 2 đường line):
  A. CŨ:  full draw_idle, 4096 điểm           (hành vi trước khi sửa)
  B. MỚI: blitting + adaptive ylim, 4096 điểm  (chỉ blitting)
  C. MỚI: blitting + adaptive ylim, 1024 điểm  (cấu hình đang dùng trong GUI)

In ms/lần và FPS tương ứng. 60fps cần <= 16.7 ms; 30fps cần <= 33 ms.

Chạy:
    .venv\\Scripts\\python.exe test\\bench_iq_fps.py
"""
import sys
import time
import statistics
from pathlib import Path

import numpy as np

try:
    import tkinter as tk
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    HAVE_TK = True
except Exception as exc:  # pragma: no cover
    print(f"[!] Không khởi tạo được TkAgg ({exc}); dùng Agg để đo (sát nhưng thiếu blit Tk).")
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    HAVE_TK = False


def make_canvas(root, npts):
    fig = Figure(figsize=(5, 3), dpi=100)
    ax = fig.add_subplot(111)
    x = np.arange(npts)
    (il,) = ax.plot(x, np.full(npts, np.nan), color="#d62728", lw=1.0, label="I", animated=True)
    (ql,) = ax.plot(x, np.full(npts, np.nan), color="#1f77b4", lw=1.0, label="Q", animated=True)
    ax.set_xlim(0, npts - 1)
    ax.set_ylabel("ADC"); ax.set_xlabel("Recent samples")
    ax.legend(loc="upper right")
    fig.tight_layout()
    if HAVE_TK:
        cv = FigureCanvasTkAgg(fig, master=root)
        cv.get_tk_widget().pack(fill="both", expand=True)
        root.update()  # realize + định kích thước widget
    else:
        cv = FigureCanvasAgg(fig)
        cv.draw()
    return fig, ax, il, ql, cv


def roll(buf, k):
    buf[:] = np.roll(buf, -k)
    # biên độ ổn định ~ ADC int16 để khung y không phải rescale liên tục
    buf[-k:] = np.random.uniform(-1800, 1800, size=k)


def bench_old(root, npts, n=120):
    """draw_idle + autoscale mỗi frame (hành vi cũ)."""
    fig, ax, il, ql, cv = make_canvas(root, npts)
    ib = np.zeros(npts); qb = np.zeros(npts)
    il.set_animated(False); ql.set_animated(False)
    cv.draw()
    xs = []
    for _ in range(n):
        roll(ib, 256); roll(qb, 256)
        il.set_ydata(ib); ql.set_ydata(qb)
        c = np.concatenate([ib, qb])
        ax.set_ylim(float(c.min()) - 1, float(c.max()) + 1)
        t = time.perf_counter()
        cv.draw()                      # full redraw đồng bộ
        if HAVE_TK:
            root.update_idletasks()
        xs.append((time.perf_counter() - t) * 1000)
    if HAVE_TK:
        cv.get_tk_widget().destroy()
    return xs


def bench_new(root, npts, n=120):
    """Blitting + adaptive ylim hysteresis (logic mới trong GUI)."""
    fig, ax, il, ql, cv = make_canvas(root, npts)
    ib = np.zeros(npts); qb = np.zeros(npts)
    bg = None; ylim = None; full_count = 0
    k = 256 if npts >= 256 else npts
    for _ in range(n):
        roll(ib, k); roll(qb, k)
        il.set_ydata(ib); ql.set_ydata(qb)
        need_full = bg is None
        c = np.concatenate([ib, qb])
        data_lo, data_hi = float(np.percentile(c, 1)), float(np.percentile(c, 99))
        span = data_hi - data_lo
        if (ylim is None or data_lo < ylim[0] or data_hi > ylim[1]
                or (ylim[1] - ylim[0]) > 3.0 * max(span, 1e-9)):
            head = max(span * 0.15, 1.0)
            ylim = (data_lo - head, data_hi + head)
            ax.set_ylim(*ylim); need_full = True
        t = time.perf_counter()
        if need_full:
            cv.draw(); bg = cv.copy_from_bbox(ax.bbox); full_count += 1
        cv.restore_region(bg)
        ax.draw_artist(il); ax.draw_artist(ql)
        cv.blit(ax.bbox)
        if HAVE_TK:
            root.update_idletasks()
        xs = locals().setdefault("_xs", [])
        xs.append((time.perf_counter() - t) * 1000)
    if HAVE_TK:
        cv.get_tk_widget().destroy()
    return locals()["_xs"], full_count


def report(name, xs, extra=""):
    xs2 = xs[3:]  # bỏ vài frame warmup
    m, md, mx = statistics.mean(xs2), statistics.median(xs2), max(xs2)
    print(f"  {name:<34} {m:6.2f} / {md:6.2f} / {mx:6.2f} ms   "
          f"~{1000/m:5.0f} FPS {extra}")


def main():
    backend = "TkAgg (thực tế)" if HAVE_TK else "Agg (xấp xỉ)"
    print("=" * 74)
    print(f"BENCHMARK FPS panel I/Q — backend: {backend}")
    print("  cột số: mean / median / max (ms/lần)  +  FPS theo mean")
    print("=" * 74)
    root = None
    if HAVE_TK:
        root = tk.Tk(); root.geometry("520x320+50+50"); root.title("bench_iq_fps")
    report("A. CŨ  full draw, 4096đ", bench_old(root, 4096))
    report("B. MỚI blit, 4096đ", *(lambda r: (r[0], f"(full-redraw {r[1]} lần)"))(bench_new(root, 4096)))
    xs, fc = bench_new(root, 1024)
    report("C. MỚI blit + downsample 1024đ", xs, f"(full-redraw {fc} lần)  <- GUI đang dùng")
    print("=" * 74)
    print("  Mốc: 60fps <= 16.7 ms ; 30fps <= 33 ms")
    if root is not None:
        root.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
