"""
bench_udp_fpga.py
=================
Benchmark riêng LỚP NHẬN UDP (fpga_udp kfifo) + get_frames, tách khỏi DSP/AI.
Mục đích: xem lớp nhận có "đứng đọng" (standing backlog) gây trễ hay không —
trước cả khi dữ liệu vào pipeline Python.

Phải chạy trên máy có phần cứng (radar + DCA1000), vì cần sensor đang stream.

Đo gì:
  * get_ms  : thời gian mỗi lần fastRead_in_Cpp_thread_get(numframes).
              - ~ numframes/fps (vd 30/60=500ms) => capture bám realtime, kfifo KHÔNG đọng.
              - << ngưỡng đó (gần 0)             => kfifo có TỒN ĐỌNG (đọc data cũ, lớp nhận trễ).
  * loss%   : (expected-received)/expected mỗi lô (mất packet / kfifo overflow).
  * eff_fps : số frame thực sự rút được / giây (so với fps radar).
  * backlog : "drain test" — đếm số lần get trả về NGAY trước khi 1 lần phải CHỜ
              => số frame đang đọng trong kfifo => quy ra giây trễ ở lớp nhận.

Hai chế độ:
  1) kfifo-only (KHÔNG cần phần cứng): đo throughput queue C++ thuần (Put/MB/s)
     để chứng minh lớp queue không bao giờ là nút thắt.
  2) full (cần radar + DCA1000): init sensor, đo get_frames / loss / backlog.

Chạy:
  # 1) không cần radar:
  python bench_udp_fpga.py --kfifo-only
  python bench_udp_fpga.py --kfifo-only --kfifo-loops 10000000 --kfifo-cap 2048
  # 2) cần radar:
  python bench_udp_fpga.py --com-port COM5 --cfg-path cfg128_128_100fps.cfg
  python bench_udp_fpga.py --com-port COM5 --cfg-path ... --numframes 30 --iters 80 --fps 60
"""

import argparse
import statistics as st
import sys
import time
from pathlib import Path

# Tránh UnicodeEncodeError khi stdout là cp1252 (Windows) — script in tiếng Việt.
for _stream in (sys.stdout, sys.stderr):
    _reconf = getattr(_stream, "reconfigure", None)
    if _reconf is not None:
        try:
            _reconf(encoding="utf-8", errors="replace")
        except Exception:
            pass


def parse_fps_from_cfg(cfg_path: str):
    """fps = 1000 / framePeriod(ms) lấy từ dòng frameCfg (parts[5])."""
    try:
        with open(cfg_path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                p = line.split()
                if p and p[0] == "frameCfg" and len(p) >= 6:
                    period_ms = float(p[5])
                    if period_ms > 0:
                        return 1000.0 / period_ms, period_ms
    except Exception:
        pass
    return None, None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Benchmark UDP receive + get_frames (fpga_udp).")
    ap.add_argument("--com-port", help="Bắt buộc trừ khi --kfifo-only.")
    ap.add_argument("--cli-baud", type=int, default=921600)
    ap.add_argument("--cfg-path", help="Bắt buộc trừ khi --kfifo-only.")
    ap.add_argument("--dca-cfg", default="cf.json")
    ap.add_argument("--numframes", type=int, default=30)
    ap.add_argument("--frame-num-in-buf", type=int, default=128)
    ap.add_argument("--iters", type=int, default=80)
    ap.add_argument("--fps", type=float, default=0.0, help="0 = parse from cfg frameCfg")
    ap.add_argument("--kfifo-only", action="store_true",
                    help="Chỉ benchmark queue C++ thuần (KHÔNG cần radar). "
                         "--com-port/--cfg-path bỏ qua trong chế độ này.")
    ap.add_argument("--kfifo-loops", type=int, default=5_000_000,
                    help="Số lần Put() trong kfifo-only.")
    ap.add_argument("--kfifo-cap", type=int, default=1024,
                    help="Sức chứa queue (packet) trong kfifo-only.")
    args = ap.parse_args(argv)

    import fpga_udp

    # ── Chế độ 1: kfifo-only — đo throughput queue C++ thuần, KHÔNG cần radar ──
    if args.kfifo_only:
        print("--- kfifo-only: benchmark queue C++ thuần (không phần cứng) ---")
        print(f"loops={args.kfifo_loops:,}  cap={args.kfifo_cap} packet\n")
        sys.stdout.flush()  # để dòng printf của C++ in SAU header Python
        # fpga_udp.kfifo_benchmark tự in: '<n> loops cost <s>sec, <x>MB/s'.
        # In ra >0 nghĩa là queue đầy sớm (cap < loops thì Put override, không lỗi).
        t = time.perf_counter()
        fpga_udp.kfifo_benchmark(args.kfifo_loops, args.kfifo_cap)
        wall = (time.perf_counter() - t) * 1000.0
        print(f"\n[wall] {wall:.1f} ms cho {args.kfifo_loops:,} lần Put()")
        print("[VERDICT] queue C++ thường đạt hàng GB/s -> KHÔNG bao giờ là nút thắt. "
              "Nếu trễ tồn tại, nó ở tốc độ RÚT (get_frames) hoặc pipeline Python, không phải queue.")
        return 0

    if not args.com_port or not args.cfg_path:
        ap.error("--com-port và --cfg-path là bắt buộc (trừ khi --kfifo-only).")

    repo = Path(__file__).resolve().parent
    cfg = args.cfg_path if Path(args.cfg_path).is_absolute() else str(repo / "configFiles" / args.cfg_path)
    dca_json = args.dca_cfg if Path(args.dca_cfg).is_absolute() else str(repo / "configFiles" / args.dca_cfg)

    from mmwave.dataloader import DCA1000
    from mmwave.dataloader.radars import TI

    fps = args.fps
    if fps <= 0:
        fps, _ = parse_fps_from_cfg(cfg)
        if not fps:
            fps = 60.0
    frame_ms = 1000.0 / fps
    sync_get_ms = args.numframes * frame_ms
    print(f"[cfg] fps={fps:.1f}, frame_period={frame_ms:.2f}ms, numframes={args.numframes}")
    print(f"[cfg] 'synced' get_frames ~ {sync_get_ms:.0f} ms/call (nếu capture bám realtime)\n")

    dca = DCA1000()
    dca.reset_radar()
    dca.reset_fpga()
    time.sleep(7.0)
    radar = TI(cli_loc=args.com_port, cli_baud=args.cli_baud,
               data_loc=args.com_port, data_baud=args.cli_baud,
               config_file=cfg, verbose=False)
    try:
        radar.setFrameCfg(0)
    except Exception:
        pass
    dca.configure(dca_json, cfg)
    dca.stream_start()
    dca.fastRead_in_Cpp_thread_start(max(args.frame_num_in_buf, args.numframes))
    radar.startSensor()
    print("[+] Sensor streaming. Bắt đầu đo...\n")

    def pull():
        t = time.perf_counter()
        dca.fastRead_in_Cpp_thread_get(numframes=args.numframes, timeOut=2, verbose=False, sortInC=True)
        dt = (time.perf_counter() - t) * 1000.0
        recv = fpga_udp.get_receivedPacketNum()
        exp = fpga_udp.get_expectedPacketNum()
        first = fpga_udp.get_firstPacketNum()
        last = fpga_udp.get_lastPacketNum()
        loss = (exp - recv) / exp * 100.0 if exp else 0.0
        return dt, loss, first, last

    try:
        # Warmup (xả vài lô đầu, bỏ qua khỏi thống kê)
        for _ in range(3):
            pull()

        get_ms, loss_pct = [], []
        gaps = 0
        prev_last = None
        t0 = time.perf_counter()
        for _ in range(args.iters):
            dt, loss, first, last = pull()
            get_ms.append(dt)
            loss_pct.append(loss)
            if prev_last is not None and first > prev_last + 1:
                gaps += 1   # firstPacket nhảy quá lastPacket trước -> có packet bị bỏ
            prev_last = last
        wall = time.perf_counter() - t0
        eff_fps = args.iters * args.numframes / wall

        print(f"--- get_frames qua {args.iters} lần ---")
        print(f"get_ms : mean={st.mean(get_ms):7.1f}  median={st.median(get_ms):7.1f}  "
              f"min={min(get_ms):7.1f}  max={max(get_ms):7.1f}")
        print(f"loss%  : mean={st.mean(loss_pct):6.2f}  max={max(loss_pct):6.2f}")
        print(f"eff_fps rút được = {eff_fps:6.1f}  (radar fps = {fps:.1f})")
        print(f"frame-boundary gaps (chunk bị mất) = {gaps}")

        med = st.median(get_ms)
        if med < 0.5 * sync_get_ms:
            print(f"\n[VERDICT] get_ms ({med:.0f}ms) << synced ({sync_get_ms:.0f}ms) => "
                  "kfifo CÓ TỒN ĐỌNG: lớp nhận đang đọc data cũ, trễ nằm Ở ĐÂY.")
        else:
            print(f"\n[VERDICT] get_ms (~{med:.0f}ms) ~ synced ({sync_get_ms:.0f}ms) => "
                  "capture bám realtime, lớp nhận KHÔNG đọng (trễ nằm ở pipeline Python sau get).")

        # ── Drain test: đo backlog đứng trong kfifo ──────────────────────────
        print("\n--- drain test (đo backlog đứng trong kfifo) ---")
        instant = 0
        cap = int(fps * 30 / args.numframes) + 5   # trần an toàn (~30s)
        while instant < cap:
            t = time.perf_counter()
            dca.fastRead_in_Cpp_thread_get(numframes=args.numframes, timeOut=2, verbose=False, sortInC=True)
            dt = time.perf_counter() - t
            if dt > 0.6 * (sync_get_ms / 1000.0):
                break  # lần này phải CHỜ -> đã rút hết tồn đọng, bắt kịp realtime
            instant += 1
        backlog_frames = instant * args.numframes
        print(f"số lần get trả NGAY = {instant} -> backlog ~{backlog_frames} frame "
              f"~ {backlog_frames / fps:.2f} s đọng trong kfifo")

    finally:
        try:
            radar.stopSensor(); radar.close()
        except Exception:
            pass
        try:
            dca.fastRead_in_Cpp_thread_stop(); dca.stream_stop(); dca.close()
        except Exception:
            pass
        print("\n[+] Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
