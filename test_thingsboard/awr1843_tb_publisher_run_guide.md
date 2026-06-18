# Huong dan chay AWR1843 ThingsBoard publisher

File chinh:

```text
test_thingsboard/awr1843_tb_publisher.py
```

Script nay doc raw `.bin` tu DCA1000/AWR1843, xu ly Range FFT theo pipeline trong:

```text
../dsp_radar/test_ve_pho_raw_data.ipynb
```

Pipeline hien tai:

```text
raw int16
-> tach I/Q kieu Q-first: I = raw[2,3], Q = raw[0,1]
-> cube [antenna, chirp, sample]
-> IIR static clutter removal alpha=0.95
-> Hanning window
-> Range FFT
-> giu nua pho dau
-> mean qua antenna va chirp
-> publish range_profile len ThingsBoard MQTT topic
```

## 1. Can dien gi

Khuyen nghi khong sua token truc tiep trong code. Hay truyen bang command line hoac bien moi truong.

Thong tin bat buoc khi publish that:

```text
TB host: dia chi ThingsBoard MQTT broker
TB port: thuong la 1883
Device access token: token cua device trong ThingsBoard
Raw bin path: file .bin can replay
Radar parameters: script hien tai dang hard-code theo notebook
```

Mac dinh trong code:

```python
DEFAULT_BIN = Path(r"E:\DATN\30_5_ong\ongx7\30_5_ongx7_65cm_lan1_Raw_0.bin")
```

Thong so dang khop notebook `test_ve_pho_raw_data.ipynb`:

```text
num_tx = 1
num_rx = 4
num_adc_samples = 128
num_loops_per_frame = 128
fft_size mac dinh = 128
range bins publish mac dinh = 64
```

Neu file raw cua ban duoc capture bang config khac, can sua thong so hard-code trong script hoac mo rong parser sau.

## 2. Cai thu vien

Tu thu muc root repo:

```powershell
pip install numpy matplotlib paho-mqtt
```

Neu dang dung virtualenv cua project thi kich hoat virtualenv truoc.

## 3. Verify khong can ThingsBoard

Chay dry-run de kiem tra parser, FFT va xem waterfall offline realtime:

```powershell
python test_thingsboard/awr1843_tb_publisher.py --dry-run --max-frames 2 --no-repeat
```

Ket qua mong doi voi default hien tai:

```text
AWR1843 replay config: 128 chirps x 1 TX x 4 RX x 128 samples
frame=262144 bytes
Range FFT: fft_size=128, publish_bins=64
Replay: fps=50.000
Offline viewer: history=200 frames, showing local realtime waterfall
```

`dry-run` bay gio:

```text
van doc raw bin
van tinh FFT va range profile
khong ket noi MQTT
khong gui len ThingsBoard
mo 1 cua so matplotlib de ve line plot + waterfall cuon theo frame
```

Payload neu publish that se co dang:

```json
{
  "ts": 1781725177741,
  "values": {
    "frame_id": 0,
    "bin": [0, 1, 2, 3, 4, 5],
    "range_profile": [63.224, 70.22, 73.086, 74.551, 73.104, 69.884]
  }
}
```

## 4. Chay publish len ThingsBoard

Dung command line:

```powershell
python test_thingsboard/awr1843_tb_publisher.py --host 127.0.0.1 --port 1883 --access-token YOUR_DEVICE_ACCESS_TOKEN
```

Hoac dung bien moi truong:

```powershell
$env:TB_HOST="127.0.0.1"
$env:TB_PORT="1883"
$env:TB_ACCESS_TOKEN="YOUR_DEVICE_ACCESS_TOKEN"
python test_thingsboard/awr1843_tb_publisher.py
```

Neu ThingsBoard chay tren server khac:

```powershell
python test_thingsboard/awr1843_tb_publisher.py --host YOUR_TB_IP --access-token YOUR_DEVICE_ACCESS_TOKEN
```

## 5. Chay voi file raw khac

Vi du:

```powershell
python test_thingsboard/awr1843_tb_publisher.py `
  --bin "data_parse/raw_data_50fps.bin" `
  --host 127.0.0.1 `
  --access-token YOUR_DEVICE_ACCESS_TOKEN
```

Neu chi muon replay 100 frame roi dung:

```powershell
python test_thingsboard/awr1843_tb_publisher.py --max-frames 100 --no-repeat --host 127.0.0.1 --access-token YOUR_DEVICE_ACCESS_TOKEN
```

Neu muon doi toc do replay:

```powershell
python test_thingsboard/awr1843_tb_publisher.py --fps 20 --host 127.0.0.1 --access-token YOUR_DEVICE_ACCESS_TOKEN
```

## 6. Cac option quan trong

```text
--dry-run              Mo viewer offline realtime, khong ket noi MQTT.
--offline-history N    So frame giu trong waterfall viewer. Mac dinh = 200.
--max-frames N         Chi xu ly N frame.
--no-repeat            Doc het file thi dung.
--bin PATH             Duong dan file raw .bin.
--fft-size N           FFT size. Mac dinh = numAdcSamples.
--range-bins N         So range bin publish. Mac dinh = fft_size / 2.
--no-clutter-removal   Tat IIR static clutter removal.
--clutter-alpha X      Alpha cho IIR clutter removal. Mac dinh = 0.95.
```

## 7. Kiem tra loi thuong gap

Neu bao missing access token:

```text
Set --access-token or TB_ACCESS_TOKEN before publishing.
```

Hay dien token cua ThingsBoard device bang `--access-token` hoac `$env:TB_ACCESS_TOKEN`.

Neu so frame/shape sai, kha nang cao la raw `.bin` khong khop thong so hard-code trong script:

```text
so TX/RX/samples/loops trong script phai dung voi file raw
```

Neu MQTT khong ket noi duoc, kiem tra:

```text
ThingsBoard dang chay chua
host/port dung chua
device access token dung chua
firewall/network co chan port 1883 khong
```

## 8. Len dashboard ThingsBoard

Telemetry key quan trong:

```text
frame_id
bin
range_profile
```

Dashboard/custom widget nen giu mot rolling buffer 2D:

```text
new range_profile -> append vao buffer -> redraw heatmap/waterfall
```

Khong nen ve tung frame roi xoa, vi muc tieu la range-time map realtime.
