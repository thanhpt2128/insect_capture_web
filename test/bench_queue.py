"""
bench_queue.py
==============
Benchmark thông lượng + độ trễ của ``DropOldestQueue`` (class thật) ở các tình
huống đại diện cho pipeline:
  - put thông lượng với payload nhỏ (item P2->P3) và payload 16MB (item P1->P2)
  - độ trễ end-to-end cross-process khi consumer NHANH hơn producer (chế độ thật)
    và khi consumer CHẬM hơn (bão hòa -> drop)

Chạy:
    .venv\\Scripts\\python.exe test\\bench_queue.py
"""
import sys
import time
import statistics
import multiprocessing as mp
from pathlib import Path
from queue import Empty

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from realTimeProc_infer import DropOldestQueue

import numpy as np


def _producer(q, m, done, pace, payload):
    blob = np.zeros(payload // 8, dtype=np.complex64) if payload else None
    for seq in range(m):
        q.put((seq, time.perf_counter()) if blob is None else (seq, time.perf_counter(), blob))
        if pace:
            time.sleep(pace)
    done.set()


def _consumer(q, done, gdelay, lat_list, recv_counter):
    while True:
        try:
            item = q.get(timeout=0.3)
        except Empty:
            if done.is_set():
                break
            continue
        lat_list.append((time.perf_counter() - item[1]) * 1000.0)
        recv_counter.value += 1
        if gdelay:
            time.sleep(gdelay)


def put_throughput(payload, n, maxsize=10):
    q = DropOldestQueue(maxsize=maxsize)
    blob = np.zeros(payload // 8, dtype=np.complex64) if payload else None
    t = time.perf_counter()
    for i in range(n):
        q.put((i, time.time()) if blob is None else (i, time.time(), blob))
    dt = time.perf_counter() - t
    time.sleep(0.2)
    return n / dt, 1000 * dt / n


def cross_latency(maxsize, m, pace, gdelay, payload):
    q = DropOldestQueue(maxsize=maxsize)
    done = mp.Event()
    mgr = mp.Manager()
    lat = mgr.list()
    recv = mp.Value("i", 0)
    pc = mp.Process(target=_consumer, args=(q, done, gdelay, lat, recv))
    pr = mp.Process(target=_producer, args=(q, m, done, pace, payload))
    pc.start(); pr.start(); pr.join(); pc.join()
    l = list(lat)
    mgr.shutdown()
    return recv.value, (statistics.mean(l) if l else float("nan")), (max(l) if l else float("nan"))


def main():
    mp.set_start_method("spawn", force=True)
    print("=" * 72)
    print("BENCHMARK DropOldestQueue (class thật)")
    print("=" * 72)

    print("\n[put throughput single-process]")
    r, ms = put_throughput(payload=0, n=5000)
    print(f"  payload nhỏ : {r:10,.0f} item/s  ({ms:.3f} ms/put)")
    r, ms = put_throughput(payload=16 * 1024 * 1024, n=60)
    print(f"  payload 16MB: {r:10,.0f} item/s  ({ms:.2f} ms/put)  (= item P1->P2)")

    print("\n[độ trễ cross-process — chế độ THẬT: consumer nhanh hơn producer]")
    recv, mean_l, max_l = cross_latency(maxsize=10, m=60, pace=0.012, gdelay=0.003,
                                        payload=16 * 1024 * 1024)
    print(f"  P1->P2 (16MB): nhận={recv}/60  trễ TB={mean_l:.1f}ms  max={max_l:.1f}ms  (drop≈0)")
    recv, mean_l, max_l = cross_latency(maxsize=10, m=80, pace=0.012, gdelay=0.001, payload=0)
    print(f"  P2->P3 (nhỏ): nhận={recv}/80  trễ TB={mean_l:.1f}ms  max={max_l:.1f}ms  (drop≈0)")

    print("\n[độ trễ cross-process — BÃO HÒA: consumer chậm hơn -> drop-oldest giữ trễ thấp]")
    recv, mean_l, max_l = cross_latency(maxsize=4, m=300, pace=0.0005, gdelay=0.003, payload=0)
    print(f"  bão hòa nhỏ : nhận={recv}/300  trễ TB={mean_l:.1f}ms  max={max_l:.1f}ms")
    print("  (trễ thấp dù producer nhanh hơn nhiều = drop-oldest hoạt động đúng)")

    print("\n" + "=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
