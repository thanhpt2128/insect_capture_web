"""
inference.py
============
Phân loại côn trùng từ file .raw radar AWR1843.

Cách dùng:
    python inference.py path/to/file.raw
    python inference.py path/to/file.raw --model rf
    python inference.py path/to/file.raw --model xgb
"""

import argparse
import numpy as np
import joblib
import os
import sys

from insect_radar_processor import InsectRadarProcessor


# -- Khởi tạo (load 1 lần khi start) ------------------------------------------

MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'models')

def _load_resources(model_name: str):
    feat_path = os.path.join(MODELS_DIR, 'feature_names.pkl')
    if not os.path.exists(feat_path):
        raise FileNotFoundError(f"Không tìm thấy {feat_path}. Chạy Task 1 trước.")

    model_files = {
        'svm': 'svm_pipeline.pkl',
        'rf' : 'randomforest.pkl',
        'xgb': 'xgboost.pkl',
    }
    if model_name not in model_files:
        raise ValueError(f"model_name phải là: {list(model_files.keys())}")

    model_path = os.path.join(MODELS_DIR, model_files[model_name])
    if not os.path.exists(model_path):
        raise FileNotFoundError(f"Không tìm thấy {model_path}. Chạy Task 2 trước.")

    return joblib.load(model_path), joblib.load(feat_path)


# -- Hàm predict chính ----------------------------------------------------------

def predict(raw_path: str,
            model_name: str = 'svm',
            range_bin_min: int = 15,
            range_bin_max: int = 20) -> dict:
    """
    Phân loại côn trùng từ 1 file .raw (30 frames).

    Returns
    -------
    dict:
        'prediction'  : 'bee' | 'fly' | None  (None nếu không có côn trùng)
        'is_insect'   : bool
        'flatness'    : float
        'proba'       : dict {'bee': float, 'fly': float} hoặc None
        'reason'      : str  (nếu is_insect=False)
        'viz'         : dict (dùng để plot RTM và spectrogram)
    """
    processor = InsectRadarProcessor(
        range_bin_min=range_bin_min,
        range_bin_max=range_bin_max,
    )
    model, feature_names = _load_resources(model_name)

    # Xử lý tín hiệu
    result = processor.process(raw_path)

    if not result['is_insect']:
        return {
            'prediction': None,
            'is_insect' : False,
            'power_threshold'  : result['power_threshold'],
            'reason'    : result['reason'],
            'proba'     : None,
            'viz'       : result['viz'],
        }

    # Kiểm tra features đầy đủ
    features = result['features']
    missing = [n for n in feature_names if n not in features]
    if missing:
        raise KeyError(f"Thiếu {len(missing)} features: {missing[:3]}...")

    # Xếp features đúng thứ tự như khi train
    X = np.array([features[n] for n in feature_names],
                 dtype=np.float64).reshape(1, -1)

    raw_pred = model.predict(X)[0]

    # XGBoost dùng numeric labels → decode về 'bee'/'fly'
    if hasattr(model, '_label_encoder'):
        pred = str(model._label_encoder.inverse_transform([raw_pred])[0])
        label_classes = model._label_encoder.classes_
    else:
        pred = str(raw_pred)
        label_classes = model.classes_

    proba = None
    if hasattr(model, 'predict_proba'):
        p = model.predict_proba(X)[0]
        proba = {str(c): float(v) for c, v in zip(label_classes, p)}

    return {
        'prediction': pred,
        'is_insect' : True,
        'power_threshold'  : result['power_threshold'],
        'proba'     : proba,
        'viz'       : result['viz'],
    }


# -- CLI ------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('raw_path',  type=str)
    parser.add_argument('--model',   type=str, default='svm',
                        choices=['svm', 'rf', 'xgb'])
    parser.add_argument('--bin_min', type=int, default=15)
    parser.add_argument('--bin_max', type=int, default=20)
    args = parser.parse_args()

    if not os.path.exists(args.raw_path):
        print(f"[ERROR] Không tìm thấy file: {args.raw_path}")
        sys.exit(1)

    print(f"\nFile  : {args.raw_path}")
    print(f"Model : {args.model}")
    print("-" * 40)

    out = predict(args.raw_path, args.model, args.bin_min, args.bin_max)

    print(f"is_insect  : {out['is_insect']}")
    print(f"power_threshold   : {out['power_threshold']:.4f}")
    if out['is_insect']:
        print(f"prediction : {out['prediction'].upper()}")
        if out['proba']:
            for cls, p in out['proba'].items():
                print(f"  P({cls:>3}) = {p:.4f}")
    else:
        print(f"reason     : {out['reason']}")
