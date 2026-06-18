# pyRadar Web (FastAPI) — Hướng dẫn sử dụng

Web UI tối giản cho pyRadar, xây bằng FastAPI + Jinja2 (single page), đáp ứng các mục UI trong `tinh_nang.md`:

- Bắt đầu / dừng quá trình real-time
- Hiển thị kết quả capture / inference JSON dạng plain code
- Quét COM và chọn COM
- Chọn file cấu hình radar `.cfg` (mặc định từ thư mục `configFiles/` của repo)
- Xem radar metrics tương ứng với file cấu hình

## Yêu cầu

- Khuyến nghị Python `3.10+`
- Windows: cần driver/thiết bị để thấy COM ports (nếu có phần cứng)

## Cài đặt

Chạy trong PowerShell tại thư mục `pyradar_web/`:

```powershell
python -m pip install -r requirements.txt
```

### Tạo và dùng môi trường ảo (khuyến nghị)

Nên tạo một `virtual environment` để cô lập phụ thuộc khi chạy web app. Dưới đây là các bước cho Windows (PowerShell / CMD) và POSIX (macOS / Linux).

- Windows (PowerShell):

```powershell
cd pyradar_web
python -m venv .venv
# Kích hoạt (PowerShell)
.\.venv\Scripts\Activate
# (nếu gặp lỗi execution policy, chạy PowerShell với quyền admin hoặc dùng cmd option below)
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

- Windows (CMD):

```cmd
cd pyradar_web
python -m venv .venv
\.venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

- macOS / Linux (bash/zsh):

```bash
cd pyradar_web
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Sau khi cài xong, chạy server như phần hướng dẫn bên dưới.

## Chạy server

Tại thư mục `pyradar_web/`:

```powershell
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Mở trình duyệt:

- http://127.0.0.1:8000

## Cách dùng trên UI

1) **Quét COM**

- Bấm `Quét lại`
- Chọn COM trong dropdown

2) **Chọn file cấu hình `.cfg`**

- Bấm `Tải lại` để load danh sách `.cfg` trong `configFiles/`
- Chọn file trong dropdown (ô nhập `cfgPath` sẽ tự điền)
- Có thể nhập thủ công đường dẫn tuyệt đối, hoặc chỉ nhập tên file (ví dụ: `cfg128_128_100fps.cfg`)

3) **Xem radar metrics**

- Bấm `Radar metrics` để gọi API và hiển thị metrics trong khung bên dưới

4) **Bắt đầu / Dừng real-time**

- Bấm `Bắt đầu` để spawn worker hardware-only và mở WebSocket stream kết quả
- Kết quả JSON sẽ được append liên tục vào khung `kết quả`
- Bấm `Dừng` để gửi lệnh stop (best-effort) và dừng worker

## API endpoints chính

- `GET /health`
- `GET /com/list` — liệt kê COM (pyserial)
- `GET /config/list` — liệt kê `.cfg` trong `configFiles/`
- `GET /config/metrics?path=...` — tính metrics từ `.cfg`
- `POST /realtime/start` — start worker hardware-only (payload: `com_port`, `cfg_path`, `cli_baud`)
- `POST /realtime/stop` — stop worker
- `GET /realtime/status`
- `GET /realtime/results?tail=50`
- `WS /realtime/ws/results?tail=50&interval=0.5`

## Worker real-time

- FastAPI sẽ spawn một process worker ở repo root: `realTimeProc_fastapi.py`
- Worker kết nối về FastAPI qua localhost TCP socket và gửi newline-delimited JSON
- Worker luôn chạy hardware capture qua DCA1000 UDP
- Kết quả stream là JSON capture summary / inference placeholder, không còn chế độ demo riêng

## Chạy hardware capture (DCA1000)

Yêu cầu (tóm tắt):

- Có board + DCA1000, mạng/IPv4 cấu hình đúng như repo đang dùng (mặc định `static_ip=192.168.33.30`, `adc_ip=192.168.33.180`)
- Build/installed module `fpga_udp` (C++/pybind) để dùng `fastRead_in_Cpp_thread_get`

Gọi API start (PowerShell):

```powershell
$body = @{ com_port = "COM5"; cfg_path = "cfg128_128_100fps.cfg"; cli_baud = 921600 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/realtime/start -ContentType "application/json" -Body $body
```

Nếu worker không nhận được dữ liệu UDP, hãy kiểm tra lại IP/port, firewall, và việc kết nối DCA1000.

## Troubleshooting

- Không thấy COM ports:
	- Kiểm tra thiết bị/driver, rút/cắm lại, hoặc chạy VS Code/terminal với quyền phù hợp
	- Đảm bảo đã `pip install -r requirements.txt` (pyserial)
- Danh sách `.cfg` rỗng:
	- Kiểm tra thư mục `configFiles/` ở root repo có file `.cfg`
- VS Code báo `fastapi/pydantic/serial` “could not be resolved”:
	- Thường là do chưa chọn đúng Python interpreter hoặc chưa cài requirements

## Ghi chú an toàn

Mặc định server chạy `127.0.0.1` (local-only). Nếu đổi sang `0.0.0.0` để truy cập từ máy khác, hãy cân nhắc bảo mật mạng nội bộ.


.venv\Scripts\activate 
 pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
