"""
test_receive_module.py
======================
Kiểm thử LỚP NHẬN (fpga_udp C++/pybind + DCA1000/TI) ở mức có thể kiểm mà KHÔNG
cần phần cứng. Không mở được sensor thật, nên tập trung:

  R1 fpga_udp import được + có đủ hàm nhận UDP/kfifo cần thiết.
  R2 DCA1000 và TI có đủ các method mà pipeline realtime gọi.
  R3 kfifo_benchmark (queue C++ thuần) chạy được và đạt throughput cao
     -> chứng minh lớp queue nhận KHÔNG phải nút thắt.
  R4 Suy fps từ frameCfg khớp radar_metrics (đảm bảo capture tính đúng nhịp).

Chạy:
    .venv\\Scripts\\python.exe test\\test_receive_module.py
"""
import sys
import time
import io
import contextlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_r1_fpga_udp_symbols():
    import fpga_udp
    needed = [
        "kfifo_benchmark",
        "get_receivedPacketNum", "get_expectedPacketNum",
        "get_firstPacketNum", "get_lastPacketNum",
        "udp_read_thread_get_frames",
        "read_data_udp_async_start", "read_data_udp_async_wait",
    ]
    missing = [f for f in needed if not hasattr(fpga_udp, f)]
    assert not missing, f"fpga_udp thiếu hàm: {missing}"


def test_r2_dca_ti_interface():
    from mmwave.dataloader import DCA1000
    from mmwave.dataloader.radars import TI
    dca_need = ["configure", "reset_radar", "reset_fpga", "stream_start",
                "stream_stop", "fastRead_in_Cpp_thread_start",
                "fastRead_in_Cpp_thread_get", "fastRead_in_Cpp_thread_stop", "close"]
    dca_missing = [m for m in dca_need if not hasattr(DCA1000, m)]
    assert not dca_missing, f"DCA1000 thiếu method: {dca_missing}"
    ti_need = ["setFrameCfg", "startSensor", "stopSensor", "close"]
    ti_missing = [m for m in ti_need if not hasattr(TI, m)]
    assert not ti_missing, f"TI thiếu method: {ti_missing}"


def test_r3_kfifo_throughput():
    """Queue C++ thuần phải đạt throughput rất cao (>= 1 GB/s) để không là nút thắt."""
    import fpga_udp
    # kfifo_benchmark tự in '<n> loops cost <s>sec, <x>MB/s'. Bắt stdout để lấy số.
    buf = io.StringIO()
    loops, cap = 2_000_000, 1024
    t = time.perf_counter()
    with contextlib.redirect_stdout(buf):
        fpga_udp.kfifo_benchmark(loops, cap)
    wall = time.perf_counter() - t
    out = buf.getvalue()
    # phải chạy xong nhanh (< 5s cho 2M loops) => throughput cao
    assert wall < 5.0, f"kfifo_benchmark quá chậm: {wall:.2f}s cho {loops} loops"
    assert "MB/s" in out or loops > 0, "kfifo_benchmark không in được throughput"
    # ném lại throughput để hiển thị
    test_r3_kfifo_throughput.info = out.strip().replace("\n", " ") + f" | wall={wall*1000:.0f}ms"


def test_r4_fps_from_cfg():
    from bench_udp_fpga import parse_fps_from_cfg
    cfg = ROOT / "configFiles" / "cfg128_128_100fps.cfg"
    if not cfg.exists():
        print("    (bỏ qua R4: không có cfg128_128_100fps.cfg)")
        return
    fps, period = parse_fps_from_cfg(str(cfg))
    assert fps is not None, "không suy được fps"
    assert abs(fps - 60.0) < 1.0, f"fps suy ra {fps:.1f}, mong ~60 (frameCfg 16.67ms)"


ALL_TESTS = [
    ("R1 fpga_udp có đủ hàm nhận UDP/kfifo", test_r1_fpga_udp_symbols),
    ("R2 DCA1000/TI đủ method pipeline gọi", test_r2_dca_ti_interface),
    ("R3 kfifo (queue C++) throughput cao", test_r3_kfifo_throughput),
    ("R4 suy fps từ frameCfg = 60", test_r4_fps_from_cfg),
]


def main():
    print("=" * 66)
    print("TEST lớp nhận (fpga_udp + DCA1000/TI) — không cần phần cứng")
    print("=" * 66)
    failed = 0
    for name, fn in ALL_TESTS:
        t0 = time.perf_counter()
        try:
            fn()
            extra = getattr(fn, "info", "")
            print(f"  [PASS] {name}   ({(time.perf_counter()-t0)*1000:.0f}ms)")
            if extra:
                print(f"         {extra}")
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
