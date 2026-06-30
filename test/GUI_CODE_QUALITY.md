# Đánh giá chất lượng code luồng GUI realtime

Phạm vi: [`realtime_gui.py`](../realtime_gui.py) (Tkinter) + cách nó điều phối worker
[`realTimeProc_infer.py`](../realTimeProc_infer.py). Đánh giá dựa trên đọc mã + đo
thực tế (xem `bench_dsp_inference.py`, `test_drop_oldest_queue.py`).

## Kiến trúc luồng

```
GUI (Tk, thread chính)                  Worker (3-4 process)
  RealtimeController.start() ──spawn──>  realTimeProc_infer.py
      mở TCP server localhost            --no-local-view --no-profile --model <chọn>
  ┌── thread _read_stdout  ←─ stdout ──  P1 capture → P2 DSP → P3 AI
  └── thread _accept_and_read ← TCP ───  P3 gửi JSON Lines (inference + iq_preview)
  log_q / result_q  ──(80ms tick)──> vẽ matplotlib trong Tk
  Stop: gửi {"cmd":"stop"} qua chính socket đó
```

Phân tách đúng: **I/O nền ở thread riêng**, **vẽ ở thread chính** qua hàng đợi —
không vi phạm quy tắc Tk "chỉ chạm widget từ main thread".

## Điểm tốt (chất lượng cao)

1. **Coalescing khi vẽ.** `_tick` (80 ms ≈ 12.5 Hz) drain *hết* queue rồi chỉ giữ
   `latest_range_plot` / `latest_iq_plot` và vẽ **một lần**
   ([realtime_gui.py:431-464](../realtime_gui.py#L431-L464)). iq_preview tới ~loop-rate
   nhưng chỉ frame mới nhất được vẽ → không nghẽn UI.
2. **Truyền cờ worker đúng.** GUI truyền `--no-local-view` (không mở cửa sổ plot thứ
   4 thừa) và `--no-profile` (tắt in `[T ...]`), lại còn lọc dòng `[T ` trong log
   ([realtime_gui.py:118](../realtime_gui.py#L118), [627](../realtime_gui.py#L627)).
3. **Vẽ tăng tiến, không tạo artist mới mỗi frame.** Waterfall dùng `set_data` +
   `set_clim`, range/iq dùng `set_ydata`/`set_data`, gọi `draw_idle` (deferred).
   Buffer cuộn bằng `np.roll` cấp phát sẵn. Rẻ và ổn định.
4. **Dọn tài nguyên khi đóng.** `WM_DELETE_WINDOW → _on_close` gọi `ctrl.stop()`;
   `stop()` gửi lệnh dừng êm trước, hết 5 s mới `terminate()`; `_cleanup` đóng
   socket + server.
5. **Log có giới hạn** (~500 dòng, tự cắt) → không phình bộ nhớ UI khi chạy lâu.
6. **Tách metrics dùng lại** module web qua `importlib` thay vì chép logic.
7. **Đã đo:** DSP 154 ms ≪ ngân sách 600 ms@50fps; svm 0.7 ms; **không rò rỉ RAM,
   không trôi độ trễ** qua 150 lô. Queue đã được sửa drop-oldest đúng 100%.

## Điểm cần cải thiện (xếp theo mức độ)

| # | Mức | Vấn đề | Vị trí | Gợi ý |
|---|-----|--------|--------|-------|
| 1 | 🔴 | Dropdown Model có **`xgb`** nhưng `xgboost` chưa cài → chọn xgb làm worker `MODEL_LOAD_FAILED` rồi chết, GUI chỉ hiện "[worker thoát, code 1]" (khó hiểu với người dùng). | [realtime_gui.py:278](../realtime_gui.py#L278) | Bỏ `xgb` khỏi `values` cho tới khi cài; hoặc worker báo lỗi rõ + GUI `messagebox`. |
| 2 | 🟡 | Stop trong cửa sổ reset ~7 s đầu: P3 chưa nối socket (`self.conn=None`) → `stop` không gửi được → `terminate()` cứng sau 5 s. | [realtime_gui.py:129-147](../realtime_gui.py#L129-L147), [realTimeProc_infer.py:441](../realTimeProc_infer.py#L441) | Nếu chưa có `conn`, `terminate()` ngay thay vì chờ 5 s. |
| 3 | 🟡 | `result_q` là `queue.Queue` **không giới hạn**. Bình thường `_tick` drain hết mỗi 80 ms nên an toàn, nhưng nếu thread chính kẹt (vẽ chậm/treo) thì iq_preview ~loop-rate có thể dồn ứ. | [realtime_gui.py:94](../realtime_gui.py#L94) | Đặt `maxsize` + drop khi đầy (đối xứng với drop-oldest của worker). |
| 4 | 🟡 | `_accept_and_read` gán `self.conn = conn` mỗi lần accept; nếu worker nối lại (hoặc nhiều kết nối) thì `conn` cũ bị ghi đè, lệnh stop chỉ tới conn mới nhất. Pipeline hiện chỉ 1 kết nối nên chưa lộ. | [realtime_gui.py:160-196](../realtime_gui.py#L160-L196) | Giữ danh sách conn hoặc khẳng định single-connection. |
| 5 | 🟢 | Không xác thực hình học `.cfg` khớp model phía GUI — phụ thuộc worker fail-fast (P2) rồi mới thấy qua log. | — | Gọi `_frame_int16_from_cfg` khi chọn cfg, cảnh báo sớm trên UI. |
| 6 | 🟢 | `numframes` cho người dùng sửa tự do, nhưng InsectRadarProcessor **yêu cầu 30**; ≠30 chỉ in cảnh báo trong log. | [realtime_gui.py:276](../realtime_gui.py#L276) | Khóa = 30 hoặc cảnh báo trên UI. |

## Kết luận

Luồng GUI **thiết kế tốt và đúng nguyên tắc** (tách thread I/O ↔ vẽ, coalescing,
vẽ tăng tiến, dọn tài nguyên, log có giới hạn) và **đạt yêu cầu thời gian thực**
với biên rộng. Rủi ro còn lại **không nằm ở hiệu năng** mà ở **trải nghiệm lỗi**:
nổi bật nhất là **#1 (dropdown xgb gây chết im lặng)** nên xử lý trước; #2–#4 là
gia cố độ bền (robustness) nên làm khi có thời gian.
