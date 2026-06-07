# Thread-get raw data flow (thread_get path)

Dưới đây là sơ đồ luồng nhận raw data bằng `thread_get` và cách dữ liệu được kéo ra, sắp xếp (sort) rồi đưa về Python.

```mermaid
flowchart TD
    subgraph PY [Python data receiver]
        direction TB
        PY2["call fastRead_in_Cpp_thread_get()"]
        PY2 --> PY3["Python wrapper "]
        PY3 --> PY4["udp_read_thread_get_frames()"]
    end

    subgraph UDP [Background UDP receive thread]
        direction TB
        UDP1["udp_read_thread_start(sock_fd)"] --> UDP2["_udp_read_thread()"]
        UDP2 --> UDP3["recvfrom() non-blocking loop"]
        UDP3 --> UDP4["build packet_t (seqNum + byteCnt + payload)"]
        UDP4 --> UDP5["Put to queue"]
    end

    PY4 --> Q["Get packet from queue"]
    UDP5 --> Q

    Q -->  D1{"Enough packets for target frame?"}
    D1 -- "no" --> D2["wait up to timeout -> return partial / timeout data"]
    D1 -- "yes" --> D3["packet records into temp buffer"]

    D3  --> S3["postProc_packet_sort()"]
    S3 --> S4["use seqNum from each packet to compute frame offsets"]
    S4 --> S5["copy payload bytes into frame queue"]
    
    %% Use Mermaid default styling (removed custom dark theme)
```

File này là bản đơn giản, source Mermaid để bạn dễ chỉnh trực tiếp.

File này chỉ chứa source Mermaid để dễ chỉnh sửa hoặc dán vào công cụ hiển thị Mermaid.
