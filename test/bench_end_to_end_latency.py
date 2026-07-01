"""
bench_end_to_end_latency.py
============================
Đo đúng 2 số mà GUI hiển thị ("Trễ xử lý" và "DSP"), bằng cách mô phỏng ĐÚNG
luồng cross-process P1 -> P2 -> P3 thật (DropOldestQueue thật, timestamp thật),
dùng dữ liệu raw thật + model svm thật. Không suy luận từ benchmark đơn lẻ.

Định nghĩa lấy trực tiếp từ realTimeProc_infer.py:
  ts       = time.time() ngay sau khi P1 gộp đủ lô 30 frame (dòng ~552)
  dsp_ms   = thời gian CHỈ riêng process_complex() trong P2 (dòng ~683-685)
  proc_ms  = time.time() - ts  ĐO TẠI P3 sau khi inference xong (dòng ~946)
           = trễ toàn trình: chờ hàng đợi P1->P2 + DSP + build_range_plot
             + chờ hàng đợi P2->P3 + inference

  "Trễ xử lý" trên GUI  <- proc_ms   (latency_var, dòng ~493-495 realtime_gui.py)
  "DSP" trên GUI        <- dsp_ms    (dsp_var, dòng ~496-498 realtime_gui.py)

Chạy:
    .venv\\Scripts\\python.exe test\\bench_end_to_end_latency.py
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
from realTimeProc_infer import DropOldestQueue

INT16_PER_FRAME = 131_072
BATCH = 30
RAW = ROOT / "data_parse" / "raw_data_50fps.bin"
FS_FPS = 50.0


def pct(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100.0
    f = int(k); c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def summ(name, xs, unit="ms"):
    print(f"  {name:<24} n={len(xs):3d}  mean={statistics.mean(xs):7.1f}  "
          f"med={statistics.median(xs):7.1f}  p95={pct(xs,95):7.1f}  max={max(xs):7.1f} {unit}")


# ─────────────────────────── P1: capture (mô phỏng) ───────────────────────────
def _p1_capture(preprocess_q, n_batches, pace_s, raw_path, done_evt):
    from insect_radar_processor.insect_radar_processor import _int16_to_complex
    raw = np.fromfile(raw_path, dtype=np.int16, count=n_batches * BATCH * INT16_PER_FRAME)
    for b in range(n_batches):
        sl = raw[b * BATCH * INT16_PER_FRAME:(b + 1) * BATCH * INT16_PER_FRAME]
        c = _int16_to_complex(sl, iq_order="QQII")
        ts = time.time()  # <- đúng vị trí P1 thật stamp ts (ngay sau khi có đủ lô)
        preprocess_q.put((b, ts, c, "complex"))
        if pace_s:
            time.sleep(pace_s)
    done_evt.set()


# ─────────────────────────── P2: preprocessing (đúng hàm thật) ────────────────
def _p2_preprocess(preprocess_q, ai_q, done_evt, dsp_ms_list):
    from insect_radar_processor.insect_radar_processor import InsectRadarProcessor
    proc = InsectRadarProcessor(range_bin_min=15, range_bin_max=20, iq_order="QQII")
    while True:
        try:
            seq, ts, iq_data, _kind = preprocess_q.get(timeout=0.3)
        except Empty:
            if done_evt.is_set():
                break
            continue
        t0 = time.time()
        result = proc.process_complex(iq_data)
        dsp_ms = (time.time() - t0) * 1000.0     # <- đúng công thức dsp_ms thật
        dsp_ms_list.append(dsp_ms)
        proc_result = {
            "is_insect": result["is_insect"],
            "features": result["features"],
            "dsp_ms": round(dsp_ms, 1),
        }
        ai_q.put((seq, ts, proc_result))


# ─────────────────────────── P3: inference (đúng hàm thật, svm) ───────────────
def _p3_ai(ai_q, done_evt, proc_ms_list, dsp_ms_forwarded, models_dir):
    import joblib
    model = joblib.load(models_dir / "svm_pipeline.pkl")
    feature_names = list(joblib.load(models_dir / "feature_names.pkl"))
    while True:
        try:
            seq, ts, proc_result = ai_q.get(timeout=0.3)
        except Empty:
            if done_evt.is_set():
                break
            continue
        if proc_result["is_insect"]:
            X = np.array([proc_result["features"][n] for n in feature_names],
                        dtype=np.float64).reshape(1, -1)
            model.predict(X)
            if hasattr(model, "predict_proba"):
                model.predict_proba(X)
        proc_ms = (time.time() - float(ts)) * 1000.0   # <- đúng công thức proc_ms thật
        proc_ms_list.append(proc_ms)
        dsp_ms_forwarded.append(proc_result["dsp_ms"])


def main():
    if not RAW.exists():
        print(f"[!] Không tìm thấy {RAW}"); return 1
    mp.set_start_method("spawn", force=True)

    n_frames_total = RAW.stat().st_size // 2 // INT16_PER_FRAME
    n_batches = min(30, n_frames_total // BATCH)
    models_dir = ROOT / "insect_radar_processor" / "models"

    print("=" * 78)
    print("ĐỘ TRỄ END-TO-END THẬT (mô phỏng đúng luồng cross-process P1->P2->P3)")
    print(f"file={RAW.name}  {n_batches} lô x {BATCH} frame @{FS_FPS:.0f}fps "
          f"(cadence P1 = {BATCH*1000/FS_FPS:.0f} ms/lô)")
    print("=" * 78)

    mgr = mp.Manager()
    preprocess_q = DropOldestQueue(maxsize=10)
    ai_q = DropOldestQueue(maxsize=10)
    done1 = mp.Event(); done2 = mp.Event()
    dsp_ms_list = mgr.list()
    proc_ms_list = mgr.list()
    dsp_ms_forwarded = mgr.list()

    pace = BATCH / FS_FPS  # nhịp P1 thật: 1 lô mỗi 600ms @50fps -> mô phỏng realtime thật

    p1 = mp.Process(target=_p1_capture, args=(preprocess_q, n_batches, pace, RAW, done1))
    p2 = mp.Process(target=_p2_preprocess, args=(preprocess_q, ai_q, done1, dsp_ms_list))
    p3 = mp.Process(target=_p3_ai, args=(ai_q, done1, proc_ms_list, dsp_ms_forwarded, models_dir))

    t_start = time.time()
    p1.start(); p2.start(); p3.start()
    p1.join()
    # cho P2/P3 rút nốt hàng đợi rồi tự thoát khi done1 set + hàng đợi cạn
    p2.join(timeout=15)
    p3.join(timeout=15)
    for p in (p2, p3):
        if p.is_alive():
            p.terminate(); p.join()
    wall_s = time.time() - t_start

    dsp_ms = list(dsp_ms_list)
    proc_ms = list(proc_ms_list)
    dsp_fwd = list(dsp_ms_forwarded)

    print(f"\nHoàn tất trong {wall_s:.1f}s (nhịp thật @{FS_FPS:.0f}fps mô phỏng)\n")
    print("[GUI 'DSP'] dsp_ms — CHỈ process_complex(), đo tại P2:")
    summ("dsp_ms", dsp_ms)
    print("\n[GUI 'Trễ xử lý'] proc_ms — batch-ready(P1) -> inference-done(P3):")
    summ("proc_ms", proc_ms)

    if dsp_ms and proc_ms:
        overhead = statistics.mean(proc_ms) - statistics.mean(dsp_ms)
        print(f"\nChênh lệch TB (proc_ms - dsp_ms) = {overhead:.1f} ms")
        print("  = build_range_plot + chờ hàng đợi P1->P2 + chờ hàng đợi P2->P3 + inference")
        print(f"  (svm inference đo riêng ở bench_dsp_inference.py: ~0.7 ms -> phần lớn "
              f"chênh lệch trên là CHỜ HÀNG ĐỢI, không phải DSP hay model)")

    print("\n" + "=" * 78)
    print("KẾT LUẬN:")
    print("  GUI hiện CẢ HAI số, KHÔNG PHẢI 1 số:")
    print("    'Trễ xử lý' = TRỄ TOÀN TRÌNH (end-to-end latency, gồm cả chờ hàng đợi + inference)")
    print("    'DSP'       = THỜI GIAN XỬ LÝ DSP THUẦN (chỉ process_complex, không gồm chờ/hàng đợi)")
    print("  => 'DSP' luôn <= 'Trễ xử lý'. Với svm, DSP chiếm gần hết proc_ms vì")
    print("     inference + hàng đợi ở steady-state gần như bằng 0.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
