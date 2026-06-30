"""
test_drop_oldest_queue.py
=========================
Kiểm thử tính đúng đắn của ``DropOldestQueue`` trong ``realTimeProc_infer.py``.

Đặc tả cần thỏa mãn:
  S1 FIFO khi chưa đầy   — get trả ra đúng thứ tự đã put.
  S2 Bounded             — số phần tử tồn ≤ maxsize (không phình bộ nhớ).
  S3 Không reorder       — chuỗi nhận được luôn tăng đơn điệu theo seq.
  S4 Drop-OLDEST         — khi đầy, phần tử bị bỏ là phần tử CŨ nhất; phần tử
                           MỚI nhất luôn được giữ (kiểm cả single-process burst
                           lẫn cross-process bão hòa với payload 16MB).

Vì sao cần test kỹ: ``multiprocessing.Queue`` đẩy dữ liệu qua feeder thread bất
đồng bộ, nên cách evict sai (get non-blocking) sẽ drop nhầm phần tử MỚI. Bộ test
này khóa lại hành vi drop-oldest đúng để tránh hồi quy.

Chạy:
    .venv\\Scripts\\python.exe test\\test_drop_oldest_queue.py
Thoát mã 0 nếu tất cả PASS, 1 nếu có FAIL.
"""
import sys
import time
import statistics
import multiprocessing as mp
from pathlib import Path
from queue import Empty

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from realTimeProc_infer import DropOldestQueue  # class THẬT đang dùng trong pipeline

import numpy as np


# ───────────────────────────── tiện ích ─────────────────────────────
def drain(q):
    out = []
    while True:
        try:
            out.append(q.get(block=False))
        except Empty:
            break
    return out


def _producer(q, m, done_evt, pace_s, payload_bytes):
    blob = np.zeros(payload_bytes // 8, dtype=np.complex64) if payload_bytes else None
    for seq in range(m):
        q.put((seq, time.perf_counter()) if blob is None else (seq, time.perf_counter(), blob))
        if pace_s:
            time.sleep(pace_s)
    done_evt.set()


def _consumer(q, done_evt, get_delay_s, recv_list):
    while True:
        try:
            item = q.get(timeout=0.3)
        except Empty:
            if done_evt.is_set():
                break
            continue
        recv_list.append(item[0])
        if get_delay_s:
            time.sleep(get_delay_s)


def _cross_process(maxsize, m, pace, gdelay, payload, runs):
    """Trả về (recv_mean, pct_no_reorder, pct_keep_newest)."""
    newest = mono = 0
    counts = []
    for _ in range(runs):
        q = DropOldestQueue(maxsize=maxsize)
        done = mp.Event()
        mgr = mp.Manager()
        recv = mgr.list()
        pc = mp.Process(target=_consumer, args=(q, done, gdelay, recv))
        pr = mp.Process(target=_producer, args=(q, m, done, pace, payload))
        pc.start(); pr.start(); pr.join(); pc.join()
        r = list(recv)
        counts.append(len(r))
        if all(r[i] < r[i + 1] for i in range(len(r) - 1)):
            mono += 1
        if r and max(r) == m - 1:
            newest += 1
        mgr.shutdown()
    return statistics.mean(counts), 100.0 * mono / runs, 100.0 * newest / runs


# ───────────────────────────── các test ─────────────────────────────
def test_s1_fifo_when_not_full():
    q = DropOldestQueue(maxsize=100)
    for i in range(50):
        q.put(i)
    time.sleep(0.1)
    got = drain(q)
    assert got == list(range(50)), f"FIFO sai: {got[:10]}..."


def test_s2_bounded_and_s4_keep_newest_burst():
    """Burst single-process: put 0..N-1 vào maxsize=Q, drain. Kept≈Q (bounded),
    luôn giữ phần tử mới nhất và là đuôi liền mạch {N-k..N-1}."""
    for Q, N in [(2, 8), (4, 50), (8, 200)]:
        trials = 200
        kept, newest_ok, suffix_ok = [], 0, 0
        for _ in range(trials):
            q = DropOldestQueue(maxsize=Q)
            for i in range(N):
                q.put(i)
            time.sleep(0.005)
            got = sorted(drain(q))
            kept.append(len(got))
            if got and max(got) == N - 1:
                newest_ok += 1
            if got and got == list(range(N - len(got), N)):
                suffix_ok += 1
        mean_kept = statistics.mean(kept)
        assert mean_kept <= Q + 0.01, f"Q={Q}: bounded sai, kept={mean_kept}"
        assert newest_ok == trials, f"Q={Q},N={N}: giữ-newest {newest_ok}/{trials}"
        assert suffix_ok == trials, f"Q={Q},N={N}: đuôi-liền-mạch {suffix_ok}/{trials}"


def test_s3_s4_cross_process_saturated_small():
    _, no_reorder, keep_newest = _cross_process(
        maxsize=4, m=200, pace=0.0005, gdelay=0.003, payload=0, runs=10)
    assert no_reorder == 100.0, f"reorder! no_reorder={no_reorder}%"
    assert keep_newest == 100.0, f"không giao được newest: {keep_newest}%"


def test_s3_s4_cross_process_saturated_16mb():
    """Ca khó nhất: payload 16MB (đúng item P1->P2), consumer chậm -> bão hòa."""
    _, no_reorder, keep_newest = _cross_process(
        maxsize=4, m=80, pace=0.0, gdelay=0.010, payload=16 * 1024 * 1024, runs=8)
    assert no_reorder == 100.0, f"reorder! no_reorder={no_reorder}%"
    assert keep_newest == 100.0, f"không giao được newest (16MB): {keep_newest}%"


ALL_TESTS = [
    ("S1  FIFO khi chưa đầy", test_s1_fifo_when_not_full),
    ("S2+S4 bounded + giữ-newest (burst)", test_s2_bounded_and_s4_keep_newest_burst),
    ("S3+S4 cross-proc bão hòa (nhỏ)", test_s3_s4_cross_process_saturated_small),
    ("S3+S4 cross-proc bão hòa (16MB)", test_s3_s4_cross_process_saturated_16mb),
]


def main():
    mp.set_start_method("spawn", force=True)
    print("=" * 70)
    print("TEST DropOldestQueue (class thật từ realTimeProc_infer.py)")
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
        except Exception as exc:  # pragma: no cover
            failed += 1
            print(f"  [ERROR] {name}: {type(exc).__name__}: {exc}")
    print("=" * 70)
    print("KẾT QUẢ:", "TẤT CẢ PASS ✅" if failed == 0 else f"{failed} FAIL ❌")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
