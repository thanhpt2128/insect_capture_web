# P2 — preprocessing_worker (Tiến trình xử lý tín hiệu)

Nguồn: `realTimeProc_infer.py → preprocessing_worker_process()`
(DSP nằm trong `InsectRadarProcessor.process_complex`)

**Nhiệm vụ:** lấy từng lô từ P1, chạy chuỗi xử lý vi Doppler, trích đặc trưng,
rồi đẩy kết quả sang P3 qua `ai_queue`.

```mermaid
flowchart TD
    START(["Bắt đầu"]) --> CFG{"Hình học .cfg khớp mô hình?"}
    CFG -- "Không" --> FATAL["Báo lỗi cấu hình<br/>dừng toàn pipeline"]
    FATAL --> END(["Kết thúc"])
    CFG -- "Có" --> INIT["Tạo InsectRadarProcessor"]

    INIT --> LOOP{"Còn chạy?"}
    LOOP -- "Không" --> END
    LOOP -- "Có" --> GET["Lấy 1 lô từ preprocess_queue"]
    GET -- "Hết hạn chờ" --> LOOP
    GET --> VAL{"Kích thước lô đúng?"}
    VAL -- "Không" --> LOOP
    VAL -- "Có" --> DSP["Xử lý vi Doppler:<br/>Range-FFT → bám đỉnh cự ly → slow-time<br/>→ lọc thông cao → STFT → ngưỡng năng lượng → đặc trưng"]
    DSP --> BUILD["Đóng gói: is_insect, power_threshold,<br/>đặc trưng, dữ liệu vẽ (range_plot)"]
    BUILD --> PUSH["ai_queue → P3"]
    PUSH --> LOOP

    DSP -. "Lỗi 1 lô" .-> SKIP["Bỏ qua lô, tiếp tục"]
    SKIP --> LOOP
```

**Điểm cần nhớ:**
- Nếu `.cfg` không khớp hình học mô hình thì **dừng cả pipeline** ngay, tránh chạy sai âm thầm.
- Lô **nền** → không có đặc trưng (kèm `reason`); lô **côn trùng** → có đủ đặc trưng cho P3.
- Lỗi một lô chỉ bỏ qua lô đó (khác P1/P3 vốn dừng cả pipeline khi lỗi).
- `ai_queue` cũng là hàng đợi **drop-oldest**.
