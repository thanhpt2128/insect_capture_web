# Kết quả kiểm thử & đánh giá — Pipeline realtime `realtime_gui.py`

> Phạm vi: toàn bộ chuỗi liên quan `realtime_gui.py` → `realTimeProc_infer.py`:
> **lớp nhận UDP (fpga_udp/DCA1000)** → **hàng đợi** → **DSP** → **suy luận AI** →
> **ghi CSDL SQLite / nhật ký** → **luồng vẽ GUI**.
> Tất cả test/benchmark **không cần phần cứng** (trừ phần đo get_frames thật), dùng
> dữ liệu raw thật + model thật.

**Ngày chạy:** 2026-07-01 · **Máy:** Windows 10, Python 3.12.10 ·
**Thư viện:** numpy 2.4.6, scipy 1.17.1, scikit-learn 1.6.1, joblib 1.5.3 ·
**Dữ liệu:** `data_parse/raw_data_50fps.bin` (1000 frame, 131072 int16/frame — khớp model) ·
**Cấu hình:** `configFiles/cfg128_128_100fps.cfg` (frameCfg thực = **60 fps**, hình học khớp 131072).

Cách chạy lại: `test/README.md`. Chạy nhanh phần đúng đắn:
`$env:PYTHONUTF8=1; .venv\Scripts\python.exe test\run_all.py`

---

## 0. Tóm tắt (verdict)

| Hạng mục | Kết quả | Ghi chú |
|---|---|---|
| **Đúng đắn (23 test case, 5 nhóm)** | ✅ **TẤT CẢ PASS** | queue, pipeline, sliding, DB, lớp nhận |
| **Lớp nhận UDP (queue C++)** | ✅ ~**47 GB/s** | không bao giờ là nút thắt |
| **DSP mỗi lô** | ✅ 147–177 ms (p95 164–201) | ≪ ngân sách 500 ms @60fps |
| **Suy luận svm** | ✅ ~0.8 ms | rf ~80 ms; **xgb chưa cài** |
| **Độ trễ xử lý (proc_ms) @60fps** | ✅ ~174 ms (p95 194) | end-to-end window→infer |
| **Hàng đợi @60fps** | ✅ **0 backlog, 0 drop** | qmax = 0 |
| **DropOldestQueue (sau fix)** | ✅ giữ newest **100%** | trước fix: 0% ở ca 16MB |
| **Ghi SQLite / nhật ký** | ✅ đúng schema, chỉ ghi khi đổi nhãn, cắt dung lượng | |
| **Vẽ I/Q (sau blitting)** | ✅ **~80 FPS** | trước: ~12 FPS |
| **Rò rỉ RAM / trôi độ trễ** | ✅ không | soak 150 lô, RSS phẳng |

**Kết luận chung:** hệ thống ở cấu hình **60 fps + svm** hoạt động ổn định, bắt kịp
thời gian thực với biên an toàn lớn (~67%), không leak, không tồn đọng hàng đợi.
Rủi ro còn lại nằm ở **trải nghiệm lỗi** (dropdown `xgb` chưa cài) chứ không phải
hiệu năng.

---

## 1. Lớp nhận UDP (fpga_udp C++/pybind + DCA1000/TI) — `test_receive_module.py`

| Test | Kết quả |
|---|---|
| R1 fpga_udp có đủ hàm nhận (kfifo, get_*PacketNum, udp_read_thread_get_frames…) | ✅ PASS |
| R2 DCA1000/TI đủ method pipeline gọi (configure, stream_start, fastRead_in_Cpp_thread_*…) | ✅ PASS |
| R3 kfifo (queue C++ thuần) throughput cao | ✅ PASS |
| R4 suy fps từ frameCfg = 60 | ✅ PASS |

**Throughput queue nhận C++ (`bench_udp_fpga.py --kfifo-only`):**
```
5,000,000 lần Put() cost 0.144 s  ->  48,412 MB/s (~47 GB/s)
```
→ Lớp hàng đợi nhận **không bao giờ là nút thắt**. Nếu có trễ ở lớp nhận thì nằm ở
tốc độ *rút* (`get_frames`) hoặc jitter phần cứng, không phải queue.

> **Hạn chế:** không có radar/DCA1000 nên **không đo được** get_frames thật, loss%,
> backlog kfifo khi stream. Phần đó cần chạy `bench_udp_fpga.py --com-port … --cfg-path …`
> trên máy có phần cứng.

---

## 2. Hàng đợi DropOldestQueue — `test_drop_oldest_queue.py` + `bench_queue.py`

| Test | Kết quả |
|---|---|
| S1 FIFO khi chưa đầy | ✅ PASS |
| S2 Bounded (không phình RAM) + S4 giữ-newest (burst) | ✅ PASS |
| S3 không reorder + S4 giữ-newest (cross-proc bão hòa, payload nhỏ) | ✅ PASS |
| S3+S4 cross-proc bão hòa **payload 16MB** (ca khó nhất) | ✅ PASS |

**Trước/sau fix drop-oldest** (đã sửa `get(block=False)` → `get(timeout=…)`):

| Ca | Trước fix | Sau fix |
|---|---|---|
| Burst Q=2/4/8 giữ-newest | 89/93/99% | **100%** |
| Cross-proc bão hòa 16MB giữ-newest | **0%** | **100%** |

**Throughput / độ trễ (`bench_queue.py`):**
```
put payload nhỏ  : 17,873 item/s (0.056 ms/put)
put payload 16MB : 43 item/s (23.5 ms/put)  [= item P1->P2]
cross P1->P2 16MB: trễ TB 37.6 ms, max 45.5 ms  (drop≈0, consumer nhanh hơn)
cross P2->P3 nhỏ : trễ TB 0.4 ms, max 13.4 ms   (drop≈0)
bão hòa (consumer chậm): trễ TB 3.2 ms  -> drop-oldest giữ trễ thấp đúng ý đồ
```

---

## 3. Pipeline DSP + AI — `test_pipeline_functional.py` + `bench_dsp_inference.py`

| Test | Kết quả |
|---|---|
| F1 alignment 58 feature (đúng số & thứ tự model↔processor) | ✅ PASS |
| F2 `process_complex` đủ schema + `rtm_db` shape [30,32] | ✅ PASS |
| F3 tương đương IQ order QQII↔IIQQ | ✅ PASS |
| F4 `_frame_int16_from_cfg` = 131072 | ✅ PASS |
| F5 smoke inference svm (nhãn hợp lệ, proba tổng ≈ 1) | ✅ PASS |

**Thời gian xử lý (dữ liệu raw thật, 33 lô):**

| Tầng | mean | p95 | max |
|---|---|---|---|
| P1 decode int16→complex /frame | 0.5 ms | 0.7 ms | 1.1 ms |
| P1 build IQ preview /frame | 0.2 ms | — | 0.6 ms |
| P1 concat 30 frame /lô | 5.7 ms | 7.2 ms | 7.8 ms |
| **P2 process_complex (DSP)** | **147–177 ms** | **164–201 ms** | 211 ms |
| P2 build_range_plot | 0.6 ms | 0.7 ms | 0.9 ms |
| **P3 svm predict /lô** | **0.8 ms** | 1.1 ms | 4.6 ms |
| P3 rf predict /lô | 80 ms | 84 ms | 113 ms |
| P3 xgb | — | — | **không load được (ModuleNotFoundError)** |

> DSP dao động 147–177 ms tùy tải máy (khi chạy nhiều tiến trình song song thì cao
> hơn), nhưng luôn ≪ ngân sách 500 ms/lô @60fps.

**Soak 150 lô liên tục:** RSS **505 MB phẳng (không rò rỉ)**, độ trễ **không trôi**.

---

## 4. Độ trễ end-to-end @60fps + backlog — `bench_60fps_queue_backlog.py`

Mô phỏng đúng luồng cross-process P1→P2→P3 ở nhịp 60 fps thật (cadence 500 ms/lô):

```
[GUI 'DSP']       dsp_ms  : mean 147.7  p95 164.1  max 169.6 ms
[GUI 'Trễ xử lý'] proc_ms : mean 174.2  p95 194.2  max 198.0 ms
Hàng đợi: P1 tạo 30 lô -> P3 nhận 30 lô -> DROP 0 ; qsize lớn nhất = 0
Ngân sách/lô 500 ms ; DSP p95 164 ms ; BIÊN AN TOÀN = 336 ms -> BẮT KỊP
```

**Phân biệt 2 số GUI hiển thị** ([realtime_gui.py:304-309](realtime_gui.py#L304)):
- **"DSP"** = `dsp_ms` = chỉ thời gian `process_complex` (đo tại P2).
- **"Trễ xử lý"** = `proc_ms` = từ lúc lô đóng (P1 stamp `ts`) → inference xong (P3).
- Chênh ~26 ms = chờ hàng đợi + build_range_plot + inference. `DSP ≤ Trễ xử lý` luôn đúng.
- Đây là **thời gian tính toán thật**, hiện ~150–200 ms **kể cả khi hàng đợi rỗng** (không phải dấu hiệu backlog).

---

## 5. Cửa sổ trượt (sliding window `--stride`) — `test_sliding_window.py` + `bench_sliding_stride.py`

| Test | Kết quả |
|---|---|
| W1 mỗi cửa sổ đúng batch_frames | ✅ PASS |
| W2+W3 bước = stride & chồng lấp = numframes−stride | ✅ PASS |
| W4 tumbling (stride mặc định) không chồng lấp | ✅ PASS |
| W5 quy tắc resolve stride (0/âm/>nf → nf) | ✅ PASS |
| W6 ví dụ 30 → 15+15 (chồng lấp 50%) | ✅ PASS |

**Benchmark stride 30 / 20 / 15 @60fps (cross-process thật):**

| stride | nhịp KQ | DSP mean/p95 | proc mean/p95 | drop | tải P2 | **độ trễ phát hiện TB** |
|---|---|---|---|---|---|---|
| **30** (tumbling) | 500 ms | 149/172 ms | 177/199 ms | 1/16¹ | **30%** | **419 ms** |
| **20** (overlap 10) | 333 ms | 146/155 ms | 171/184 ms | 0/23 | **44%** | **329 ms** |
| **15** (overlap 15) | 250 ms | 149/167 ms | 176/188 ms | 0/31 | **60%** | **292 ms** |

*Độ trễ phát hiện = gather (chờ frame lọt cửa sổ) + proc_ms.*
¹ *1 lô drop ở stride=30 là lô khởi động (JIT/cache chưa nóng); lần chạy 60fps riêng ở mục 4 cho **0 drop**.*

**Nhận xét:**
- DSP/proc_ms **không đổi** theo stride (cửa sổ luôn 30 frame) → stride chỉ đổi *tần suất*, không đổi *chi phí/lô*.
- stride 30→15: độ trễ phát hiện giảm **419 → 292 ms (−30%)**, đổi lại tải P2 30% → 60%.
- Không cần train lại model; P2/P3 không đổi. Đã wire vào GUI (ô "Stride", mặc định 0 = tumbling).
- **Khuyến nghị:** stride=20 (cân bằng, tải 44%) hoặc stride=15 (nhanh nhất, tải 60% vẫn an toàn). Không đi dưới 15.

---

## 6. Ghi CSDL SQLite + nhật ký — `test_database.py`

| Test | Kết quả |
|---|---|
| D1 `open_detection_db` tạo bảng `detections` đủ cột (id, ts, label, power, score, proba) | ✅ PASS |
| D2 `insert_detection` ghi & đọc lại đúng; proba (dict) → JSON; None giữ NULL | ✅ PASS |
| D3 **chỉ ghi khi nhãn ĐỔI** (bỏ nhãn `error`) — 11 lô → 5 bản ghi đúng mốc đổi | ✅ PASS |
| D4 `_enforce_db_size_cap` vượt ngưỡng → xoá ~10% bản ghi **cũ nhất** + log `DB_TRIMMED` | ✅ PASS |
| D5 `log_event` đúng định dạng `[EVENT] thời_gian \| detail` | ✅ PASS |

→ Logic "chỉ lưu khi đổi loài" (giữ DB gọn) và cơ chế cắt dung lượng 1 GB hoạt động đúng.

---

## 7. Luồng vẽ GUI (I/Q blitting + downsample) — `bench_iq_fps.py`

| Cấu hình vẽ panel I/Q (TkAgg thực) | ms/lần | FPS |
|---|---|---|
| CŨ — full draw, 4096 điểm | 85.9 | ~12 |
| MỚI — blitting, 4096 điểm | 50.3 | ~20 |
| **MỚI — blitting + downsample 1024đ (GUI đang dùng)** | **12.4** | **~80** |

→ Render panel I/Q **12 → 80 FPS** (nhanh ~7×), không còn là nút thắt. Chi tiết đánh
giá chất lượng luồng code GUI: `test/GUI_CODE_QUALITY.md`.

---

## 8. Bảng tổng hợp test case

| Nhóm | File | Số case | Kết quả |
|---|---|---|---|
| Hàng đợi drop-oldest | `test_drop_oldest_queue.py` | 4 | ✅ PASS |
| Pipeline DSP+AI | `test_pipeline_functional.py` | 5 | ✅ PASS |
| Cửa sổ trượt | `test_sliding_window.py` | 5 | ✅ PASS |
| CSDL SQLite + log | `test_database.py` | 5 | ✅ PASS |
| Lớp nhận UDP | `test_receive_module.py` | 4 | ✅ PASS |
| **Tổng** | | **23** | ✅ **TẤT CẢ PASS** |

Benchmark (không assert, in số liệu): `bench_dsp_inference.py`, `bench_queue.py`,
`bench_iq_fps.py`, `bench_end_to_end_latency.py`, `bench_60fps_queue_backlog.py`,
`bench_sliding_stride.py`, `bench_udp_fpga.py --kfifo-only`.

---

## 9. Đánh giá tổng thể & khuyến nghị

**Điểm mạnh (đã kiểm chứng):**
1. Bắt kịp realtime 60 fps với biên an toàn **~67%** (DSP 164 ms p95 / ngân sách 500 ms).
2. Không rò rỉ RAM, không trôi độ trễ (soak 150 lô).
3. Hàng đợi không tồn đọng, không drop ở steady-state; drop-oldest (sau fix) đúng 100%.
4. Lớp nhận C++ ~47 GB/s — không bao giờ là nút thắt.
5. Ghi DB gọn (chỉ khi đổi nhãn) + cắt dung lượng đúng.
6. Vẽ I/Q sau tối ưu ~80 FPS.

**Cần xử lý / lưu ý:**
| Mức | Vấn đề | Đề xuất |
|---|---|---|
| 🔴 | Dropdown GUI có `xgb` nhưng **xgboost chưa cài** → chọn là worker chết | Cài `xgboost` hoặc bỏ `xgb` khỏi dropdown + báo lỗi rõ |
| 🟡 | DSP tăng khi máy tải nặng (147→177 ms) → biên co lại nếu chạy stride nhỏ + app khác | Ưu tiên svm; stride ≥ 15; giám sát khi chạy nặng |
| 🟡 | Reset chặn 7 s lúc khởi động; Stop trong lúc đó → hard-kill | (tùy chọn) rút ngắn / terminate ngay khi chưa nối socket |

**Hạn chế của đợt kiểm thử này:** không có radar + DCA1000 nên **không đo được**:
get_frames thật, loss% gói UDP, backlog kfifo khi stream, và jitter timing phần cứng.
Các số liệu độ trễ dựa trên mô phỏng cross-process đúng cấu trúc + dữ liệu raw thật;
phần cứng thật có thể thêm jitter ở tầng thu (đo bằng `bench_udp_fpga.py` full mode +
cờ `--profile` của worker khi chạy thật).
