"""
classify.py
===========
Script don gian: .bin -> extract -> process -> classify -> visualize.

Cach dung:
    python classify.py "D:\An Engr\Project\DATN\Code\project\data\ongx2_65cm_lan4_Raw_0.bin" 120
    python classify.py "D:\An Engr\Project\DATN\Code\project\data\ongx2_65cm_lan4_Raw_0.bin" --model rf
    python classify.py "D:\An Engr\Project\DATN\Code\project\data\ongx2_65cm_lan4_Raw_0.bin" --no-plot
"""

import os
import sys
import tempfile
import argparse
import numpy as np

from extract_frames import read_frames, file_info
from inference import predict
from usage_guide import plot_range_time_map, plot_spectrogram


def run(bin_path: str,
        start_frame: int,
        n_frames: int = 30,
        model_name: str = 'svm',
        range_bin_min: int = 15,
        range_bin_max: int = 20,
        plot: bool = True) -> dict:

    # ── Buoc 1: Extract frames ────────────────────────────────────────────────
    print(f"\n[1/4] Extract  frame {start_frame} -> {start_frame + n_frames - 1} ...")
    raw_data = read_frames(bin_path, start_frame, n_frames)

    tmp_fd, tmp_path = tempfile.mkstemp(suffix='.raw')
    os.close(tmp_fd)
    raw_data.tofile(tmp_path)

    # ── Buoc 2 & 3: Process + Classify ───────────────────────────────────────
    print(f"[2/4] Process  tin hieu ...")
    print(f"[3/4] Classify voi model '{model_name}' ...")

    try:
        result = predict(
            tmp_path,
            model_name=model_name,
            range_bin_min=range_bin_min,
            range_bin_max=range_bin_max,
        )
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    # ── In ket qua ────────────────────────────────────────────────────────────
    print("\n" + "=" * 45)
    print(f"  is_insect      : {result['is_insect']}")
    print(f"  power_threshold: {result['power_threshold']:.2f}")

    if result['is_insect']:
        print(f"  prediction     : {result['prediction'].upper()}")
        if result['proba']:
            for cls, p in result['proba'].items():
                print(f"    P({cls:>3}) = {p:.4f}")
    else:
        print(f"  reason         : {result['reason']}")
    print("=" * 45)

    # ── Buoc 4: Visualize ─────────────────────────────────────────────────────
    if plot:
        print("\n[4/4] Ve do thi ...")
        label = result['prediction'].upper() if result['is_insect'] else 'BACKGROUND'
        title = f"frame {start_frame}-{start_frame + n_frames - 1} | {model_name} | {label}"
        plot_range_time_map(result, title=f"Range-Time Map | {title}")
        plot_spectrogram(result,    title=f"Micro-Doppler  | {title}")
    else:
        print("[4/4] Visualize: tat (--no-plot)")

    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Extract -> Process -> Classify -> Visualize"
    )
    parser.add_argument('bin_path',    help="Duong dan file .bin")
    parser.add_argument('start_frame', type=int, help="Frame bat dau (0-indexed)")
    parser.add_argument('--frames',    type=int, default=30,
                        help="So frame (mac dinh: 30)")
    parser.add_argument('--model',     type=str, default='svm',
                        choices=['svm', 'rf', 'xgb'],
                        help="Model: svm | rf | xgb  (mac dinh: svm)")
    parser.add_argument('--bin-min',   type=int, default=15)
    parser.add_argument('--bin-max',   type=int, default=20)
    parser.add_argument('--no-plot',   action='store_true',
                        help="Tat visualize")
    args = parser.parse_args()

    if not os.path.exists(args.bin_path):
        print(f"[ERROR] Khong tim thay: {args.bin_path}")
        sys.exit(1)

    total_frames, file_size = file_info(args.bin_path)
    max_start = total_frames - args.frames
    if not (0 <= args.start_frame <= max_start):
        print(f"[ERROR] start_frame={args.start_frame} khong hop le "
              f"(phai trong [0, {max_start}]).")
        sys.exit(1)

    run(
        bin_path=args.bin_path,
        start_frame=args.start_frame,
        n_frames=args.frames,
        model_name=args.model,
        range_bin_min=args.bin_min,
        range_bin_max=args.bin_max,
        plot=not args.no_plot,
    )
