"""
bench_sliding_stride.py
=======================
Đo hiệu năng + độ trễ của cửa sổ trượt (sliding window) ở stride 30 (tumbling,
đối chiếu), 20 và 15 — mô phỏng ĐÚNG luồng cross-process P1->P2->P3 thật ở 60fps.

Dùng: DropOldestQueue thật, InsectRadarProcessor thật, model svm thật, dữ liệu
raw thật; P1 nhịp 60fps thật và chạy ĐÚNG logic gom-lô trượt của capture worker.

Đo cho mỗi stride:
  - dsp_ms         : thời gian process_complex() (GUI hiển thị "DSP")
  - proc_ms        : window-close -> inference-done (GUI hiển thị "Trễ xử lý")
  - nhịp kết quả   : khoảng cách trung vị giữa 2 kết quả liên tiếp tại P3
  - backlog/drop   : windows P1 tạo vs P3 nhận; qsize lớn nhất
  - tải P2         : dsp_mean / cadence (P2 bận bao nhiêu % thời gian)
  - độ trễ PHÁT HIỆN tổng = gather (chờ gom vào cửa sổ) + proc_ms

Chạy:
    .venv\\Scripts\\python.exe test\\bench_sliding_stride.py
"""
import sys
import time
import statistics
import multiprocessing as mp
from pathlib import Path
from queue import Empty

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np

INT16_PER_FRAME = 131_072
BATCH = 30
FS_FPS = 60.0
RAW = ROOT / "data_parse" / "raw_data_50fps.bin"
N_FRAMES = 480  # ~8s @60fps -> stride15:31, stride20:23, stride30:16 cửa sổ


def pct(xs, p):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100.0
    f = int(k); c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


# ── P1: capture — nhịp 60fps thật + ĐÚNG logic gom-lô trượt ──
def _p1(preprocess_q, stride, n_frames, done_evt, produced_ctr, max_qsize):
    from insect_radar_processor.insect_radar_processor import _int16_to_complex
    raw = np.fromfile(RAW, dtype=np.int16, count=n_frames * INT16_PER_FRAME)
    frames = raw.reshape(n_frames, INT16_PER_FRAME)
    iq_batch = []
    batch_seq = 0
    start = time.perf_counter()
    for f in range(n_frames):
        # nhịp 60fps thật (lịch tuyệt đối, tránh trôi)
        target = start + (f + 1) / FS_FPS
        now = time.perf_counter()
        if target > now:
            time.sleep(target - now)
        c = _int16_to_complex(frames[f], iq_order="QQII")
        iq_batch.append(c)
        if len(iq_batch) >= BATCH:
            batch_iq = np.concatenate(iq_batch[:BATCH]).astype(np.complex64, copy=False)
            del iq_batch[:stride]                     # <- logic trượt thật
            try:
                qs = preprocess_q.queue.qsize()
                with max_qsize.get_lock():
                    if qs > max_qsize.value:
                        max_qsize.value = qs
            except Exception:
                pass
            preprocess_q.put((batch_seq, time.time(), batch_iq, "complex"))
            with produced_ctr.get_lock():
                produced_ctr.value += 1
            batch_seq += 1
    done_evt.set()


# ── P2: DSP thật ──
def _p2(preprocess_q, ai_q, done_evt, dsp_ms_list):
    import realTimeProc_infer as w
    w.np = np
    from realTimeProc_infer import _build_range_plot
    from insect_radar_processor.insect_radar_processor import InsectRadarProcessor
    proc = InsectRadarProcessor(range_bin_min=15, range_bin_max=20, iq_order="QQII")
    while True:
        try:
            seq, ts, iq_data, _k = preprocess_q.get(timeout=0.5)
        except Empty:
            if done_evt.is_set():
                break
            continue
        t0 = time.time()
        result = proc.process_complex(iq_data)
        dsp_ms = (time.time() - t0) * 1000.0
        dsp_ms_list.append(dsp_ms)
        rp = _build_range_plot(result, proc.cfg.range_resolution)  # như thật
        ai_q.put((seq, ts, {"is_insect": result["is_insect"],
                            "features": result["features"], "range_plot": rp,
                            "dsp_ms": round(dsp_ms, 1)}))


# ── P3: inference svm thật ──
def _p3(ai_q, done_evt, proc_ms_list, arrival_list, received_ctr, models_dir):
    import joblib
    model = joblib.load(models_dir / "svm_pipeline.pkl")
    feature_names = list(joblib.load(models_dir / "feature_names.pkl"))
    while True:
        try:
            seq, ts, pr = ai_q.get(timeout=0.5)
        except Empty:
            if done_evt.is_set():
                break
            continue
        if pr["is_insect"]:
            X = np.array([pr["features"][n] for n in feature_names],
                        dtype=np.float64).reshape(1, -1)
            model.predict(X)
            if hasattr(model, "predict_proba"):
                model.predict_proba(X)
        proc_ms_list.append((time.time() - float(ts)) * 1000.0)
        arrival_list.append(time.perf_counter())
        with received_ctr.get_lock():
            received_ctr.value += 1


def run_stride(stride):
    q1 = None
    from realTimeProc_infer import DropOldestQueue
    q1 = DropOldestQueue(maxsize=10)
    q2 = DropOldestQueue(maxsize=10)
    done = mp.Event()
    mgr = mp.Manager()
    dsp_ms = mgr.list(); proc_ms = mgr.list(); arrivals = mgr.list()
    produced = mp.Value("i", 0); received = mp.Value("i", 0); max_qsize = mp.Value("i", 0)
    models_dir = ROOT / "insect_radar_processor" / "models"

    p1 = mp.Process(target=_p1, args=(q1, stride, N_FRAMES, done, produced, max_qsize))
    p2 = mp.Process(target=_p2, args=(q1, q2, done, dsp_ms))
    p3 = mp.Process(target=_p3, args=(q2, done, proc_ms, arrivals, received, models_dir))
    p1.start(); p2.start(); p3.start()
    p1.join()
    p2.join(timeout=20); p3.join(timeout=20)
    for p in (p2, p3):
        if p.is_alive():
            p.terminate(); p.join()

    dsp = list(dsp_ms); proc = list(proc_ms); arr = sorted(arrivals)
    cadence_ms = [(arr[i + 1] - arr[i]) * 1000.0 for i in range(len(arr) - 1)]
    res = {
        "stride": stride,
        "cadence_ideal_ms": stride / FS_FPS * 1000.0,
        "dsp_mean": statistics.mean(dsp) if dsp else float("nan"),
        "dsp_p95": pct(dsp, 95),
        "proc_mean": statistics.mean(proc) if proc else float("nan"),
        "proc_p95": pct(proc, 95),
        "cadence_med": statistics.median(cadence_ms) if cadence_ms else float("nan"),
        "produced": produced.value,
        "received": received.value,
        "max_qsize": max_qsize.value,
    }
    mgr.shutdown()
    return res


def main():
    if not RAW.exists():
        print(f"[!] Không tìm thấy {RAW}"); return 1
    mp.set_start_method("spawn", force=True)
    print("=" * 92)
    print(f"BENCHMARK SLIDING WINDOW @ {FS_FPS:.0f}fps  (numframes={BATCH}, {N_FRAMES} frame/lần chạy)")
    print("=" * 92)

    results = []
    for stride in (30, 20, 15):
        label = "tumbling" if stride == BATCH else f"overlap {BATCH - stride}"
        print(f"\n>>> stride={stride} ({label}) ...", flush=True)
        results.append(run_stride(stride))

    print("\n" + "=" * 92)
    print(f"{'stride':>6} {'nhịp lý tưởng':>14} {'DSP mean/p95':>16} {'proc mean/p95':>17} "
          f"{'nhịp KQ thực':>13} {'drop':>10} {'qmax':>5} {'tải P2':>8}")
    print("-" * 92)
    for r in results:
        drop = r["produced"] - r["received"]
        duty = r["dsp_mean"] / r["cadence_ideal_ms"] * 100.0
        print(f"{r['stride']:>6} {r['cadence_ideal_ms']:>11.0f} ms "
              f"{r['dsp_mean']:>6.0f}/{r['dsp_p95']:<6.0f} ms "
              f"{r['proc_mean']:>6.0f}/{r['proc_p95']:<6.0f} ms "
              f"{r['cadence_med']:>10.0f} ms "
              f"{drop:>3}/{r['produced']:<4} {r['max_qsize']:>5} {duty:>6.0f}%")

    # Độ trễ PHÁT HIỆN tổng = gather (chờ gom vào cửa sổ) + proc_ms
    print("\n" + "-" * 92)
    print("ĐỘ TRỄ PHÁT HIỆN (từ lúc sự kiện xuất hiện -> có kết quả phản ánh nó):")
    print("  = gather (chờ frame lọt vào cửa sổ & cửa sổ đóng) + proc_ms")
    print(f"  {'stride':>6} {'gather TB':>11} {'gather xấu nhất':>16} {'proc mean':>11} "
          f"{'-> TỔNG TB':>12} {'TỔNG xấu nhất':>15}")
    for r in results:
        s = r["stride"]
        gather_avg = (s - 1) / 2 / FS_FPS * 1000.0
        gather_worst = (s - 1) / FS_FPS * 1000.0
        total_avg = gather_avg + r["proc_mean"]
        total_worst = gather_worst + r["proc_p95"]
        print(f"  {s:>6} {gather_avg:>8.0f} ms {gather_worst:>13.0f} ms {r['proc_mean']:>8.0f} ms "
              f"{total_avg:>9.0f} ms {total_worst:>12.0f} ms")

    print("\n" + "=" * 92)
    print("Đọc: 'tải P2' = % thời gian P2 bận DSP (100% -> hết biên, bắt đầu backlog).")
    print("     'drop' = số cửa sổ bị DropOldestQueue bỏ (P2 không theo kịp).")
    print("     stride nhỏ hơn -> nhịp KQ dày hơn + độ trễ phát hiện thấp hơn, đổi lại tải P2 cao hơn.")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
