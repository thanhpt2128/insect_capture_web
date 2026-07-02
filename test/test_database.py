"""
test_database.py
================
Kiểm thử lớp ghi SQLite + nhật ký sự kiện trong realTimeProc_infer.py, không cần
phần cứng. Dùng thư mục tạm (monkeypatch LOG_DIR/DB_PATH/EVENT_LOG_PATH).

Bao phủ:
  D1 open_detection_db tạo đúng bảng 'detections' với đủ cột.
  D2 insert_detection ghi & đọc lại đúng; proba (dict) -> JSON; None giữ nguyên.
  D3 Logic "chỉ ghi khi nhãn ĐỔI" (tái hiện đúng điều kiện trong ai_worker_process):
     bỏ qua nhãn 'error', chỉ ghi khi khác nhãn ghi lần trước.
  D4 _enforce_db_size_cap: khi DB vượt ngưỡng -> xoá ~10% bản ghi CŨ nhất + log DB_TRIMMED.
  D5 log_event ghi 1 dòng có dạng "[EVENT] ... | detail".

Chạy:
    .venv\\Scripts\\python.exe test\\test_database.py
"""
import sys
import json
import tempfile
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import realTimeProc_infer as W


def _patch_to_tmp():
    """Trỏ LOG_DIR/DB_PATH/EVENT_LOG_PATH sang thư mục tạm; trả (tmpdir, restore)."""
    tmp = Path(tempfile.mkdtemp(prefix="pyradar_db_test_"))
    saved = (W.LOG_DIR, W.DB_PATH, W.EVENT_LOG_PATH, W.DB_MAX_BYTES)
    W.LOG_DIR = tmp
    W.DB_PATH = tmp / "detections.db"
    W.EVENT_LOG_PATH = tmp / "events.log"

    def restore():
        W.LOG_DIR, W.DB_PATH, W.EVENT_LOG_PATH, W.DB_MAX_BYTES = saved
        shutil.rmtree(tmp, ignore_errors=True)

    return tmp, restore


def test_d1_open_creates_schema():
    tmp, restore = _patch_to_tmp()
    try:
        conn = W.open_detection_db()
        assert conn is not None, "open_detection_db trả None"
        cols = [r[1] for r in conn.execute("PRAGMA table_info(detections)").fetchall()]
        for c in ("id", "ts", "label", "power", "score", "proba"):
            assert c in cols, f"thiếu cột '{c}' (có: {cols})"
        conn.close()
    finally:
        restore()


def test_d2_insert_and_readback():
    tmp, restore = _patch_to_tmp()
    try:
        conn = W.open_detection_db()
        W.insert_detection(conn, ts=123.5, label="bee", power=50000.0, score=0.87,
                           proba={"bee": 0.87, "fly": 0.13})
        W.insert_detection(conn, ts=124.0, label="background", power=100.0, score=None,
                           proba=None)
        rows = conn.execute("SELECT ts,label,power,score,proba FROM detections ORDER BY id").fetchall()
        assert len(rows) == 2, f"mong 2 bản ghi, có {len(rows)}"
        assert rows[0][1] == "bee" and abs(rows[0][0] - 123.5) < 1e-6
        proba = json.loads(rows[0][4])
        assert abs(proba["bee"] - 0.87) < 1e-6, "proba JSON sai"
        assert rows[1][3] is None and rows[1][4] is None, "None phải giữ nguyên NULL"
        conn.close()
    finally:
        restore()


def test_d3_write_only_on_label_change():
    """Tái hiện đúng điều kiện trong ai_worker_process:
       if cur_label and cur_label != 'error' and cur_label != last_db_label."""
    tmp, restore = _patch_to_tmp()
    try:
        conn = W.open_detection_db()
        # chuỗi nhãn mô phỏng nhiều lô liên tiếp
        seq_labels = ["background", "background", "bee", "bee", "bee",
                      "error", "fly", "fly", "background", "background", "bee"]
        last = None
        written = 0
        for lab in seq_labels:
            if lab and lab != "error" and lab != last:
                W.insert_detection(conn, ts=time.time(), label=lab, power=1.0,
                                   score=None, proba=None)
                written += 1
                last = lab
        # các mốc ĐỔI nhãn (bỏ error): background->bee->fly->background->bee = 5
        n = conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0]
        assert n == 5, f"mong 5 bản ghi (số lần đổi nhãn), có {n}"
        labels = [r[0] for r in conn.execute("SELECT label FROM detections ORDER BY id")]
        assert labels == ["background", "bee", "fly", "background", "bee"], labels
        conn.close()
    finally:
        restore()


def test_d4_size_cap_trims_oldest():
    tmp, restore = _patch_to_tmp()
    try:
        conn = W.open_detection_db()
        # ghi 400 bản ghi với ngưỡng khổng lồ (không cắt)
        for i in range(400):
            W.insert_detection(conn, ts=float(i), label="bee", power=1.0, score=0.5,
                               proba={"bee": 0.5, "fly": 0.5})
        before = conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0]
        assert before == 400, f"trước khi cắt phải có 400, có {before}"
        oldest_ts_before = conn.execute("SELECT MIN(ts) FROM detections").fetchone()[0]

        # hạ ngưỡng xuống dưới kích thước hiện tại -> lần enforce kế sẽ cắt ~10%
        W.DB_MAX_BYTES = 1
        W._enforce_db_size_cap(conn)
        after = conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0]
        assert after < before, f"phải cắt bớt (before={before}, after={after})"
        assert after >= before - before // 10 - 1, "chỉ nên cắt ~10%"
        oldest_ts_after = conn.execute("SELECT MIN(ts) FROM detections").fetchone()[0]
        assert oldest_ts_after > oldest_ts_before, "phải xoá các bản ghi CŨ nhất (ts nhỏ nhất)"

        # có ghi sự kiện DB_TRIMMED
        log_txt = W.EVENT_LOG_PATH.read_text(encoding="utf-8") if W.EVENT_LOG_PATH.exists() else ""
        assert "DB_TRIMMED" in log_txt, "thiếu log DB_TRIMMED"
        conn.close()
    finally:
        restore()


def test_d5_log_event_format():
    tmp, restore = _patch_to_tmp()
    try:
        W.log_event("CAPTURE_START", "com=COM5; stride=15")
        W.log_event("PLAIN")
        lines = W.EVENT_LOG_PATH.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2, f"mong 2 dòng, có {len(lines)}"
        assert lines[0].startswith("[CAPTURE_START]") and "com=COM5; stride=15" in lines[0]
        assert lines[1].startswith("[PLAIN]") and "|" not in lines[1], "dòng không detail không có '|'"
    finally:
        restore()


ALL_TESTS = [
    ("D1 open_detection_db tạo bảng đúng schema", test_d1_open_creates_schema),
    ("D2 insert & đọc lại (proba JSON, None)", test_d2_insert_and_readback),
    ("D3 chỉ ghi khi nhãn ĐỔI (bỏ 'error')", test_d3_write_only_on_label_change),
    ("D4 _enforce_db_size_cap cắt ~10% cũ nhất", test_d4_size_cap_trims_oldest),
    ("D5 log_event đúng định dạng", test_d5_log_event_format),
]


def main():
    print("=" * 66)
    print("TEST ghi SQLite + nhật ký sự kiện (realTimeProc_infer.py)")
    print("=" * 66)
    failed = 0
    for name, fn in ALL_TESTS:
        t0 = time.perf_counter()
        try:
            fn()
            print(f"  [PASS] {name}   ({(time.perf_counter()-t0)*1000:.0f}ms)")
        except AssertionError as exc:
            failed += 1
            print(f"  [FAIL] {name}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"  [ERROR] {name}: {type(exc).__name__}: {exc}")
    print("=" * 66)
    print("KẾT QUẢ:", "TẤT CẢ PASS ✅" if failed == 0 else f"{failed} FAIL ❌")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
