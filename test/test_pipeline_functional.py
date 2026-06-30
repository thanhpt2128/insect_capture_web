"""
test_pipeline_functional.py
===========================
Kiểm thử chức năng pipeline DSP + AI mà KHÔNG cần phần cứng, dùng dữ liệu raw
thật trong ``data_parse/`` và model thật trong ``insect_radar_processor/models/``.

Bao phủ:
  F1 Alignment feature_names.pkl  ==  InsectRadarProcessor.get_feature_names()
  F2 process_complex trả đủ schema; lô insect có đủ 58 feature hữu hạn;
     viz.rtm_db đúng shape [n_frames, n_range_bins].
  F3 Tương đương IQ order: QQII.real==IIQQ.imag và QQII.imag==IIQQ.real
     (cùng byte, chỉ hoán I/Q) — bảo chứng quyết định decode realtime.
  F4 _frame_int16_from_cfg suy ra đúng 131072 int16/frame cho cfg 128x128x4RX.
  F5 Smoke inference: svm.predict trả nhãn hợp lệ, proba tổng ≈ 1.

Chạy:
    .venv\\Scripts\\python.exe test\\test_pipeline_functional.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import joblib

from insect_radar_processor.insect_radar_processor import (
    InsectRadarProcessor, _int16_to_complex,
)
from realTimeProc_infer import _frame_int16_from_cfg

INT16_PER_FRAME = 131_072
BATCH = 30
RAW_CANDIDATES = [
    ROOT / "data_parse" / "raw_data_50fps.bin",
    ROOT / "data_parse" / "raw_data_50fps_3.bin",
]
MODELS_DIR = ROOT / "insect_radar_processor" / "models"


def _find_raw():
    for p in RAW_CANDIDATES:
        if p.exists() and p.stat().st_size >= BATCH * INT16_PER_FRAME * 2:
            return p
    return None


def _load_insect_batch(proc):
    """Tìm 1 lô có côn trùng trong file raw để có features cho test inference."""
    raw_path = _find_raw()
    if raw_path is None:
        return None, None
    n_frames = raw_path.stat().st_size // 2 // INT16_PER_FRAME
    for b in range(min(40, n_frames // BATCH)):
        sl = np.fromfile(raw_path, dtype=np.int16,
                         count=BATCH * INT16_PER_FRAME,
                         offset=b * BATCH * INT16_PER_FRAME * 2)
        res = proc.process_complex(_int16_to_complex(sl, iq_order="QQII"))
        if res["is_insect"]:
            return res, sl
    # không có lô insect: trả lô cuối cùng để vẫn test được schema
    return res, sl


# ───────────────────────────── các test ─────────────────────────────
def test_f1_feature_alignment():
    proc = InsectRadarProcessor(range_bin_min=15, range_bin_max=20, iq_order="QQII")
    model_names = list(joblib.load(MODELS_DIR / "feature_names.pkl"))
    proc_names = proc.get_feature_names()
    assert len(model_names) == 58, f"feature_names.pkl có {len(model_names)} (mong 58)"
    assert proc_names == model_names, (
        "Thứ tự/feature processor KHÁC model: "
        f"missing={set(model_names)-set(proc_names)} extra={set(proc_names)-set(model_names)}")


def test_f2_process_complex_schema():
    raw_path = _find_raw()
    assert raw_path is not None, "Không tìm thấy file raw 128x128 trong data_parse/ để test"
    proc = InsectRadarProcessor(range_bin_min=15, range_bin_max=20, iq_order="QQII")
    sl = np.fromfile(raw_path, dtype=np.int16, count=BATCH * INT16_PER_FRAME)
    res = proc.process_complex(_int16_to_complex(sl, iq_order="QQII"))

    for key in ("is_insect", "power_threshold", "features", "viz"):
        assert key in res, f"thiếu key '{key}' trong kết quả"
    rtm = np.asarray(res["viz"]["rtm_db"])
    assert rtm.shape == (BATCH, proc.cfg.n_range_bins_keep), f"rtm_db shape {rtm.shape}"
    if res["is_insect"]:
        feats = res["features"]
        names = proc.get_feature_names()
        assert set(feats.keys()) == set(names), "features dict không khớp tên"
        assert all(np.isfinite(float(feats[n])) for n in names), "có feature NaN/inf"


def test_f3_iq_order_equivalence():
    rng = np.random.default_rng(0)
    raw = rng.integers(-2000, 2000, size=4 * 1000, dtype=np.int16)
    qq = _int16_to_complex(raw, iq_order="QQII")
    ii = _int16_to_complex(raw, iq_order="IIQQ")
    assert np.array_equal(qq.real, ii.imag), "QQII.real phải == IIQQ.imag"
    assert np.array_equal(qq.imag, ii.real), "QQII.imag phải == IIQQ.real"


def test_f4_cfg_geometry():
    cfg = ROOT / "configFiles" / "cfg128_128_100fps.cfg"
    if not cfg.exists():
        print("    (bỏ qua F4: không có cfg128_128_100fps.cfg)")
        return
    n = _frame_int16_from_cfg(str(cfg))
    assert n == INT16_PER_FRAME, f"cfg 128x128 suy ra {n}, mong {INT16_PER_FRAME}"


def test_f5_inference_smoke():
    model = joblib.load(MODELS_DIR / "svm_pipeline.pkl")
    feature_names = list(joblib.load(MODELS_DIR / "feature_names.pkl"))
    proc = InsectRadarProcessor(range_bin_min=15, range_bin_max=20, iq_order="QQII")
    res, _ = _load_insect_batch(proc)
    assert res is not None, "Không có dữ liệu raw để test inference"
    if not res["is_insect"]:
        print("    (cảnh báo F5: không tìm thấy lô insect; chỉ kiểm model load được)")
        return
    X = np.array([res["features"][n] for n in feature_names], dtype=np.float64).reshape(1, -1)
    pred = model.predict(X)[0]
    classes = [str(c) for c in model.classes_]
    assert str(pred) in classes, f"nhãn '{pred}' không thuộc {classes}"
    if hasattr(model, "predict_proba"):
        p = model.predict_proba(X)[0]
        assert abs(float(np.sum(p)) - 1.0) < 1e-6, "proba không tổng về 1"


ALL_TESTS = [
    ("F1 feature alignment (58, đúng thứ tự)", test_f1_feature_alignment),
    ("F2 process_complex schema + viz shape", test_f2_process_complex_schema),
    ("F3 tương đương IQ order QQII<->IIQQ", test_f3_iq_order_equivalence),
    ("F4 _frame_int16_from_cfg = 131072", test_f4_cfg_geometry),
    ("F5 smoke inference svm", test_f5_inference_smoke),
]


def main():
    print("=" * 70)
    print("TEST chức năng pipeline DSP + AI (không cần phần cứng)")
    print("=" * 70)
    failed = 0
    for name, fn in ALL_TESTS:
        t0 = time.perf_counter()
        try:
            fn()
            print(f"  [PASS] {name}   ({time.perf_counter()-t0:.1f}s)")
        except AssertionError as exc:
            failed += 1
            print(f"  [FAIL] {name}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"  [ERROR] {name}: {type(exc).__name__}: {exc}")
    print("=" * 70)
    print("KẾT QUẢ:", "TẤT CẢ PASS ✅" if failed == 0 else f"{failed} FAIL ❌")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
