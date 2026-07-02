"""
run_all.py
==========
Chạy lần lượt các file test *correctness* và tổng hợp PASS/FAIL.
(Không chạy benchmark — benchmark chỉ in số liệu, gọi riêng khi cần.)

    .venv\\Scripts\\python.exe test\\run_all.py
Exit 0 nếu tất cả PASS, 1 nếu có file FAIL.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORRECTNESS = [
    "test_drop_oldest_queue.py",
    "test_pipeline_functional.py",
    "test_sliding_window.py",
    "test_database.py",
    "test_receive_module.py",
]


def main():
    env_note = "(đặt PYTHONUTF8=1 nếu console báo lỗi Unicode)"
    print("=" * 72)
    print(f"RUN ALL — correctness  {env_note}")
    print("=" * 72)
    results = {}
    for name in CORRECTNESS:
        print(f"\n>>> {name}")
        proc = subprocess.run([sys.executable, str(HERE / name)])
        results[name] = proc.returncode

    print("\n" + "=" * 72)
    print("TỔNG HỢP")
    for name, rc in results.items():
        print(f"  {'PASS ✅' if rc == 0 else 'FAIL ❌'}  {name}")
    failed = sum(1 for rc in results.values() if rc != 0)
    print("=" * 72)
    print("KẾT QUẢ:", "TẤT CẢ PASS ✅" if failed == 0 else f"{failed} file FAIL ❌")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
