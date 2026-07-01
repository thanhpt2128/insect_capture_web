# test/ — Kiểm thử & benchmark pipeline realtime AI

Bộ test/benchmark cho luồng GUI realtime ([`realtime_gui.py`](../realtime_gui.py) →
[`realTimeProc_infer.py`](../realTimeProc_infer.py)). **Không cần phần cứng** — dùng
dữ liệu raw thật trong `data_parse/` và model thật trong
`insect_radar_processor/models/`.

## Chạy

```powershell
# từ thư mục gốc repo, dùng python của venv
.venv\Scripts\python.exe test\run_all.py            # chạy toàn bộ test correctness
.venv\Scripts\python.exe test\test_drop_oldest_queue.py
.venv\Scripts\python.exe test\test_pipeline_functional.py
.venv\Scripts\python.exe test\bench_dsp_inference.py
.venv\Scripts\python.exe test\bench_queue.py
```

> Trên Windows nên đặt `PYTHONUTF8=1` để console in được ký tự Unicode:
> `$env:PYTHONUTF8=1; .venv\Scripts\python.exe test\run_all.py`

## Các file

| File | Loại | Nội dung |
|------|------|----------|
| `test_drop_oldest_queue.py` | correctness | FIFO, bounded, không reorder, **drop-oldest đúng 100%** (single-proc burst + cross-proc bão hòa payload 16MB). Exit 1 nếu FAIL. |
| `test_pipeline_functional.py` | correctness | Alignment 58 feature, schema `process_complex`, tương đương IQ order QQII↔IIQQ, hình học cfg=131072, smoke inference svm. |
| `test_sliding_window.py` | correctness | Logic cửa sổ trượt `--stride`: kích thước cửa sổ, bước trượt, chồng lấp, tumbling mặc định, quy tắc resolve. |
| `bench_dsp_inference.py` | benchmark | Thời gian từng tầng P1/P2/P3 + soak 150 lô (rò rỉ RAM, trôi độ trễ) + kết luận ngân sách thời gian thực. |
| `bench_queue.py` | benchmark | Thông lượng put (payload nhỏ & 16MB) + độ trễ cross-process (chế độ thật & bão hòa). |
| `bench_iq_fps.py` | benchmark | FPS thực tế panel I/Q (TkAgg): so cũ (full draw 4096) vs mới (blitting + downsample 1024). |
| `bench_end_to_end_latency.py` | benchmark | Mô phỏng cross-process P1->P2->P3 thật (nhịp @50fps), đo đúng 2 số GUI hiển thị: `dsp_ms` ("DSP") vs `proc_ms` ("Trễ xử lý"). |
| `bench_sliding_stride.py` | benchmark | So cửa sổ trượt stride 30/20/15 @60fps: dsp_ms, proc_ms, nhịp kết quả, backlog/drop, tải P2, độ trễ phát hiện tổng (gather + proc). |
| `GUI_CODE_QUALITY.md` | đánh giá | Phân tích chất lượng luồng code GUI: điểm tốt + điểm cần cải thiện. |
| `run_all.py` | runner | Chạy lần lượt các file *correctness*, tổng hợp PASS/FAIL. |

## Kết quả tham chiếu (máy dev, Python 3.12)

- DSP `process_complex`: ~154 ms/lô (p95 167) ≪ ngân sách 600 ms@50fps → **bắt kịp**.
- Inference: svm 0.7 ms · rf 79 ms · xgb **không load được** (chưa cài `xgboost`).
- Soak 150 lô: **không rò rỉ RAM, không trôi độ trễ**.
- DropOldestQueue (sau fix): **giữ phần tử mới nhất 100%** mọi kịch bản, kể cả
  payload 16MB cross-process (trước fix: 0%).
- Panel I/Q (TkAgg, sau blitting + downsample 1024): **~82 FPS** (12,2 ms/lần),
  so với **~12 FPS** (84,8 ms/lần) khi full-draw 4096 điểm trước đây.

## Phụ thuộc

`numpy scipy scikit-learn joblib` (bắt buộc), `psutil` (tùy chọn, cho phần đo RAM).
Cài `xgboost` nếu muốn benchmark/inference model xgb.
