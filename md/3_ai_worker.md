# P3 — ai_worker (Tiến trình suy luận)

Nguồn: `realTimeProc_infer.py → ai_worker_process()`

**Nhiệm vụ:** nạp mô hình một lần, suy luận nhãn cho mỗi lô, gửi kết quả về GUI (TCP),
đẩy telemetry lên ThingsBoard (MQTT) và ghi SQLite khi nhãn thay đổi.

## Khởi tạo

```mermaid
flowchart TD
    START(["Bắt đầu"]) --> MODEL{"Nạp mô hình OK?"}
    MODEL -- "Không" --> FAIL["log: MODEL_LOAD_FAILED<br/>dừng pipeline"]
    MODEL -- "Có" --> CONN["Mở SQLite + kết nối TCP về GUI<br/>(gửi status: ready)"]
    CONN --> TB{"ThingsBoard bật & có cấu hình?"}
    TB -- "Có" --> TBON["Kết nối MQTT (tự kết nối lại 1–60s)"]
    TB -- "Không" --> TBOFF["log: TB_DISABLED"]
    TBON --> LOOP(["Vào vòng lặp"])
    TBOFF --> LOOP
```

## Vòng lặp xử lý mỗi lô

```mermaid
flowchart TD
    LOOP{"Còn chạy?"} -- "Không" --> FIN["Đóng SQLite, ngắt MQTT, đóng socket"]
    FIN --> END(["Kết thúc"])

    LOOP -- "Có" --> STOP{"Nhận lệnh stop từ GUI?"}
    STOP -- "Có" --> DOSTOP["log: STOP_COMMAND<br/>báo dừng pipeline"]
    DOSTOP --> FIN
    STOP -- "Không" --> GET["Lấy 1 lô từ ai_queue"]
    GET -- "Hết hạn chờ" --> LOOP

    GET --> INFER{"Lô có côn trùng?"}
    INFER -- "Không" --> BG["nhãn = background"]
    INFER -- "Có" --> PRED["Suy luận: nhãn + xác suất + độ tin cậy"]
    BG --> SEND
    PRED --> SEND

    SEND["Gửi kết quả JSON về GUI (TCP)"]
    SEND --> MQTT{"ThingsBoard bật?"}
    MQTT -- "Có" --> PUB["Publish telemetry (MQTT, QoS 1)"]
    MQTT -- "Không" --> SQL
    PUB --> SQL

    SQL{"Nhãn khác lần ghi trước?"}
    SQL -- "Có" --> WRITE["Ghi SQLite + log: DETECTION_CHANGE"]
    SQL -- "Không" --> LOOP
    WRITE --> LOOP
```

**Điểm cần nhớ:**
- Mô hình nạp **một lần** trước vòng lặp; lỗi nạp → dừng pipeline.
- **GUI là server, P3 là client**; lệnh dừng đến từ GUI qua chính kết nối TCP đó.
- Telemetry đẩy **mỗi lô**; lỗi MQTT chỉ cảnh báo, không làm sập pipeline.
- SQLite **chỉ ghi khi nhãn thay đổi** (bỏ qua nhãn `error`) → cơ sở dữ liệu gọn, lưu mốc đổi loài.
