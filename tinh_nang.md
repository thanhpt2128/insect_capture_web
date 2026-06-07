Kiến trúc Backend

Framework: FastAPI (entry: main.py).
Server chạy thường với uvicorn (listed in requirements.txt).
Template engine: Jinja2; static files được mount tại /static (thư mục static).
Các route được tổ chức trong api: camera, capture, cli, com, config, video, session, health.
Khối xử lý (processing block)

CLI controller: CliController trong cli_controller.py — khởi tiến trình CLI (subprocess), gửi lệnh qua stdin, đọc stdout, giữ log, callback on-exit.

Radar metrics / xử lý cấu hình: radar_metrics.py — phân tích file cấu hình cfg để tính toán kích thước radar-cube, FFT bins, range/velocity resolution, v.v. 
COM / Serial: ComService trong com_service.py — liệt kê cổng COM bằng pyserial.
Frontend

Single-page UI template: index.html.
Tĩnh JS/CSS: app.js (gọi API REST nội bộ, cập nhật UI), styles.css.
UI fetch tới các endpoint 
Công nghệ chính

Python 3 + FastAPI (ASGI) + Uvicorn
Jinja2 templates + Vanilla JS frontend (fetch API)
PySerial (pyserial) cho COM port
File I/O & filesystem-based session management (Pathlib)
Tệp/điểm quan trọng



FastAPI kích hoạt một Process  chạy pyRadar để nhận tín hiệu, xử lý DSP, chạy mô hình AI trực tiếp từ bộ nhớ và trả chuỗi JSON kết quả suy luận về cho FastAPI qua Socket để đẩy lên Web App.

Sử dụng lõi xử lý pyRadar để tiếp nhận trực tiếp luồng tín hiệu từ cổng mạng mà không lưu vào đĩa.  

Sau khi chạy các thuật toán DSP (FFTs, DBSCAN...) và mô hình AI tích hợp , Process này đóng gói dữ liệu kết quả thành dạng JSON và truyền ngược về cho FastAPI Core qua cổng Local Socket.


Tích hợp Real-time vào FastAPI
Việc chạy pyRadar bên trong FastAPI yêu cầu xử lý các tác vụ nặng về CPU một cách bất đồng bộ. Thay vì chạy vòng lặp xử lý chính trong luồng của FastAPI (điều này sẽ làm treo máy chủ web), hệ thống nên sử dụng loop.run_in_executor kết hợp với ProcessPoolExecutor. Điều này cho phép tận dụng đa nhân của CPU để thực hiện các phép tính FFT và suy luận AI (inference) trong khi FastAPI vẫn có thể phản hồi các yêu cầu từ dashboard giao diện người dùng qua WebSocket để hiển thị kết quả suy luận dữ liệu thời gian thực.

tôi muốn có các phần sau trên ui
bắt đầu quá trình chạy
dừng quá trình chạy
kết quả sau suy luận ( chỉ làm dạng plain code demo chưa cần xử lý dsp hay ai gì)
phát hiện com chọn com
chọn file config trong folder trên máy
xem radar metrics với file config tương ứng


