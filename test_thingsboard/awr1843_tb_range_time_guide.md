# Hướng dẫn tối giản: AWR1843 + Python + ThingsBoard để vẽ Range-Time realtime

Mục tiêu của cấu hình này là:

- đọc raw `.bin` từ AWR1843/DCA1000,
- tính Range FFT trong Python,
- đẩy từng frame lên ThingsBoard,
- hiển thị dạng realtime liên tục giống video/waterfall,
- hạn chế hoặc tránh lưu stream tốc độ cao vào database của ThingsBoard.

## 1) Kiến trúc đề xuất

```text
AWR1843 + DCA1000
        │
        │ raw .bin
        ▼
Python publisher
        │
        ├─ parse frame
        ├─ window + Range FFT
        ├─ build range_profile
        ▼
ThingsBoard MQTT broker
        │
        ├─ telemetry ingest
        ├─ rule chain
        └─ dashboard realtime
                │
                ▼
     Custom widget (ECharts heatmap)
```

## 2) Vì sao vẫn dùng ThingsBoard

ThingsBoard phù hợp khi bạn muốn:

- nhiều người cùng xem dashboard,
- sau này nhiều device,
- quản lý user / customer / dashboard tốt hơn,
- mở rộng hệ thống IoT sau này.

Nó có dashboard realtime, custom widget, và hỗ trợ widget development bằng HTML/CSS/JS; ngoài chart có sẵn, bạn cũng có thể nhúng ECharts cho các kiểu hiển thị nâng cao.

## 3) Chọn hướng truyền nhanh nhất

Để giảm overhead:

- dùng MQTT,
- dùng access token của device,
- publish telemetry qua topic ngắn `v2/t`,
- gửi payload JSON gọn,
- chỉ gửi đúng phần cần hiển thị.

Khuyến nghị payload mỗi frame:

```json
{
  "ts": 1710000000000,
  "values": {
    "frame_id": 12,
    "bins": 512,
    "range_profile": [12.3, 13.1, 10.8]
  }
}
```

## 4) Các bước trên ThingsBoard

### Bước 1: tạo device
- Vào **Entities → Devices**.
- Bấm **Add device**.
- Đặt tên, ví dụ `awr1843_01`.
- Mở device rồi lấy **Access token** trong phần credentials.

### Bước 2: tạo dashboard
- Vào **Dashboards**.
- Bấm **Add dashboard**.
- Đặt tên, ví dụ `Radar Realtime`.
- Mở dashboard rồi vào chế độ edit.

### Bước 3: tạo alias cho device
- Trong dashboard, mở **Entity aliases**.
- Tạo alias trỏ tới device `awr1843_01`.

### Bước 4: test bằng chart đơn giản trước
- Dùng chart mặc định để xem các key như `frame_id` hoặc `bins`.
- Khi telemetry chạy ổn, chuyển sang custom widget heatmap.

### Bước 5: tạo custom widget cho range-time
- Dùng custom widget.
- Chèn ECharts trong Resources.
- Vẽ heatmap/waterfall bằng dữ liệu `range_profile`.
- Widget giữ một buffer 2D trong bộ nhớ trình duyệt và chỉ redraw liên tục.

## 5) Cách để không lưu stream cao vào DB

Nếu deployment của bạn hỗ trợ rule chain cấu hình realtime-only, hãy để stream này đi theo nhánh chỉ phục vụ WebSocket dashboard, không ghi xuống storage lâu dài. Mục tiêu là giữ dashboard cập nhật liên tục nhưng không tạo dữ liệu lịch sử khổng lồ.

Nếu hệ thống của bạn buộc phải ghi telemetry mặc định, hãy ít nhất:

- giảm tần suất gửi,
- chỉ gửi bin cần thiết,
- hoặc chỉ gửi kênh đã rút gọn như max/peak/ROI.

## 6) Code Python mẫu

File mẫu:
- `awr1843_tb_publisher.py`

Chức năng:
- mở file `.bin`,
- đọc frame,
- tạo complex IQ,
- window + FFT,
- publish MQTT lên ThingsBoard.

Chạy bằng:

```bash
pip install numpy paho-mqtt
python awr1843_tb_publisher.py
```

## 7) Chỗ cần sửa theo file bin thực tế

Trong project thật, phần quan trọng nhất là parser `.bin`.

Cần xác định:
- file là raw stream từ DCA1000 hay file đã được đóng gói lại,
- số sample mỗi frame,
- số chirp,
- số RX/TX,
- thứ tự I/Q,
- kiểu dữ liệu `int16` hay định dạng khác.

Phần `read_complex_frames_from_bin()` trong file Python hiện chỉ là mẫu khung để thay thế.

## 8) Mục tiêu hiển thị range-time

Widget không nên chỉ vẽ 1 frame rồi mất.
Nó պետք phải:

- nhận frame mới,
- thêm vào ma trận 2D,
- giữ một rolling window, ví dụ 30–200 frame,
- redraw ngay.

Cảm giác cuối cùng phải giống video/waterfall liên tục.

## 9) Gợi ý cho AI agent khi sửa tiếp

Nếu cần mở rộng, hãy ưu tiên theo thứ tự:
1. hoàn thiện parser `.bin`,
2. cố định format payload,
3. viết custom widget heatmap,
4. thêm buffer rolling window,
5. tối ưu tốc độ gửi và render.
