# Tích hợp InsectRadarProcessor vào realTimeProc_fastapi_decoupled.py

## Tổng quan kiến trúc

File `realTimeProc_fastapi_decoupled.py` chạy 3 process song song, giao tiếp qua queue:

```
[Process 1] capture_worker_process
        │  raw ADC data (numpy int16)
        ▼  preprocess_queue
[Process 2] preprocessing_worker_process   ← ĐÃ TÍCH HỢP
        │  {is_insect, power_threshold, features, reason}
        ▼  ai_queue
[Process 3] ai_worker_process              ← ĐÃ TÍCH HỢP
        │
        ▼  TCP socket (JSON Lines)
[FastAPI Server]
```

---

## Những thay đổi đã thực hiện

### 1. Thêm vào phần đầu file

```python
import os
import tempfile

# Số int16 mỗi frame (phải khớp với RadarConfig trong insect_radar_processor.py)
# 1 TX × 4 RX × 128 ADC samples × 128 loops × 2 (I+Q) = 131 072
_INT16_PER_FRAME = 131_072
```

### 2. Thêm CLI args vào `main()`

| Arg | Mặc định | Mô tả |
|-----|----------|-------|
| `--model` | `svm` | Model phân loại: `svm` \| `rf` \| `xgb` |
| `--range-bin-min` | `15` | Range bin bắt đầu vùng quan tâm |
| `--range-bin-max` | `20` | Range bin kết thúc vùng quan tâm |
| `--numframes` | `2` → phải đặt `30` | Số frame mỗi lần capture (bắt buộc = 30) |

---

## Luồng xử lý chi tiết

### Process 2 — `preprocessing_worker_process`

**Trước tích hợp:** chỉ tính `mean/std/min/max` của raw bytes, không có DSP.

**Sau tích hợp:** chạy full pipeline xử lý tín hiệu radar.

```
[Khởi tạo — chạy 1 lần trước vòng lặp]
├── Import InsectRadarProcessor
├── Tạo processor (range_bin_min, range_bin_max)
└── Tạo 1 temp file cố định (ghi đè mỗi iteration, tránh overhead tạo/xóa)

[Vòng lặp — mỗi batch frames]
│
├── 1. Lấy (seq, ts, raw_data) từ preprocess_queue
│
├── 2. Validate kích thước
│       actual_int16 == numframes × 131_072 ?
│       Không → in cảnh báo, skip
│
├── 3. Ghi raw_data vào temp file
│       np.asarray(raw_data, dtype=np.int16).tofile(tmp_path)
│
├── 4. Gọi processor.process(tmp_path)
│       ├── Đọc int16 → IQ complex
│       ├── Reshape → radar cube [Frame, TX, RX, Loop, Sample]
│       ├── Static clutter removal (trừ mean theo chiều Loop)
│       ├── Hanning window + Range FFT → [Frame, RX, Loop, RangeBin]
│       ├── Range-Time Map + peak tracking
│       ├── Trích slow-time signal tại 3 range bins xung quanh peak
│       ├── High-pass filter (loại clutter chậm, cutoff = 50 Hz)
│       ├── STFT tổng hợp 12 spectrograms (3 bins × 4 anten)
│       ├── Tính power_threshold (tổng power dải 50–800 Hz)
│       │       > 45 000 → có côn trùng → tiếp tục trích features
│       │       ≤ 45 000 → background → trả về is_insect=False
│       └── Trích xuất 58 features (nếu is_insect=True)
│               ├── Band power ratios (150-350 Hz, 350-800 Hz, ...)
│               ├── Spectral entropy, flatness, centroid, bandwidth
│               ├── Ridge frequency mean/std
│               └── MFCC (8 hệ số × 3 thống kê = 24 features)
│
├── 5. Strip viz data (RTM, spectrogram — numpy arrays nặng, không cần downstream)
│
├── 6. Đẩy vào ai_queue:
│       {
│           "is_insect":       bool,
│           "power_threshold": float,
│           "features":        dict (58 keys) | None,
│           "reason":          str | None
│       }
│
└── [finally] Xóa temp file khi process kết thúc
```

---

### Process 3 — `ai_worker_process`

**Trước tích hợp:** placeholder cứng `label: "hardware-capture"`, không có inference.

**Sau tích hợp:** load model thật, phân loại theo features.

```
[Khởi tạo — chạy 1 lần trước vòng lặp]
├── Import joblib
├── Load model từ models/<model_name>.pkl
│       svm → svm_pipeline.pkl   (Pipeline: StandardScaler + SVM)
│       rf  → randomforest.pkl
│       xgb → xgboost.pkl
├── Load feature_names từ models/feature_names.pkl
│       (danh sách 58 tên feature, đúng thứ tự khi train)
└── Kết nối TCP socket đến FastAPI server

[Vòng lặp — mỗi batch]
│
├── 1. Poll socket — nhận lệnh điều khiển từ FastAPI
│       cmd == "stop" → set exit_event, thoát
│
├── 2. Lấy (seq, ts, proc_result) từ ai_queue
│
├── 3. Inference
│       ┌── is_insect == False
│       │       ai_result = {label: "background", score: None, proba: None, reason: ...}
│       │
│       └── is_insect == True
│               ├── Build X: lấy features theo đúng thứ tự feature_names
│               │       X = [features[n] for n in feature_names]  shape (1, 58)
│               ├── model.predict(X) → label thô
│               ├── Decode label (XGBoost dùng numeric → decode qua _label_encoder)
│               ├── model.predict_proba(X) → {bee: float, fly: float}
│               └── ai_result = {label, score (max proba), proba}
│
├── 4. Build payload JSON và gửi qua TCP socket
│       {
│           "type":            "inference",
│           "mode":            "hardware",
│           "seq":             int,
│           "ts":              float,
│           "com_port":        str,
│           "cfg_path":        str,
│           "is_insect":       bool,       ← MỚI
│           "power_threshold": float,      ← MỚI
│           "result": {
│               "label":  "bee" | "fly" | "background" | "error",
│               "score":  float | None,
│               "proba":  {"bee": float, "fly": float} | None,
│               "reason": str | None       (chỉ khi background)
│           }
│       }
│
└── 5. Sleep interval (nếu args_dict["interval"] > 0)
```

---

## Cách chạy

```bash
python realTimeProc_fastapi_decoupled.py \
  --server-port 8765          \
  --com-port COM3             \
  --cfg-path ongx2_65cm.cfg  \
  --numframes 30              \
  --model svm                 \
  --range-bin-min 15          \
  --range-bin-max 20
```

> **Quan trọng:** `--numframes` **bắt buộc phải = 30**. InsectRadarProcessor kỳ vọng
> đúng 30 frame (`30 × 131 072 = 3 932 160` int16 values). Nếu sai, preprocessing
> worker sẽ in cảnh báo và skip toàn bộ batch đó.

---

## Cấu trúc thư mục models/ cần có

```
project/
└── models/
    ├── feature_names.pkl      # list[str], 58 tên feature
    ├── svm_pipeline.pkl       # sklearn Pipeline (StandardScaler + SVM)
    ├── randomforest.pkl       # RandomForestClassifier
    └── xgboost.pkl            # XGBClassifier (có _label_encoder nếu dùng numeric labels)
```

---

## Lưu ý kỹ thuật

| Vấn đề | Giải pháp |
|--------|-----------|
| `InsectRadarProcessor.process()` chỉ đọc file | Dùng 1 temp file cố định, ghi đè mỗi lần — tránh tạo/xóa liên tục |
| `viz` data (RTM, spectrogram) nặng | Strip trước khi đẩy vào queue, chỉ giữ features |
| Model load lâu | Load **1 lần** trước vòng lặp, không load trong loop |
| Windows spawn mode | Tất cả import nặng (`InsectRadarProcessor`, `joblib`) đặt trong thân hàm worker, không ở module level |
| XGBoost dùng numeric label | Decode qua `model._label_encoder` nếu tồn tại, fallback về `model.classes_` |
