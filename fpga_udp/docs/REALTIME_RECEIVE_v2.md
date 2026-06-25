# Luồng nhận dữ liệu realtime (fpga_udp v2.0)

Tài liệu mô tả **đầy đủ** đường đi của dữ liệu radar từ lúc gói UDP rời DCA1000
cho tới khi một frame ADC hoàn chỉnh được đưa vào model — đi từ tầng thấp nhất
(`fpga_udp` C++) lên tới pipeline đa tiến trình Python.

> Phạm vi: đường **thread mode** (`fastRead_in_Cpp_thread_*`) — đường được
> `realTimeProc.py` và `realTimeProc_infer.py` dùng. Các hàm cũ
> (`read_data_udp`, `read_data_udp_block_thread`, async…) **không đổi** so với
> v1.3 và không nằm trong tài liệu này.

---

## 1. Tổng quan các tầng

```
┌──────────────┐   UDP 1466B/gói   ┌───────────────────────────────────────────┐
│  DCA1000 FPGA │ ───────────────▶ │  fpga_udp (C++ extension)                   │
└──────────────┘   (Ethernet trực  │                                             │
                    tiếp tới NIC)   │  _udp_read_thread  ──▶ frame ring buffer ──┐│
                                    │  (producer, 1 thread)   + bitmap window    ││
                                    │                                            ││
                                    │  udp_read_thread_get_frames ◀──────────────┘│
                                    │  (consumer, gọi từ Python)                  │
                                    └───────────────────────────────────────────┘
                                                     │  np.int16[]
                                                     ▼
                              mmwave/dataloader/adc.py  (DCA1000 class)
                              fastRead_in_Cpp_thread_start / _get / _stop
                                                     │  (seq, ts, raw_adc)
                                                     ▼
                              realTimeProc(_infer).py  (3–4 tiến trình)
                              capture ─▶ preprocess ─▶ ai ─▶ web/MQTT/SQLite
```

Nguyên tắc thiết kế cốt lõi của v2.0:

1. **Thread nhận chỉ làm việc nhỏ và đều mỗi gói** (1 `memcpy` payload + set 1 bit
   bitmap). Không có thao tác nặng (memset/copy nguyên frame) trên đường nhận →
   không làm nghẽn `recvfrom` → không tràn đệm UDP của OS → **không rớt gói**.
2. **Ghép frame ngay trong thread nhận** theo **offset tuyệt đối** suy từ số thứ
   tự gói (`seqNum`) → mất gói không làm lệch frame.
3. **Cửa sổ bitmap** cho phép **chịu gói đến sai thứ tự** (reorder).
4. Phần copy nặng (lấy frame ra) đẩy sang **luồng gọi consumer**, không đụng thread nhận.

---

## 2. Tầng phần cứng & định dạng gói UDP

`DCA1000` mở 2 socket UDP ([adc.py](../../mmwave/dataloader/adc.py)):

| Socket | Cổng | Vai trò |
|---|---|---|
| `config_socket` | 4096 | gửi/nhận lệnh điều khiển FPGA (reset, start record, config…) |
| `data_socket`   | 4098 | nhận luồng dữ liệu ADC thô (bind `192.168.33.30`) |

Mỗi gói dữ liệu từ FPGA dài cố định **1466 byte** (`BYTES_OF_PACKET`):

```
 byte:  0      4            10                                   1466
        ┌──────┬────────────┬─────────────────────────────────────┐
        │seqNum│  byteCount  │            payload (1456 B)          │
        │ 4 B  │    6 B      │         = packetSize_d - 10          │
        └──────┴────────────┴─────────────────────────────────────┘
        little-endian uint32
```

- `seqNum`: số thứ tự gói, **bắt đầu từ 1**, tăng dần liên tục.
- `payloadSize = 1456` byte ADC mỗi gói.
- Nếu frame cuối thiếu byte, FPGA chèn 0 cho đủ gói.

Một **frame** có `BYTES_IN_FRAME` byte, tính từ file cfg:
```
BYTES_IN_FRAME = chirps × rx × tx × IQ × samples × 2(bytes)
```
Ví dụ cấu hình côn trùng (1TX×4RX×128 samples×128 chirps×2 IQ×2 byte) = **262144 byte/frame**
→ ~`262144 / 1456 ≈ 180.04` gói/frame (frame **không** chia hết cho gói → gói luôn
vắt qua ranh giới frame; logic phải xử lý điều này).

Python **bind** socket nhưng truyền `data_socket.fileno()` xuống C++ để C++ tự
`recvfrom` — toàn bộ hiệu năng nhận nằm ở C++.

---

## 3. fpga_udp C++ — cấu trúc dữ liệu

File: [fpga_udp/src/main.cpp](../src/main.cpp).

```c
#define REORDER_WIN 2          // số frame trong cửa sổ ghép (chịu reorder)

uint8_t  *frameRing_g;         // ring N ô, mỗi ô = BYTES_IN_FRAME (dữ liệu frame)
uint32_t *frameLost_g;         // số gói mất của từng frame đã publish (cho thống kê)
uint32_t  frameSlots_g;        // N = số ô frame trong ring
int       bytesInFrame_g;      // byte/frame

std::atomic<uint64_t> frameIn_g;   // tổng frame producer đã publish
std::atomic<uint64_t> frameOut_g;  // tổng frame consumer đã lấy
std::mutex frameOut_mutex;         // bảo vệ cập nhật frameOut (override + consumer)

uint64_t  alignBase_g;         // offset byte tuyệt đối của ranh giới frame đầu tiên

// Trạng thái cửa sổ trượt (chỉ producer dùng):
uint8_t  *bm_g;                // REORDER_WIN bitmap "gói đã tới"
uint32_t  bmBytes_g;           // số byte mỗi bitmap
uint32_t  recvCnt_g[REORDER_WIN];  // số gói đã nhận của từng frame trong cửa sổ
```

Hàm tiện ích (deterministic, suy từ `alignBase_g` và `bytesInFrame_g`):

| Hàm | Ý nghĩa |
|---|---|
| `slotPtr(f)` | con trỏ tới ô dữ liệu của frame chỉ số `f` (`frameRing_g + (f % N)*frame`) |
| `bmPtr(f)` | con trỏ bitmap của frame `f` trong cửa sổ (`f % REORDER_WIN`) |
| `frameFirstSeq(f)` | seqNum của gói **đầu tiên** đóng góp vào frame `f` |
| `frameExpCount(f)` | số gói kỳ vọng cho frame `f` = `lastSeq − firstSeq + 1` |
| `openFrame(f)` | xoá bitmap + reset `recvCnt` khi một frame mới vào cửa sổ |

**Ring buffer = một queue producer–consumer**: `frameIn_g − frameOut_g` là độ sâu
hàng đợi; đầy thì override frame cũ nhất. Khác queue cũ (v1.3) ở chỗ đơn vị là
**frame** và dữ liệu được ghi **in-place** (không copy khi “put”).

---

## 4. Producer — `_udp_read_thread` (1 thread, chạy liên tục)

Đặt socket sang **non-blocking** rồi vòng lặp `recvfrom` từng gói. Với mỗi gói:

```
seqNum  = gói.seqNum                          (1-based)
absByte = (seqNum - 1) * payloadSize          // vị trí byte tuyệt đối của payload
endByte = absByte + payloadSize
```

### 4.1. Căn ranh giới frame — một lần duy nhất

Gói đầu phiên (`!asmInited_g`):
```
alignBase_g = ceil(absByte / frame) * frame   // làm tròn LÊN ranh giới frame
openFrame(0) … openFrame(REORDER_WIN-1)        // mở cửa sổ ban đầu
```
→ bỏ tối đa **một** frame dở ở đầu, từ đó mọi frame căn đúng `bytesInFrame`, **không
resync mỗi lần** như v1.3.

### 4.2. Bỏ phần thuộc frame đã publish

```
pubBoundary = alignBase_g + frameIn_g * frame
s = max(absByte, pubBoundary)
if s >= endByte:  bỏ gói   // gói cũ/trùng cho frame đã phát đi rồi
```

### 4.3. Xác định frame mà gói chạm tới

```
fStart = (s - alignBase_g) / frame
fEnd   = (endByte - 1 - alignBase_g) / frame   // = fStart, hoặc fStart+1 nếu vắt biên
```

### 4.4. Dời cửa sổ nếu gói vượt ra ngoài (mất gói lớn / nhảy xa)

```
while fEnd > frameIn_g + (REORDER_WIN - 1):
    flushOldest()      // publish frame cũ nhất (gói thiếu coi như mất → zero-fill)
```

### 4.5. Ghi payload **in-place** + đánh dấu bitmap

Với mỗi frame `f` trong `[fStart, fEnd]` (1 hoặc 2 frame):
```
fb = alignBase_g + f*frame
lo = max(s, fb);  hi = min(endByte, fb + frame)
if lo < hi:
    memcpy(slotPtr(f) + (lo-fb), payload + (lo-absByte), hi-lo)   // ghi đúng offset

bit = seqNum - frameFirstSeq(f)
if bit < frameExpCount(f) and bit chưa set:
    set bit trong bmPtr(f);  recvCnt_g[f % REORDER_WIN]++
```
**Không có thao tác zero nào ở đây** → gói đến sai thứ tự vẫn rơi đúng chỗ, không
xoá nhầm dữ liệu đã có.

### 4.6. Publish ngay các frame đã đủ (đường zero-latency)

```
while recvCnt_g[frameIn_g % REORDER_WIN] >= frameExpCount(frameIn_g):
    flushOldest()
```
Khi không mất gói: frame publish **ngay khi gói cuối của nó tới**, không thêm trễ,
không memset.

> Chi phí mỗi gói: 1 `memcpy` (~1456 B) + set 1 bit + vài phép chia. Tương đương
> v1.3, nhỏ và đều → đây là lý do hết rớt gói ~1.7%.

---

## 5. `flushOldest` — chốt frame cũ nhất ra consumer

```
f   = frameIn_g
exp = frameExpCount(f);  firstSeq = frameFirstSeq(f)

// Chỉ zero đúng vùng các gói KHÔNG tới (đọc bitmap). Không mất gói ⇒ vòng này rỗng.
for i in [0, exp):
    if bit i chưa set:
        zero vùng byte của gói (firstSeq+i) nằm trong frame f

frameLost_g[f % frameSlots_g] = exp - recvCnt   // lưu số gói mất của frame này

// override: nếu ring sắp tràn thì bỏ frame cũ nhất chưa tiêu thụ
lock(frameOut_mutex):
    if (f+1 - frameOut_g) > frameSlots_g - REORDER_WIN:
        frameOut_g = f+1 - (frameSlots_g - REORDER_WIN)

frameIn_g = f + 1        // publish (release)
framePutCnt_g++
openFrame(f + REORDER_WIN)   // một frame mới vào đỉnh cửa sổ
```

Điểm mấu chốt: **không mất gói → không có memset nào** (vòng zero rỗng). Chỉ khi có
mất gói mới zero đúng các lỗ nhỏ (mỗi lỗ ~1456 B). → không bao giờ có “burst”.

---

## 6. Consumer — `udp_read_thread_get_frames(frameNum, …)`

Gọi từ Python (luồng gọi, **không phải** thread nhận):

```
1) Poll 1ms tới khi (frameIn_g - frameOut_g) >= frameNum  hoặc timeout.
2) lock(frameOut_mutex):
       out  = frameOut_g
       take = min(frameNum, frameIn_g - out)
       frameOut_g = out + take          // RESERVE (giành chỗ) — producer không override mất
3) Tính thống kê cho ĐÚNG lô [out, out+take):
       firstByte = alignBase_g + out*frame
       lastByte  = alignBase_g + (out+take)*frame - 1
       firstPacketNum_g = firstByte/payload + 1
       lastPacketNum_g  = lastByte /payload + 1
       expectedPacketNum_g = last - first + 1
       receivedPacketNum_g = expected - Σ frameLost_g[out..out+take)
4) Copy take frame ra mảng numpy (NGOÀI lock) → trả về.
```

Vì đã reserve dưới lock rồi copy ngoài lock, thread nhận **không bị chặn** bởi thao
tác copy nặng. Ring lớn (mặc định 512 frame) nên producer không thể “vòng” đè lên
vùng đang copy.

Trả về mảng `uint8` dài `take*frame` byte (timeout → trả ít frame hơn). Python bọc
thành `np.int16` (zero-copy).

---

## 7. Lớp Python `DCA1000` (adc.py)

[mmwave/dataloader/adc.py](../../mmwave/dataloader/adc.py):

| Hàm Python | Gọi xuống C++ |
|---|---|
| `fastRead_in_Cpp_thread_start(frameNumInBuf)` | `udp_read_thread_init(BYTES_IN_FRAME, frameNumInBuf)` + `udp_read_thread_start(fileno)` |
| `fastRead_in_Cpp_thread_get(numframes, …)` | `udp_read_thread_get_frames(numframes, BYTES_IN_FRAME, timeOut, sortInC)` → bọc `np.int16` |
| `fastRead_in_Cpp_thread_stop()` | `udp_read_thread_stop()` |

- `BYTES_IN_FRAME` lấy từ `ADC_PARAMS` (đọc cfg trong `configure()` → `refresh_parameter()`).
- `init` giờ trả **số byte** dung lượng ring (không phải số gói như v1.3).
- Tham số `sortInC` còn để tương thích nhưng **bị bỏ qua** (frame đã ghép sẵn).
- `verbose=True` in `firstPacketNum/lastPacketNum` và `loss%` lấy từ các getter — nay
  phản ánh **đúng lô vừa lấy** (xem §9).

Ràng buộc: `frameNumInBuf >= numframes` (buffer phải đủ chứa một lần đọc).

---

## 8. Pipeline realtime (realTimeProc_infer.py)

[realTimeProc_infer.py](../../realTimeProc_infer.py) chạy 3–4 tiến trình nối bằng
`DropOldestQueue` (đầy thì bỏ lô cũ, ưu tiên lô mới — hợp với tinh thần override của ring):

```
capture_worker ─▶ preprocess_queue ─▶ preprocessing_worker ─▶ ai_queue ─▶ ai_worker
   (DCA1000)          (DSP / InsectRadarProcessor)               (model + Web/MQTT/SQLite)
```

1. **capture_worker**: cấu hình radar+DCA, `fastRead_in_Cpp_thread_start`, rồi vòng
   lặp `fastRead_in_Cpp_thread_get(numframes=30)` → đẩy `(seq, time.time(), raw_adc)`
   vào `preprocess_queue`.
2. **preprocessing_worker**: kiểm tra kích thước `raw_adc` (= `numframes × 131072`
   int16), chạy `InsectRadarProcessor.process_array` (Range-FFT, STFT, đặc trưng),
   tạo `range_plot` cho web, đẩy sang `ai_queue`.
3. **ai_worker**: load model (svm/rf/xgb), suy luận, gửi JSON Lines tới FastAPI,
   publish ThingsBoard MQTT, ghi SQLite khi nhãn đổi.

`realTimeProc.py` (bản đơn giản, không pipeline) thì gọi
`fastRead_in_Cpp_thread_get(numframes=1, verbose=True)` trong vòng lặp — đây là file
dùng để quan sát `firstPacketNum`/`loss`.

> Tương thích: chữ ký API thread không đổi so với v1.3, mảng trả về cùng kiểu/kích
> thước → **không phải sửa code** ở các `realTimeProc_*.py`.

---

## 9. Thống kê & tính tỉ lệ mất gói

Bốn getter (`get_firstPacketNum`, `get_lastPacketNum`, `get_receivedPacketNum`,
`get_expectedPacketNum`) cùng `get_framePutCnt` được cập nhật **trong `get_frames`,
cho đúng lô frame vừa trả** (không phải giá trị tích luỹ/đóng băng như bản trung gian):

```
loss% = (expected - received) / expected * 100
```
- `firstPacketNum` **biến thiên** theo từng lần gọi (vì mỗi lô bắt đầu ở frame khác).
- `received/expected` đếm theo gói **thuộc đúng các frame của lô** (lấy từ
  `frameLost_g` đã lưu khi publish).

So với hai lỗi từng gặp ở bản trung gian:
- ❌ `firstPacketNum` luôn = 1  → ✅ nay thay đổi theo lô.
- ❌ loss luôn ~1.7% (do burst memset+Put ở thread nhận)  → ✅ thread nhận không còn
  burst nên không rớt gói; loss báo đúng frame thật sự mất.

---

## 10. Mất gói & reorder — đặc tính đảm bảo

| Tình huống | Hành vi |
|---|---|
| **Mất vài gói trong 1 frame** | Lỗ zero đúng offset; frame vẫn đúng kích thước; **frame sau không lệch** |
| **Mất nguyên nhiều frame** | Nhánh 4.4 publish các frame trống (zero), giữ đúng số frame/định thời, tự bắt kịp |
| **Gói đến sai thứ tự (reorder)** trong `< REORDER_WIN` frame | Ghép lại **chính xác** (ghi theo offset tuyệt đối, publish khi bitmap đủ) |
| **Reorder vượt `REORDER_WIN` frame** | Gói tới quá muộn (frame đã publish) → tính là mất; cực hiếm trên link trực tiếp |

Vì sao không lệch khi mất gói: mọi byte ghi theo **offset tuyệt đối** từ `seqNum`,
độc lập với gói trước → một gói mất chỉ để lại lỗ zero, gói sau vẫn đúng vị trí.

---

## 11. Tham số & giới hạn

| Tham số | Mặc định | Ý nghĩa |
|---|---|---|
| `REORDER_WIN` (C++) | 2 | số frame trong cửa sổ ghép; lớn hơn = chịu reorder xa hơn nhưng tăng trễ cho frame mất gói |
| `frameNumInBuf` (Python) | 256–512 | số ô frame của ring; lớn hơn = chịu consumer chậm lâu hơn trước khi override |
| `numframes` | 1 (realTimeProc) / 30 (infer) | số frame lấy mỗi lần `get` |
| `timeOut` | 2 s | thời gian chờ tối đa trong `get_frames` |

Trễ: frame **đủ gói** publish ngay (0 trễ thêm). Frame **mất gói** bị giữ tối đa tới
khi tràn cửa sổ (~`REORDER_WIN` frame) rồi mới flush — ở 100fps là ~20ms, chỉ áp
dụng cho frame lỗi.

Bộ nhớ thêm của bitmap: `REORDER_WIN × ceil((gói/frame + 2)/8)` byte (≈ vài chục byte)
+ mảng `frameLost_g` (`frameSlots × 4` byte).

---

## 12. So sánh nhanh với v1.3

| | v1.3.0 | v2.0 |
|---|---|---|
| Đơn vị queue | gói (`UnlockQueue<packet_t>`) | frame (ring in-place) |
| Căn frame / sort | ở luồng `get`, mỗi lần (discard + sort) | ở thread nhận, ghép sẵn |
| Việc/gói ở thread nhận | `Put(1466B)` | `memcpy` payload + set 1 bit |
| Discard ở biên mỗi `get` | có | không (căn 1 lần) |
| Mất “đầu frame sau” ở gói vắt biên | có thể | không |
| Chống mất gói | sort theo seq trong cửa sổ đọc | offset tuyệt đối + zero-fill |
| Chống reorder | có (sort cả buffer) | có (bitmap cửa sổ trượt) |
| Stat | tính lại mỗi `get` | tính cho đúng lô `get` |

---

## 13. Kiểm thử

- `fpga_udp/tests/test.py` — version + add/subtract (CI tác giả).
- Test loopback (gửi UDP đúng định dạng DCA1000 vào socket, so byte): không mất gói
  (byte-exact), mất gói (lỗ zero đúng + alignment giữ nguyên + loss báo đúng lô),
  frame thật 262144B, và **reorder** (xáo thứ tự gửi) → vẫn byte-exact, 0% loss.

> Lưu ý: loopback phần mềm **không tái hiện** rớt gói do tràn đệm socket; việc loss
> thực giảm về ~0 cần xác nhận trên **phần cứng thật** với `realTimeProc.py`.
