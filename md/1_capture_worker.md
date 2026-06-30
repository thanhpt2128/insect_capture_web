# P1 — capture_worker (Tiến trình thu nhận)

Nguồn: `realTimeProc_infer.py → capture_worker_process()`

**Nhiệm vụ:** cấu hình & khởi động AWR1843 + DCA1000, lấy từng khung ADC qua luồng UDP nền,
gom đủ một lô (30 khung) rồi đẩy sang P2 qua `preprocess_queue`.

```mermaid
flowchart TD
    START(["Bắt đầu"]) --> SETUP["Cấu hình DCA1000 + nạp .cfg<br/>Khởi động luồng UDP nền + startSensor"]
    SETUP --> LOG["log: CAPTURE_START"]
    LOG --> LOOP{"Còn chạy?"}

    LOOP -- "Không" --> STOP["Dừng sensor, đóng DCA1000<br/>log: CAPTURE_STOP"]
    STOP --> END(["Kết thúc"])

    LOOP -- "Có" --> GET["Lấy 1 khung ADC từ luồng UDP"]
    GET --> CHK{"Khung hợp lệ?"}
    CHK -- "Không" --> LOOP
    CHK -- "Có" --> DEC["Giải mã int16 → IQ phức (QQII)<br/>Thêm vào lô hiện tại"]
    DEC --> FULL{"Đã đủ 30 khung?"}
    FULL -- "Chưa" --> LOOP
    FULL -- "Rồi" --> PUSH["Ghép lô → preprocess_queue<br/>(bắt đầu lô mới)"]
    PUSH --> LOOP

    GET -. "Lỗi" .-> ERR["log: CAPTURE_ERROR<br/>báo dừng toàn pipeline"]
    ERR --> STOP
```

**Điểm cần nhớ:**
- Đọc **từng khung một**, việc gom 30 khung thành lô diễn ra ở phía Python.
- `preprocess_queue` là hàng đợi **drop-oldest**: P2 chậm thì lô cũ bị bỏ để giữ dữ liệu mới.
- Lỗi thu nhận sẽ báo dừng đồng bộ cả ba tiến trình.
