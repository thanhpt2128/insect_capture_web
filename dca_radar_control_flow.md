# DCA & Radar Service Flow (service-only)

Sơ đồ này mô tả luồng điều khiển khi chỉ chạy phần service (`realTimeProc`): cấu hình, khởi động, đọc dữ liệu và dọn dẹp cho `DCA1000` và radar.

```mermaid
flowchart TD
  Start["Controller start"] --> Load["Load DCA config"]
  Load  --> DCA_Start["dca.stream_start()"]
  DCA_Start --> DCA_Thread["start DCA read thread"]

  Start --> SerialOpen["Open radar serial COM"]
  SerialOpen --> SendProfile["Send profile / cfg via CLI to radar"]
  SendProfile --> Radar_Start["radar.startSensor()"]

  DCA_Thread --> ReadLoop["Service loop: fastRead_in_Cpp_thread_get() -> process frames -> local results"]
  Radar_Start --> ReadLoop

  StopSig["Stop signal"] --> Service_Stop["Service initiates stop/cleanup"]
  Service_Stop --> DCA_Stop["dca.fastRead_in_Cpp_thread_stop()"]
  DCA_Stop --> DCA_StreamStop["dca.stream_stop()"]
  DCA_StreamStop --> DCA_Close["dca.close()"]

  Service_Stop --> Radar_Stop["radar.stopSensor()"]
  Radar_Stop --> SerialClose["close serial port"]

  Exception["on exception"] --> Service_Stop

  DCA_Close --> End["Service stopped / cleaned up"]
  SerialClose --> End
```

Phiên bản này tối giản, rõ ràng và kiểm tra hợp lệ với Mermaid.
