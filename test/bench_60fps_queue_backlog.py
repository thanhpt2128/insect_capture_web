"""
bench_60fps_queue_backlog.py
=============================
Với cfg thật đang dùng (``configFiles/cfg128_128_100fps.cfg`` — dù tên file ghi
100fps, frameCfg thực đặt periodicity 16.67 ms = **60 fps**, hình học khớp
131072 int16/frame với model), mô phỏng ĐÚNG luồng cross-process P1->P2->P3 ở
nhịp 60fps thật (30 frame/lô -> cadence 500 ms/lô) và trả lời:

  1. dsp_ms / proc_ms ở 60fps có khác 50fps không?
  2. Hàng đợi (preprocess_queue, ai_queue) có tồn đọng dữ liệu (backlog) không?
  3. Có lô nào bị DropOldestQueue drop không? (so P1 tạo ra vs P3 nhận được)

Chạy:
    .venv\\Scripts\\python.exe test\\bench_60fps_queue_backlog.py
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
from realTimeProc_infer import DropOldestQueue, _frame_int16_from_cfg

INT16_PER_FRAME = 131_072
BATCH = 30
RAW = ROOT / "data_parse" / "raw_data_50fps.bin"   # nội dung mẫu; nhịp bơm mô phỏng theo CFG thật
CFG = ROOT / "configFiles" / "cfg128_128_100fps.cfg"


def pct(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100.0
    f = int(k); c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def summ(name, xs, unit="ms"):
    print(f"  {name:<24} n={len(xs):3d}  mean={statistics.mean(xs):7.1f}  "
          f"med={statistics.median(xs):7.1f}  p95={pct(xs,95):7.1f}  max={max(xs):7.1f} {unit}")


def _p1_capture(preprocess_q, n_batches, pace_s, raw_path, done_evt, produced_ctr,
                 max_qsize_seen):
    from insect_radar_processor.insect_radar_processor import _int16_to_complex
    raw = np.fromfile(raw_path, dtype=np.int16, count=n_batches * BATCH * INT16_PER_FRAME)
    for b in range(n_batches):
        sl = raw[b * BATCH * INT16_PER_FRAME:(b + 1) * BATCH * INT16_PER_FRAME]
        c = _int16_to_complex(sl, iq_order="QQII")
        try:
            qs = preprocess_q.queue.qsize()
            with max_qsize_seen.get_lock():
                if qs > max_qsize_seen.value:
                    max_qsize_seen.value = qs
        except Exception:
            pass
        ts = time.time()
        preprocess_q.put((b, ts, c, "complex"))
        with produced_ctr.get_lock():
            produced_ctr.value += 1
        if pace_s:
            time.sleep(pace_s)
    done_evt.set()


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
        dsp_ms_list.append((time.time() - t0) * 1000.0)
        proc_result = {"is_insect": result["is_insect"], "features": result["features"]}
        ai_q.put((seq, ts, proc_result))


def _p3_ai(ai_q, done_evt, proc_ms_list, received_ctr, models_dir):
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
        proc_ms_list.append((time.time() - float(ts)) * 1000.0)
        with received_ctr.get_lock():
            received_ctr.value += 1


def main():
    if not RAW.exists() or not CFG.exists():
        print("[!] Thiếu file raw hoặc cfg"); return 1
    mp.set_start_method("spawn", force=True)

    frame_int16 = _frame_int16_from_cfg(str(CFG))
    print("=" * 78)
    print(f"CFG: {CFG.name}  ->  int16/frame suy ra = {frame_int16} "
          f"({'KHỚP' if frame_int16 == INT16_PER_FRAME else 'KHÔNG KHỚP'} model)")
    FS_FPS = 60.0  # frameCfg periodicity thật của cfg này = 16.67ms = 60fps (xác nhận qua radar_metrics)
    cadence_ms = BATCH * 1000 / FS_FPS
    print(f"Nhịp mô phỏng: {FS_FPS:.0f} fps -> cadence = {cadence_ms:.0f} ms/lô (30 frame)")
    print("=" * 78)

    n_frames_total = RAW.stat().st_size // 2 // INT16_PER_FRAME
    n_batches = min(30, n_frames_total // BATCH)
    models_dir = ROOT / "insect_radar_processor" / "models"

    mgr = mp.Manager()
    preprocess_q = DropOldestQueue(maxsize=10)
    ai_q = DropOldestQueue(maxsize=10)
    done1 = mp.Event()
    dsp_ms_list = mgr.list()
    proc_ms_list = mgr.list()
    produced_ctr = mp.Value("i", 0)
    received_ctr = mp.Value("i", 0)
    max_qsize_seen = mp.Value("i", 0)

    pace = BATCH / FS_FPS

    p1 = mp.Process(target=_p1_capture, args=(preprocess_q, n_batches, pace, RAW, done1,
                                              produced_ctr, max_qsize_seen))
    p2 = mp.Process(target=_p2_preprocess, args=(preprocess_q, ai_q, done1, dsp_ms_list))
    p3 = mp.Process(target=_p3_ai, args=(ai_q, done1, proc_ms_list, received_ctr, models_dir))

    t_start = time.time()
    p1.start(); p2.start(); p3.start()
    p1.join()
    p2.join(timeout=15); p3.join(timeout=15)
    for p in (p2, p3):
        if p.is_alive():
            p.terminate(); p.join()
    wall_s = time.time() - t_start

    dsp_ms = list(dsp_ms_list)
    proc_ms = list(proc_ms_list)
    produced = produced_ctr.value
    received = received_ctr.value
    max_qsize = max_qsize_seen.value

    print(f"\nHoàn tất trong {wall_s:.1f}s\n")
    print("[GUI 'DSP'] dsp_ms ở 60fps:")
    summ("dsp_ms", dsp_ms)
    print("\n[GUI 'Trễ xử lý'] proc_ms ở 60fps:")
    summ("proc_ms", proc_ms)

    print("\n" + "-" * 78)
    print("HÀNG ĐỢI (preprocess_queue, P1->P2):")
    print(f"  P1 tạo ra   : {produced} lô")
    print(f"  P3 nhận được: {received} lô")
    dropped = produced - received
    print(f"  BỊ DROP     : {dropped} lô ({'CÓ drop-oldest kích hoạt' if dropped>0 else 'KHÔNG lô nào bị drop'})")
    print(f"  qsize lớn nhất P1 thấy trước khi put (preprocess_queue): {max_qsize} "
          f"(0/1 = P2 luôn rút kịp, không tồn đọng; >1 = bắt đầu dồn ứ)")

    budget = cadence_ms
    slow = statistics.mean(dsp_ms)
    margin = budget - pct(dsp_ms, 95)
    print("\n" + "=" * 78)
    print("KẾT LUẬN @60fps:")
    print(f"  ngân sách/lô = {budget:.0f} ms   DSP p95 = {pct(dsp_ms,95):.0f} ms   "
          f"biên an toàn (budget - p95) = {margin:.0f} ms")
    print(f"  -> {'BẮT KỊP, còn dư biên an toàn' if margin > 0 else 'CHẠM/VƯỢT ngân sách -> rủi ro dồn ứ'}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
