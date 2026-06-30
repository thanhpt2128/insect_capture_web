"""
bench_dsp_inference.py
======================
Benchmark hiệu năng / thời gian của pipeline thật, KHÔNG cần phần cứng, dùng dữ
liệu raw thật trong ``data_parse/``. Đo từng tầng và đối chiếu ngân sách thời
gian thực để kết luận pipeline có bắt kịp tốc độ khung hình hay không.

Đo:
  P1  decode int16->complex /frame, build IQ preview /frame, concat 30 frame /lô
  P2  InsectRadarProcessor.process_complex (DSP) /lô  + build range_plot
  P3  inference svm / rf /lô  (xgb nếu cài được)
  Soak: 150 lô liên tục -> kiểm rò rỉ RAM + trôi độ trễ

Chạy:
    .venv\\Scripts\\python.exe test\\bench_dsp_inference.py
"""
import sys
import time
import gc
import os
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import joblib

from insect_radar_processor.insect_radar_processor import (
    InsectRadarProcessor, _int16_to_complex,
)

INT16_PER_FRAME = 131_072
BATCH = 30
FS_FPS = 50.0  # file 50fps -> 30 frame = 600 ms ngân sách/lô (steady-state)
MODELS_DIR = ROOT / "insect_radar_processor" / "models"
RAW = ROOT / "data_parse" / "raw_data_50fps.bin"


def pct(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100.0
    f = int(k); c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def summ(name, xs, unit="ms"):
    print(f"  {name:<28} n={len(xs):3d}  mean={statistics.mean(xs):7.1f}  "
          f"med={statistics.median(xs):7.1f}  p95={pct(xs,95):7.1f}  max={max(xs):7.1f} {unit}")


def build_iq_preview(cplx, n=256):
    s = cplx.ravel()
    idx = np.linspace(0, s.size - 1, num=n, dtype=np.int64)
    v = s[idx]
    i = np.real(v).astype(np.float32); q = np.imag(v).astype(np.float32)
    return ([round(float(x), 3) for x in i.tolist()],
            [round(float(x), 3) for x in q.tolist()])


def build_range_plot(result):
    viz = result.get("viz") or {}
    rtm = np.asarray(viz.get("rtm_db"), dtype=np.float32)
    _ = [round(float(v), 3) for v in rtm.mean(axis=0).ravel().tolist()]
    _ = [[round(float(v), 3) for v in np.asarray(r).ravel().tolist()] for r in rtm.tolist()]


def main():
    if not RAW.exists():
        print(f"[!] Không tìm thấy {RAW}. Bỏ qua benchmark.")
        return 0
    try:
        import psutil
        proc_ps = psutil.Process(os.getpid())
    except Exception:
        proc_ps = None

    n_frames_total = RAW.stat().st_size // 2 // INT16_PER_FRAME
    n_batches = min(40, n_frames_total // BATCH)
    raw = np.fromfile(RAW, dtype=np.int16, count=n_batches * BATCH * INT16_PER_FRAME)
    frames = raw.reshape(n_batches * BATCH, INT16_PER_FRAME)
    print("=" * 78)
    print(f"BENCHMARK  file={RAW.name}  frames={n_frames_total}  dùng {n_batches} lô x {BATCH}")
    print("=" * 78)

    # ── P1 ────────────────────────────────────────────────────────────────
    print("\n[P1] CAPTURE: decode/frame + IQ preview/frame + concat/lô")
    decode_ms, preview_ms, concat_ms = [], [], []
    for b in range(n_batches):
        iq_batch = []
        for fi in range(b * BATCH, (b + 1) * BATCH):
            t = time.perf_counter(); c = _int16_to_complex(frames[fi], iq_order="QQII")
            decode_ms.append((time.perf_counter() - t) * 1000)
            t = time.perf_counter(); build_iq_preview(c)
            preview_ms.append((time.perf_counter() - t) * 1000)
            iq_batch.append(c)
        t = time.perf_counter(); np.concatenate(iq_batch).astype(np.complex64, copy=False)
        concat_ms.append((time.perf_counter() - t) * 1000)
    summ("decode int16->cplx /frame", decode_ms)
    summ("build IQ preview /frame", preview_ms)
    summ("concat 30 frame /lô", concat_ms)
    p1_per_frame = statistics.mean(decode_ms) + statistics.mean(preview_ms)
    print(f"  -> P1 Python/frame ≈ {p1_per_frame:.2f} ms; ngân sách @{FS_FPS:.0f}fps = {1000/FS_FPS:.1f} ms/frame")

    # ── P2 ────────────────────────────────────────────────────────────────
    print("\n[P2] PREPROCESSING: process_complex (DSP) + build_range_plot")
    proc = InsectRadarProcessor(range_bin_min=15, range_bin_max=20, iq_order="QQII")
    dsp_ms, plot_ms, insect = [], [], 0
    results = []
    for b in range(n_batches):
        sl = raw[b * BATCH * INT16_PER_FRAME:(b + 1) * BATCH * INT16_PER_FRAME]
        c = _int16_to_complex(sl, iq_order="QQII")
        t = time.perf_counter(); res = proc.process_complex(c)
        dsp_ms.append((time.perf_counter() - t) * 1000)
        t = time.perf_counter(); build_range_plot(res)
        plot_ms.append((time.perf_counter() - t) * 1000)
        if res["is_insect"]:
            insect += 1
        results.append(res)
    summ("process_complex (DSP)", dsp_ms)
    summ("build_range_plot", plot_ms)
    print(f"  -> insect: {insect}/{n_batches}; ngân sách/lô @{FS_FPS:.0f}fps = {BATCH*1000/FS_FPS:.0f} ms")

    # ── P3 ────────────────────────────────────────────────────────────────
    print("\n[P3] INFERENCE")
    feature_names = list(joblib.load(MODELS_DIR / "feature_names.pkl"))
    insect_feats = [r["features"] for r in results if r["is_insect"]]
    for mname, fn in [("svm", "svm_pipeline.pkl"), ("rf", "randomforest.pkl"), ("xgb", "xgboost.pkl")]:
        try:
            model = joblib.load(MODELS_DIR / fn)
        except Exception as exc:
            print(f"  [{mname}] không load được ({type(exc).__name__}) -> bỏ qua")
            continue
        if not insect_feats:
            print(f"  [{mname}] không có lô insect để đo"); continue
        inf_ms = []
        for feats in insect_feats:
            X = np.array([feats[n] for n in feature_names], dtype=np.float64).reshape(1, -1)
            t = time.perf_counter()
            model.predict(X)
            if hasattr(model, "predict_proba"):
                model.predict_proba(X)
            inf_ms.append((time.perf_counter() - t) * 1000)
        summ(f"[{mname}] predict /lô", inf_ms)

    # ── Soak ──────────────────────────────────────────────────────────────
    print("\n[SOAK] 150 lô process_complex liên tục (rò rỉ RAM + trôi độ trễ)")
    lat, rss = [], []
    for i in range(150):
        sl = raw[(i % n_batches) * BATCH * INT16_PER_FRAME:((i % n_batches) + 1) * BATCH * INT16_PER_FRAME]
        c = _int16_to_complex(sl, iq_order="QQII")
        t = time.perf_counter(); proc.process_complex(c); lat.append((time.perf_counter() - t) * 1000)
        if proc_ps is not None and i % 10 == 0:
            gc.collect(); rss.append(proc_ps.memory_info().rss / 1e6)
    print(f"  lat: 20 lô đầu={statistics.mean(lat[:20]):.1f}ms  20 lô cuối={statistics.mean(lat[-20:]):.1f}ms  "
          f"drift={statistics.mean(lat[-20:])-statistics.mean(lat[:20]):+.1f}ms")
    if rss:
        print(f"  RSS: đầu={rss[0]:.0f}MB  cuối={rss[-1]:.0f}MB  tăng={rss[-1]-rss[0]:+.0f}MB")

    # ── Kết luận ngân sách ────────────────────────────────────────────────
    budget = BATCH * 1000 / FS_FPS
    slow = max(statistics.mean(dsp_ms), p1_per_frame * BATCH)
    print("\n" + "=" * 78)
    print("KẾT LUẬN NGÂN SÁCH (steady-state, các tầng chạy song song/pipelined)")
    print("=" * 78)
    print(f"  ngân sách/lô (30 frame @{FS_FPS:.0f}fps) = {budget:.0f} ms")
    print(f"  P1 30×(decode+preview)               = {p1_per_frame*BATCH:.0f} ms")
    print(f"  P2 DSP mean / p95                    = {statistics.mean(dsp_ms):.0f} / {pct(dsp_ms,95):.0f} ms")
    print(f"  tầng chậm nhất (binding)             = {slow:.0f} ms")
    print(f"  -> {'BẮT KỊP' if slow < budget else 'KHÔNG kịp -> drop lô'} @{FS_FPS:.0f}fps  "
          f"(@100fps ngân sách = {BATCH*1000/100:.0f} ms)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
