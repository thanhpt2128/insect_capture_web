"""
extract_frames.py
=================
Đọc file .bin chứa nhiều frame liên tục và trích xuất đoạn 30 frame
bắt đầu từ vị trí do người dùng chọn.

Cách dùng:
    # Xem tổng số frame và quét năng lượng toàn file
    python extract_frames.py data/recording.bin --info

    # Trích xuất 30 frame từ frame 120
    python extract_frames.py data/recording.bin --start 120

    # Trích xuất + chỉ định file output + số frame tuỳ ý
    python extract_frames.py data/recording.bin --start 120 --frames 30 --output out.raw

    # Trích xuất + kiểm tra ngay bằng InsectRadarProcessor
    python extract_frames.py data/recording.bin --start 120 --verify
"""

import argparse
import os
import sys
import numpy as np

# =============================================================================
# THÔNG SỐ RADAR (phải khớp với insect_radar_processor.py / RadarConfig)
# =============================================================================

NUM_TX             = 1
NUM_RX             = 4
NUM_ADC_SAMPLES    = 128
NUM_LOOPS_PER_FRAME = 128

# Số int16 trong 1 frame:
#   mỗi complex sample = 2 int16 (I + Q)
#   samples/frame = num_tx × num_rx × num_adc × num_loops = 65 536
#   int16/frame   = 65 536 × 2 = 131 072
INT16_PER_FRAME = NUM_TX * NUM_RX * NUM_ADC_SAMPLES * NUM_LOOPS_PER_FRAME * 2
BYTES_PER_FRAME = INT16_PER_FRAME * 2   # int16 = 2 bytes


# =============================================================================
# HÀM TIỆN ÍCH
# =============================================================================

def file_info(bin_path: str) -> tuple[int, int]:
    """Trả về (total_frames, file_size_bytes)."""
    size = os.path.getsize(bin_path)
    total_int16  = size // 2
    total_frames = total_int16 // INT16_PER_FRAME
    return total_frames, size


def read_frames(bin_path: str, start_frame: int, n_frames: int) -> np.ndarray:
    """
    Đọc n_frames frame liên tiếp từ vị trí start_frame trong file .bin.

    Parameters
    ----------
    bin_path    : đường dẫn file .bin
    start_frame : frame bắt đầu (0-indexed)
    n_frames    : số frame cần đọc

    Returns
    -------
    ndarray int16, shape (n_frames * INT16_PER_FRAME,)
    """
    offset_bytes = start_frame * BYTES_PER_FRAME
    count_int16  = n_frames * INT16_PER_FRAME

    data = np.fromfile(
        bin_path,
        dtype=np.int16,
        count=count_int16,
        offset=offset_bytes,
    )

    if len(data) < count_int16:
        actual = len(data) // INT16_PER_FRAME
        raise ValueError(
            f"Chỉ đọc được {actual} frames từ frame {start_frame} "
            f"(yêu cầu {n_frames}). File có thể bị cắt hoặc start_frame quá lớn."
        )

    return data


def save_raw(data: np.ndarray, output_path: str) -> None:
    """Lưu dữ liệu int16 ra file .raw."""
    data.tofile(output_path)
    n_frames = len(data) // INT16_PER_FRAME
    print(f"  Saved  : {output_path}")
    print(f"  Frames : {n_frames}")
    print(f"  Size   : {len(data) * 2:,} bytes ({len(data) * 2 / 1024:.1f} KB)")


# =============================================================================
# QUÉT NĂNG LƯỢNG (để tìm vùng có côn trùng trước khi chọn start_frame)
# =============================================================================

def _frame_to_rtm_db(bin_path: str, start_frame: int, n_frames: int) -> np.ndarray:
    """
    Tính Range-Time Map (dB) nhanh cho đoạn n_frames frame.
    Dùng để scan mà không cần InsectRadarProcessor.

    Returns
    -------
    rtm_db : ndarray [n_frames, 32] — mean magnitude dB theo range bin
    """
    data = read_frames(bin_path, start_frame, n_frames)

    # Chuyển int16 → complex (IIQQ, quy ước offline/train: I=cột[0,1], Q=cột[2,3])
    mat     = data.reshape(-1, 4)
    raw_I   = mat[:, [0, 1]].flatten()
    raw_Q   = mat[:, [2, 3]].flatten()
    cplx    = raw_I.astype(np.float32) + 1j * raw_Q.astype(np.float32)

    # Reshape → [frame, ant, loop, sample]
    n_chirps = NUM_TX * NUM_LOOPS_PER_FRAME
    cube = cplx.reshape(n_frames, n_chirps, NUM_RX, NUM_ADC_SAMPLES)
    cube = cube.reshape(n_frames, NUM_LOOPS_PER_FRAME, NUM_TX, NUM_RX, NUM_ADC_SAMPLES)
    cube = cube.transpose(0, 2, 3, 1, 4)                              # [F, TX, RX, Loop, Sample]
    cube = cube.reshape(n_frames, NUM_TX * NUM_RX, NUM_LOOPS_PER_FRAME, NUM_ADC_SAMPLES)

    # Static clutter removal
    cube = cube - np.mean(cube, axis=2, keepdims=True)

    # Range FFT (chỉ lấy 32 bin đầu)
    win      = np.hanning(NUM_ADC_SAMPLES)
    rfft     = np.fft.fft(cube * win, axis=3)[..., :32]

    # Mean magnitude qua anten và chirps → [n_frames, 32]
    mag_map  = np.mean(np.abs(rfft), axis=(1, 2))
    rtm_db   = 20 * np.log10(mag_map + 1e-6)

    return rtm_db


def scan_energy(bin_path: str, total_frames: int,
                window: int = 30, step: int = 10) -> list[dict]:
    """
    Quét toàn bộ file theo cửa sổ, trả về năng lượng trung bình theo range.

    Parameters
    ----------
    window : int — kích thước cửa sổ (frame)
    step   : int — bước nhảy giữa các cửa sổ

    Returns
    -------
    list of dict: [{'start', 'end', 'peak_bin', 'peak_db', 'energy_roi'}]
    """
    results = []
    starts  = range(0, total_frames - window + 1, step)

    for i, s in enumerate(starts):
        sys.stdout.write(f"\r  Scanning... {i+1}/{len(starts)}", )
        sys.stdout.flush()
        try:
            rtm = _frame_to_rtm_db(bin_path, s, window)
            mean_rtm  = rtm.mean(axis=0)          # [32]
            roi_db    = mean_rtm[15:20].mean()     # vùng range 15-20
            peak_bin  = int(np.argmax(mean_rtm))
            peak_db   = float(mean_rtm[peak_bin])
            results.append({
                'start'     : s,
                'end'       : s + window - 1,
                'peak_bin'  : peak_bin,
                'peak_db'   : peak_db,
                'energy_roi': float(roi_db),
            })
        except Exception as e:
            results.append({'start': s, 'end': s + window - 1,
                            'error': str(e)})

    print()
    return results


def print_scan_table(results: list[dict], threshold_db: float = None) -> None:
    """In bảng kết quả scan + bar chart ASCII."""
    if not results:
        print("  Không có kết quả scan.")
        return

    energies = [r['energy_roi'] for r in results if 'energy_roi' in r]
    if not energies:
        return

    e_min = min(energies)
    e_max = max(energies)
    bar_width = 30

    # Gợi ý ngưỡng = percentile 75 (vùng có nhiều năng lượng hơn)
    if threshold_db is None:
        threshold_db = float(np.percentile(energies, 75))

    print(f"\n  {'Start':>6}  {'End':>6}  {'PeakBin':>7}  {'ROI dB':>8}  {'Activity':}")
    print("  " + "-" * 70)

    for r in results:
        if 'error' in r:
            print(f"  {r['start']:>6}  {r['end']:>6}  ERR: {r['error']}")
            continue
        e    = r['energy_roi']
        norm = (e - e_min) / (e_max - e_min + 1e-9)
        bar  = '#' * int(norm * bar_width) + '.' * (bar_width - int(norm * bar_width))
        flag = " <-- ACTIVE" if e >= threshold_db else ""
        print(f"  {r['start']:>6}  {r['end']:>6}  {r['peak_bin']:>7}  {e:>8.2f}  |{bar}|{flag}")

    print(f"\n  Ngưỡng gợi ý (P75): {threshold_db:.2f} dB")
    best = max(results, key=lambda r: r.get('energy_roi', -999))
    print(f"  Start frame hoat dong nhat: {best['start']}  (ROI = {best['energy_roi']:.2f} dB)")


# =============================================================================
# VERIFY (chạy InsectRadarProcessor trên file vừa trích xuất)
# =============================================================================

def verify_output(output_path: str,
                  range_bin_min: int = 15,
                  range_bin_max: int = 20) -> None:
    """Chạy InsectRadarProcessor trên file output để kiểm tra."""
    try:
        from insect_radar_processor import InsectRadarProcessor
    except ImportError:
        print("  [WARN] Không import được insect_radar_processor — bỏ qua verify.")
        return

    print(f"\n  Verifying '{output_path}' voi InsectRadarProcessor...")
    proc   = InsectRadarProcessor(range_bin_min=range_bin_min,
                                  range_bin_max=range_bin_max)
    result = proc.process(output_path)

    print(f"  is_insect            : {result['is_insect']}")
    print(f"  power_threshold    : {result['power_threshold']:.4f}")
    if result['is_insect']:
        print(f"  So features          : {len(result['features'])}")
        print(f"  --> San sang dua vao model.")
    else:
        print(f"  reason               : {result['reason']}")
        print(f"  --> Day la doan nen, thu chon start_frame khac.")


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Trich xuat 30 frame lien tiep tu file .bin radar AWR1843."
    )
    parser.add_argument(
        "bin_path",
        type=str,
        help="Duong dan file .bin",
    )
    parser.add_argument(
        "--start", "-s",
        type=int,
        default=None,
        help="Frame bat dau (0-indexed). Bat buoc tru khi dung --info.",
    )
    parser.add_argument(
        "--frames", "-n",
        type=int,
        default=30,
        help="So frame can trich xuat (mac dinh: 30).",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="Duong dan file output .raw (mac dinh: tu sinh ten).",
    )
    parser.add_argument(
        "--info",
        action="store_true",
        help="Chi hien thi thong tin file, khong trich xuat.",
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="Quet nang luong toan bo file de tim vung co con trung.",
    )
    parser.add_argument(
        "--scan-step",
        type=int,
        default=10,
        help="Buoc nhay khi scan (frame). Mac dinh: 10.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Chay InsectRadarProcessor tren file output de kiem tra.",
    )
    parser.add_argument(
        "--bin-min",
        type=int,
        default=15,
        help="range_bin_min cho InsectRadarProcessor (mac dinh: 15).",
    )
    parser.add_argument(
        "--bin-max",
        type=int,
        default=20,
        help="range_bin_max cho InsectRadarProcessor (mac dinh: 20).",
    )

    args = parser.parse_args()

    # ── Kiểm tra file tồn tại ──────────────────────────────────────────────
    if not os.path.exists(args.bin_path):
        print(f"[ERROR] Khong tim thay file: {args.bin_path}")
        sys.exit(1)

    total_frames, file_size = file_info(args.bin_path)

    print("=" * 60)
    print(f"  File       : {args.bin_path}")
    print(f"  Size       : {file_size:,} bytes ({file_size / 1024 / 1024:.2f} MB)")
    print(f"  Total frames  : {total_frames}")
    print(f"  Frame size    : {BYTES_PER_FRAME:,} bytes ({BYTES_PER_FRAME/1024:.0f} KB)")
    print(f"  INT16/frame   : {INT16_PER_FRAME:,}")

    # Ước tính fps (AWR1843 default ~60 fps)
    fps = 60.0
    duration_s = total_frames / fps
    print(f"  Duration (est): {duration_s:.1f}s @ {fps:.0f} fps")
    print("=" * 60)

    if args.info:
        if total_frames == 0:
            print("[WARN] File rong hoac kich thuoc khong hop le.")
        return

    # ── Scan năng lượng ────────────────────────────────────────────────────
    if args.scan:
        print(f"\nQuet nang luong (window={args.frames}, step={args.scan_step})...")
        scan_results = scan_energy(
            args.bin_path, total_frames,
            window=args.frames, step=args.scan_step
        )
        print_scan_table(scan_results)
        print()

        # Nếu không chỉ định --start, hỏi người dùng
        if args.start is None:
            try:
                val = input("  Nhap start_frame de trich xuat (Enter de bo qua): ").strip()
                if val == "":
                    print("Bo qua trich xuat.")
                    return
                args.start = int(val)
            except (EOFError, ValueError):
                print("Bo qua trich xuat.")
                return

    # ── Kiểm tra --start ──────────────────────────────────────────────────
    if args.start is None:
        print("[ERROR] Chua chi dinh --start. Dung --info hoac --scan de xem file truoc.")
        print("  Vi du: python extract_frames.py recording.bin --start 120")
        sys.exit(1)

    if args.start < 0:
        print(f"[ERROR] start_frame phai >= 0 (nhan duoc: {args.start})")
        sys.exit(1)

    max_start = total_frames - args.frames
    if args.start > max_start:
        print(f"[ERROR] start_frame={args.start} qua lon.")
        print(f"  File co {total_frames} frames, can {args.frames} frames lien tiep.")
        print(f"  start_frame toi da: {max_start}")
        sys.exit(1)

    # ── Sinh tên output nếu chưa có ──────────────────────────────────────
    if args.output is None:
        base     = os.path.splitext(os.path.basename(args.bin_path))[0]
        out_dir  = os.path.dirname(args.bin_path)
        args.output = os.path.join(out_dir,
                                   f"{base}_frame{args.start:04d}"
                                   f"_to_{args.start + args.frames - 1:04d}.raw")

    # ── Trích xuất ────────────────────────────────────────────────────────
    print(f"\nTrich xuat frames [{args.start} → {args.start + args.frames - 1}]...")
    try:
        data = read_frames(args.bin_path, args.start, args.frames)
    except ValueError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    print(f"  Doc thanh cong: {len(data):,} int16 values")

    # Tạo thư mục output nếu cần
    out_dir = os.path.dirname(args.output)
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    save_raw(data, args.output)

    # ── Verify (tuỳ chọn) ─────────────────────────────────────────────────
    if args.verify:
        verify_output(args.output, args.bin_min, args.bin_max)

    print("\nHoan thanh.")


if __name__ == "__main__":
    main()
