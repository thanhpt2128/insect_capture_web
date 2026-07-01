"""
bench_queue_size_recovery.py
=============================
Kiểm chứng comment trong realTimeProc_infer.py (dòng ~1188-1191):

    "Size=1: any stage stall self-heals via overflow-drop (newest kept), so a
    transient freeze cannot inject a permanent 1-batch phase lag. Size>=2 lets
    a stall leave a stale batch that never drains (stages are exactly
    rate-matched to capture), pinning latency at depth x ~565ms."

Nhưng CLI mặc định hiện tại là --preprocess-queue-size=10 và --ai-queue-size=10
(dòng 1192-1193) — TRÁI với khuyến nghị "Size=1" trong chính comment đó.

Test này: cố tình gây 1 lô "khựng" (P2 delay thêm 300ms cho đúng 1 lô, mô
phỏng GC pause / OS scheduling hiccup thật), rồi đo proc_ms của các lô SAU đó
để xem mất bao lâu quay lại baseline (~150-180ms) — so sánh maxsize=10 (hiện
tại) vs maxsize=1 (khuyến nghị trong comment).

Chạy:
    .venv\\Scripts\\python.exe test\\bench_queue_size_recovery.py
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

BATCH = 30
FS_FPS = 60.0
CADENCE_S = BATCH / FS_FPS  # 0.5s @60fps


def _producer(q, n_batches, cadence_s, done_evt):
    for seq in range(n_batches):
        q.put((seq, time.time()))
        time.sleep(cadence_s)
    done_evt.set()


def _consumer_with_one_stall(q_in, q_out, done_evt, stall_at_seq, stall_s, dsp_ms_list):
    """Mô phỏng P2: DSP ~150ms mỗi lô, NHƯNG cố tình khựng thêm stall_s ở đúng
    1 lô (stall_at_seq) để mô phỏng GC pause / OS hiccup thoáng qua."""
    while True:
        try:
            seq, ts = q_in.get(timeout=0.5)
        except Empty:
            if done_evt.is_set():
                break
            continue
        t0 = time.time()
        time.sleep(0.15)  # DSP nền ổn định ~150ms (giống thật)
        if seq == stall_at_seq:
            time.sleep(stall_s)  # khựng thêm 1 lần duy nhất
        dsp_ms_list.append((time.time() - t0) * 1000.0)
        q_out.put((seq, ts))


def _sink(q, done_evt, proc_ms_by_seq):
    while True:
        try:
            seq, ts = q.get(timeout=0.5)
        except Empty:
            if done_evt.is_set():
                break
            continue
        proc_ms_by_seq[seq] = (time.time() - float(ts)) * 1000.0


def run(label, maxsize, n_batches=24, stall_at_seq=8, stall_s=0.3):
    mp.set_start_method("spawn", force=True)
    q_in = DropOldestQueue(maxsize=maxsize)
    q_out = DropOldestQueue(maxsize=maxsize)
    done1 = mp.Event()
    mgr = mp.Manager()
    dsp_ms_list = mgr.list()
    proc_ms_by_seq = mgr.dict()

    p_prod = mp.Process(target=_producer, args=(q_in, n_batches, CADENCE_S, done1))
    p_p2 = mp.Process(target=_consumer_with_one_stall,
                      args=(q_in, q_out, done1, stall_at_seq, stall_s, dsp_ms_list))
    p_sink = mp.Process(target=_sink, args=(q_out, done1, proc_ms_by_seq))

    p_prod.start(); p_p2.start(); p_sink.start()
    p_prod.join()
    p_p2.join(timeout=10); p_sink.join(timeout=10)
    for p in (p_p2, p_sink):
        if p.is_alive():
            p.terminate(); p.join()

    got = dict(proc_ms_by_seq)
    seqs = sorted(got.keys())
    print(f"\n[{label}] maxsize={maxsize}  (khựng +{stall_s*1000:.0f}ms tại lô #{stall_at_seq})")
    print(f"  lô nhận được: {len(seqs)}/{n_batches} "
          f"({'CÓ lô bị drop' if len(seqs) < n_batches else 'không lô nào bị drop'})")
    baseline = statistics.mean([got[s] for s in seqs if s < stall_at_seq]) if any(s < stall_at_seq for s in seqs) else float("nan")
    print(f"  proc_ms baseline (trước khựng) = {baseline:.0f} ms")
    row = "  proc_ms quanh lô khựng: "
    for s in seqs:
        if stall_at_seq - 2 <= s <= stall_at_seq + 8:
            tag = "*" if s == stall_at_seq else " "
            row += f"[{s}{tag}]{got[s]:.0f} "
    print(row)
    # đếm số lô SAU khi khựng vẫn còn > baseline*1.3 (chưa hồi phục)
    after = [s for s in seqs if s > stall_at_seq]
    lingering = sum(1 for s in after if got[s] > baseline * 1.3) if not np.isnan(baseline) else -1
    print(f"  số lô SAU khựng vẫn còn CAO (>1.3x baseline) trước khi hồi phục: {lingering}")
    return got, baseline, lingering


def main():
    print("=" * 78)
    print("KIỂM CHỨNG: sau 1 lô khựng thoáng qua, proc_ms có tự hồi phục ngay không?")
    print("So sánh --preprocess-queue-size=10 (MẶC ĐỊNH HIỆN TẠI) vs =1 (comment khuyến nghị)")
    print("=" * 78)

    run("A. maxsize=10 (MẶC ĐỊNH HIỆN TẠI)", maxsize=10)
    run("B. maxsize=1  (comment khuyến nghị 'self-heal')", maxsize=1)

    print("\n" + "=" * 78)
    print("Đọc kết quả: nếu (A) có nhiều lô 'còn CAO' hơn (B) sau lô khựng,")
    print("nghĩa là maxsize=10 khiến 1 lần khựng thoáng qua ĐỂ LẠI ĐUÔI trễ cao")
    print("kéo dài vài lô rồi mới tự giảm dần -- đúng như comment mô tả.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
