"""
test_sliding_window.py
======================
Kiểm chứng logic cửa sổ trượt (sliding window) trong capture_worker_process của
realTimeProc_infer.py, mà không cần phần cứng.

Tái hiện CHÍNH XÁC khối gom-lô của vòng lặp capture:
    iq_batch.append(frame)
    if len(iq_batch) >= batch_frames:
        window = iq_batch[:batch_frames]
        del iq_batch[:stride]
        -> phát 1 cửa sổ

Khẳng định:
  W1 Mỗi cửa sổ đúng batch_frames frame.
  W2 2 cửa sổ liên tiếp lệch nhau đúng 'stride' frame (frame đầu window k+1
     = frame đầu window k + stride).
  W3 Chồng lấp = batch_frames - stride.
  W4 stride == batch_frames -> tumbling (không chồng lấp), khớp hành vi cũ.
  W5 Quy tắc resolve stride khớp main(): stride<=0 hoặc >numframes -> = numframes.

Chạy:
    .venv\\Scripts\\python.exe test\\test_sliding_window.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def resolve_stride(numframes, stride):
    """Giống hệt quy tắc trong main() và capture_worker_process."""
    nf = max(1, int(numframes))
    s = int(stride or 0)
    if s <= 0 or s > nf:
        s = nf
    return s


def simulate(batch_frames, stride, n_frames):
    """Chạy đúng khối gom-lô của capture loop, trả list cửa sổ (mỗi cửa sổ là
    list chỉ số frame)."""
    iq_batch = []
    windows = []
    for f in range(n_frames):
        iq_batch.append(f)
        if len(iq_batch) >= batch_frames:
            windows.append(list(iq_batch[:batch_frames]))
            del iq_batch[:stride]
    return windows


def test_w1_window_size():
    for stride in (30, 15, 10, 1):
        wins = simulate(30, resolve_stride(30, stride), 120)
        assert wins, f"stride={stride}: không tạo cửa sổ nào"
        for w in wins:
            assert len(w) == 30, f"stride={stride}: cửa sổ có {len(w)} frame (mong 30)"


def test_w2_stride_step_and_w3_overlap():
    for stride in (30, 20, 15, 10, 1):
        s = resolve_stride(30, stride)
        wins = simulate(30, s, 200)
        for k in range(len(wins) - 1):
            step = wins[k + 1][0] - wins[k][0]
            assert step == s, f"stride={s}: bước giữa 2 cửa sổ = {step} (mong {s})"
            # chồng lấp = số frame chung
            overlap = len(set(wins[k]) & set(wins[k + 1]))
            assert overlap == 30 - s, f"stride={s}: chồng lấp {overlap} (mong {30 - s})"


def test_w4_tumbling_no_overlap():
    wins = simulate(30, resolve_stride(30, 0), 120)   # 0 -> tumbling
    # tumbling: các cửa sổ rời nhau hoàn toàn
    assert wins[0] == list(range(0, 30))
    assert wins[1] == list(range(30, 60))
    for k in range(len(wins) - 1):
        assert set(wins[k]).isdisjoint(wins[k + 1]), "tumbling không được chồng lấp"


def test_w5_resolve_rules():
    assert resolve_stride(30, 0) == 30      # mặc định -> tumbling
    assert resolve_stride(30, -5) == 30     # âm -> tumbling
    assert resolve_stride(30, 99) == 30     # > numframes -> kẹp về numframes
    assert resolve_stride(30, 15) == 15     # hợp lệ
    assert resolve_stride(30, 1) == 1       # hợp lệ (cực đại chồng lấp)


def test_w6_example_50pct_overlap():
    """Đúng ví dụ user hỏi: numframes=30, stride=15 -> 30, rồi 15+15..."""
    wins = simulate(30, 15, 90)
    assert wins[0] == list(range(0, 30)),  f"{wins[0][:3]}..."
    assert wins[1] == list(range(15, 45)), f"{wins[1][:3]}..."
    assert wins[2] == list(range(30, 60)), f"{wins[2][:3]}..."
    # cửa sổ 1 giữ 15 frame cuối (15..29) của cửa sổ 0 + 15 frame mới (30..44)
    assert wins[1][:15] == list(range(15, 30)), "phải giữ 15 frame đuôi của cửa sổ trước"
    assert wins[1][15:] == list(range(30, 45)), "phải ghép 15 frame mới"


ALL_TESTS = [
    ("W1 mỗi cửa sổ đúng batch_frames", test_w1_window_size),
    ("W2+W3 bước = stride & chồng lấp = nf-stride", test_w2_stride_step_and_w3_overlap),
    ("W4 tumbling không chồng lấp (hành vi cũ)", test_w4_tumbling_no_overlap),
    ("W5 quy tắc resolve stride", test_w5_resolve_rules),
    ("W6 ví dụ 30 -> 15+15 (chồng lấp 50%)", test_w6_example_50pct_overlap),
]


def main():
    print("=" * 66)
    print("TEST sliding window (capture_worker_process)")
    print("=" * 66)
    failed = 0
    for name, fn in ALL_TESTS:
        t0 = time.perf_counter()
        try:
            fn()
            print(f"  [PASS] {name}   ({(time.perf_counter()-t0)*1000:.1f}ms)")
        except AssertionError as exc:
            failed += 1
            print(f"  [FAIL] {name}: {exc}")
    print("=" * 66)
    print("KẾT QUẢ:", "TẤT CẢ PASS ✅" if failed == 0 else f"{failed} FAIL ❌")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
