Đặc tả Kỹ thuật dạng Markdown dành cho AI Coding Agent
AI Coding Agent có thể đọc hiểu và thực thi trực tiếp đặc tả dưới đây để sinh mã nguồn tối ưu cho tệp realTimeProc_fastapi_decoupled.py.

SYSTEM DESIGN SPECIFICATION: HIGH-THROUGHPUT DECOUPLED RADAR PIPELINE (SIMPLIFIED CONTROL)
Target Objective
Refactor the monolithic real-time hardware data processing script realTimeProc_fastapi.py into a highly decoupled, three-process architecture using Python's native multiprocessing package. The target script must connect to a Texas Instruments mmWave radar and a DCA1000 EVM board, execute signal preprocessing, run AI inference, and stream newline-delimited JSON results back to a FastAPI backend over a localhost TCP socket.

Key Architecture Rules
No Manual Ring Buffer: Remove any complex custom ring buffer implementations in Python. Rely strictly on clean, process-safe bounded queues.

Decoupled Responsibilities (Three Processes, Not Threads):

Process 1 (Capture Process): High-speed UDP packet ingestion from DCA1000 using the optimized C++ backend thread and serial commands.

Process 2 (Preprocessing Process): Signal filtering and statistical/DSP calculation (currently hosting the numpy statistical calculations).

Process 3 (AI Inference Process): AI model execution and outbound TCP socket client streaming JSON results.

Simplified Shutdown Control (No TCP Receiver Thread):

Since FastAPI spawns this script as a subprocess, the pipeline must cleanly shut down using OS Signal Handling (SIGINT or SIGTERM) registered in the main process.

Additionally, Socket Disconnection Detection must be implemented in the AI Worker. If a TCP send fails (FastAPI closed connection), it must trigger exit_event.set() to automatically shut down all other processes cleanly.

Drop-Oldest Queue Mechanism: Implement custom bounded queue wrappers with non-blocking write capabilities to automatically drop outdated frames when the queue is full, preventing memory leaks and latency accumulation.

Process Data Flow & Protocols
Preprocess Queue Protocol (Process 1 -> Process 2)
An exchange queue containing captured raw numpy arrays and metadata:

Python
# Item payload format: (sequence_number, capture_timestamp, raw_adc_data_array)
(
    seq: int, 
    ts: float, 
    raw_data: np.ndarray # Shape & dtype resolved dynamically from DCA1000
)
AI Queue Protocol (Process 2 -> Process 3)
An exchange queue carrying extracted features and statistical representations:

Python
# Item payload format: (sequence_number, capture_timestamp, preprocessed_features_dict)
(
    seq: int, 
    ts: float, 
    features: dict # Statistical summary and extracted feature vectors
)
TCP Socket Streaming Output Protocol (Process 3 -> FastAPI)
Outbound JSON Lines format (UTF-8 encoded, ending with a \n character). The JSON schema must remain strictly compatible with the original FastAPI backend expectations:

JSON
{
    "type": "inference",
    "mode": "hardware",
    "seq": 45,
    "ts": 1717315200.12,
    "com_port": "COM5",
    "cfg_path": "configFiles/cfg128_128_100fps.cfg",
    "capture": {
        "numframes": 2,
        "elapsed_s": 0.5,
        "size": 16384,
        "dtype": "int16",
        "sample_n": 8192,
        "sample_mean": 124.5,
        "sample_std": 30.2,
        "sample_min": -512,
        "sample_max": 512
    },
    "result": {
        "label": "gesture-classification-placeholder",
        "score": null
    },
    "preview": [12, -45, 120, -5],
    "note": "Decoupled multi-process architecture active."
}
Production Blueprint (File: realTimeProc_fastapi_decoupled.py)
The AI Agent must generate the decoupled pipeline script adhering strictly to this robust structure. Note that libraries such as numpy, DCA1000, and TI must be imported inside the worker processes to prevent serialization issues during process creation on Windows (using the spawn method).

Python
import argparse
import json
import socket
import sys
import time
import signal
import multiprocessing
from pathlib import Path
from queue import Empty, Full
import traceback

# Global pointers to prevent initialization-level import crashes
np = None
DCA1000 = None
TI = None

def import_dependencies():
    """Import scientific and hardware packages within the process context to avoid serialization lockups."""
    global np, DCA1000, TI
    import numpy as as_np
    np = as_np
    try:
        from mmwave.dataloader import DCA1000 as DCA_Class
        from mmwave.dataloader.radars import TI as TI_Class
        DCA1000 = DCA_Class
        TI = TI_Class
    except ImportError as e:
        print(f"[-] Hardware library import error: {e}. Check mmwave package installations.", flush=True)
        raise e

def _to_builtin(obj):
    """Recursively convert numpy types to native Python equivalents for JSON compatibility."""
    if obj is None:
        return None
    if isinstance(obj, (int, float, str, bool)):
        return obj
    if hasattr(obj, "item"):
        try:
            return obj.item()
        except Exception:
            pass
    if isinstance(obj, dict):
        return {k: _to_builtin(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_builtin(x) for x in obj]
    return str(obj)

def _send_json_line(sock, data):
    """Serialize and transmit newline-delimited JSON over the socket. Raise error on failure."""
    clean_data = _to_builtin(data)
    line = json.dumps(clean_data) + "\n"
    sock.sendall(line.encode('utf-8'))


class DropOldestQueue:
    """
    Process-safe Bounded Queue wrapper that drops the oldest element 
    when full to maintain minimal latency in real-time execution.
    """
    def __init__(self, maxsize: int):
        self.manager = multiprocessing.Manager()
        self.queue = self.manager.Queue(maxsize=maxsize)
        self.lock = multiprocessing.Lock()

    def put(self, item):
        with self.lock:
            try:
                self.queue.put(item, block=False)
            except Full:
                try:
                    # Drop the oldest item at the head of the queue
                    self.queue.get_nowait()
                except Exception:
                    pass
                # Insert the newest item at the tail
                try:
                    self.queue.put(item, block=False)
                except Full:
                    pass  # Fail-safe in case of race conditions

    def get(self, block=True, timeout=None):
        return self.queue.get(block=block, timeout=timeout)


# ==========================================
# 1. PROCESS: RADAR UDP HARDWARE CAPTURE
# ==========================================
def capture_worker_process(exit_event, preprocess_queue, args_dict):
    """Runs the hardware interface to fetch raw UDP packet streams."""
    import_dependencies()
    print("[+] Capture Process initiated.", flush=True)
    
    dca = None
    radar = None
    
    try:
        # Resolve config files
        cfg_path = Path(args_dict['cfg_path'])
        if not cfg_path.is_absolute():
            cfg_path = Path(__file__).resolve().parent.parent / "configFiles" / cfg_path
            if not cfg_path.exists():
                cfg_path = Path(__file__).resolve().parent / "configFiles" / args_dict['cfg_path']
        
        dca_json_path = Path(args_dict['dca_cfg'])
        if not dca_json_path.is_absolute():
            dca_json_path = Path(__file__).resolve().parent / args_dict['dca_cfg']
            
        print(f"[+] Resolving Configs: Radar={cfg_path}, DCA={dca_json_path}", flush=True)

        # Initialize DCA1000 Capture Card
        try:
            dca = DCA1000()
            dca.reset_radar()
            dca.reset_fpga()
            time.sleep(5.0)  # Hardware settling delay
        except Exception as err:
            print(f"[-] Failed to reset DCA1000: {err}", flush=True)
            raise err

        # Initialize TI Command Serial Interface
        radar = TI(
            cli_loc=args_dict['com_port'],
            cli_baud=args_dict['cli_baud'],
            connected=True
        )
        radar.setFrameCfg(0)  # Continuous frame looping mode

        # Configure DCA1000 Network
        adc_params, cfg_params = dca.configure(str(dca_json_path), str(cfg_path))
        print(f"[+] DCA Hardware configured. Params: {adc_params}", flush=True)

        # Fire up hardware stream
        dca.stream_start()
        max_buffer_size = max(int(args_dict['frame_num_in_buf']), int(args_dict['numframes']))
        dca.fastRead_in_Cpp_thread_start(max_buffer_size)
        radar.startSensor()
        print("[+] Hardware started. Capturing UDP data...", flush=True)

        seq = 0
        while not exit_event.is_set():
            # Extract raw frame buffer using pybind C++ thread
            raw_adc_data = dca.fastRead_in_Cpp_thread_get(
                numframes=int(args_dict['numframes']),
                timeOut=2,
                verbose=False,
                sortInC=True
            )

            if raw_adc_data is not None and len(raw_adc_data) > 0:
                capture_time = time.time()
                # Non-blocking write to preprocessing queue
                preprocess_queue.put((seq, capture_time, raw_adc_data))
                seq += 1
            else:
                time.sleep(0.001)  # Yield CPU core to prevent thrashing

    except Exception as e:
        print(f"[-] Exception occurred in Capture Process: {e}", flush=True)
        traceback.print_exc()
        exit_event.set()
    finally:
        print("[+] Halting hardware systems and releasing ports...", flush=True)
        if radar:
            try:
                radar.stopSensor()
                radar.close()
            except Exception:
                pass
        if dca:
            try:
                dca.fastRead_in_Cpp_thread_stop()
                dca.stream_stop()
                dca.close()
            except Exception:
                pass
        print("[+] Capture Process terminated cleanly.", flush=True)


# ==========================================
# 2. PROCESS: PREPROCESSING WORKER (DSP)
# ==========================================
def preprocessing_worker_process(exit_event, preprocess_queue, ai_queue, args_dict):
    """Consumes raw ADC data arrays and extracts structural DSP/Statistical features."""
    import_dependencies()
    print("[+] Preprocessing Process initiated.", flush=True)

    while not exit_event.is_set():
        try:
            seq, ts, raw_data = preprocess_queue.get(block=True, timeout=1.0)
        except Empty:
            continue

        try:
            # -------------------------------------------------------------
            # NOTE FOR DEVELOPER / AI AGENT:
            # Integrate actual FMCW DSP algorithms here (e.g., Range FFT,
            # Clutter Removal, Doppler processing, noise filtering).
            # The placeholder code below preserves the original numpy statistical calculations.
            # -------------------------------------------------------------
            total_elements = raw_data.size
            preview_slice_size = min(64, total_elements)
            stat_slice_size = min(8192, total_elements)

            preview_slice = raw_data[:preview_slice_size].tolist()
            stats_subset = raw_data[:stat_slice_size]

            sample_mean = np.mean(stats_subset)
            sample_std = np.std(stats_subset)
            sample_min = np.min(stats_subset)
            sample_max = np.max(stats_subset)

            features = {
                "numframes": int(args_dict['numframes']),
                "size": int(total_elements),
                "dtype": str(raw_data.dtype),
                "sample_n": int(stat_slice_size),
                "sample_mean": float(sample_mean),
                "sample_std": float(sample_std),
                "sample_min": int(sample_min),
                "sample_max": int(sample_max),
                "preview": preview_slice
            }
            # -------------------------------------------------------------

            # Write the statistical feature metrics to the AI Queue
            ai_queue.put((seq, ts, features))

        except Exception as e:
            print(f"[-] Preprocessing failure at sequence {seq}: {e}", flush=True)


# ==========================================
# 3. PROCESS: AI WORKER & COMMUNICATIONS
# ==========================================
def ai_worker_process(exit_event, ai_queue, args_dict):
    """Runs deep neural network inference and operates network socket communications."""
    import_dependencies()
    print("[+] AI Worker Process initiated.", flush=True)

    # Establish TCP client connection to the FastAPI socket server
    sock = None
    try:
        sock = socket.create_connection(
            (args_dict['server_host'], int(args_dict['server_port'])),
            timeout=10
        )
        print(f"[+] TCP Connection verified with {args_dict['server_host']}:{args_dict['server_port']}", flush=True)
        _send_json_line(sock, {"type": "status", "event": "ready"})
    except Exception as e:
        print(f"[-] Connection to FastAPI server failed: {e}", flush=True)
        exit_event.set()
        return

    try:
        while not exit_event.is_set():
            try:
                seq, ts, features = ai_queue.get(block=True, timeout=1.0)
            except Empty:
                continue

            # -------------------------------------------------------------
            # NOTE FOR DEVELOPER / AI AGENT:
            # Place deep learning AI model inference routines here (e.g. PyTorch, ONNX, TFLite).
            # Convert raw statistics or feature tensors into classified gestures/labels.
            # -------------------------------------------------------------
            ai_inference_result = {
                "label": "hardware-capture", # REPLACE with the actual dynamic model inference result
                "score": None                # REPLACE with confidence score float (e.g., 0.95)
            }
            # -------------------------------------------------------------

            # Package finalized JSON payload matching original FastAPI schemas
            payload = {
                "type": "inference",
                "mode": "hardware",
                "seq": seq,
                "ts": ts,
                "com_port": args_dict['com_port'],
                "cfg_path": args_dict['cfg_path'],
                "capture": {
                    "numframes": features["numframes"],
                    "elapsed_s": float(args_dict['interval']),
                    "size": features["size"],
                    "dtype": features["dtype"],
                    "sample_n": features["sample_n"],
                    "sample_mean": features["sample_mean"],
                    "sample_std": features["sample_std"],
                    "sample_min": features["sample_min"],
                    "sample_max": features["sample_max"]
                },
                "result": ai_inference_result,
                "preview": features["preview"],
                "note": "Decoupled pipeline active; AI inference placeholder running."
            }

            # Ship out newline-delimited JSON. If send fails, shutdown whole pipeline.
            try:
                _send_json_line(sock, payload)
            except Exception as e:
                print(f"[!] Send failed (FastAPI disconnected): {e}. Initiating teardown...", flush=True)
                exit_event.set()
                break

            # Control pacing to prevent flooding FastAPI UI
            if float(args_dict['interval']) > 0:
                time.sleep(float(args_dict['interval']))

    except Exception as e:
        print(f"[-] AI Worker Process execution error: {e}", flush=True)
    finally:
        if sock:
            try:
                _send_json_line(sock, {"type": "status", "event": "stopped"})
                sock.shutdown(socket.SHUT_RDWR)
                sock.close()
            except Exception:
                pass
        print("[+] AI Worker Process terminated.", flush=True)


# ==========================================
# PIPELINE SYSTEM ENTRY POINT & SIGNAL HANDLER
# ==========================================
def main():
    parser = argparse.ArgumentParser(description="High-Throughput Decoupled Multi-Process Radar Pipeline.")
    parser.add_argument("--server-host", type=str, default="127.0.0.1")
    parser.add_argument("--server-port", type=int, required=True)
    parser.add_argument("--com-port", type=str, required=True)
    parser.add_argument("--cli-baud", type=int, default=921600)
    parser.add_argument("--cfg-path", type=str, required=True)
    parser.add_argument("--dca-cfg", type=str, default="cf.json")
    parser.add_argument("--numframes", type=int, default=2)
    parser.add_argument("--frame-num-in-buf", type=int, default=128)
    parser.add_argument("--interval", type=float, default=0.5)

    args = parser.parse_args()
    args_dict = vars(args)

    # Windows process initialization compatibility
    multiprocessing.freeze_support()

    # Create global multiprocessing exit flag and non-blocking queues
    exit_event = multiprocessing.Event()
    preprocess_queue = DropOldestQueue(maxsize=10)
    ai_queue = DropOldestQueue(maxsize=10)

    # Register OS Signal Handler in the main process to capture exit commands from FastAPI (e.g. subprocess kill)
    def signal_handler(signum, frame):
        print(f"[!] System Signal {signum} caught in main process. Stopping all subprocesses cleanly...", flush=True)
        exit_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Instantiate processes (Processes, NOT threads, to completely bypass GIL and maximize multi-core execution)
    capture_proc = multiprocessing.Process(
        target=capture_worker_process,
        args=(exit_event, preprocess_queue, args_dict),
        name="RadarCaptureProcess"
    )
    
    preprocess_proc = multiprocessing.Process(
        target=preprocessing_worker_process,
        args=(exit_event, preprocess_queue, ai_queue, args_dict),
        name="DSPPreprocessingProcess"
    )
    
    ai_proc = multiprocessing.Process(
        target=ai_worker_process,
        args=(exit_event, ai_queue, args_dict),
        name="InferenceProcess"
    )

    print("[*] Decoupled multi-process radar pipeline initializing...", flush=True)
    capture_proc.start()
    preprocess_proc.start()
    ai_proc.start()

    # Wait until exit_event is set (either by OS signals, socket drops, or manual Ctrl+C)
    while not exit_event.is_set():
        try:
            time.sleep(0.1)
        except IOError:
            # Handle sleep interruptions cleanly
            pass

    print("[*] Exit flag set. Synchronizing teardown across all processes...", flush=True)
    
    # Wait for processes to exit cleanly
    capture_proc.join(timeout=3)
    preprocess_proc.join(timeout=3)
    ai_proc.join(timeout=3)

    # Force terminate if any process is deadlocked
    for proc in [capture_proc, preprocess_proc, ai_proc]:
        if proc.is_alive():
            print(f"[!] Warning: Process {proc.name} failed to terminate cleanly. Terminating immediately...", flush=True)
            proc.terminate()
            proc.join()

    print("[*] Pipeline system fully stopped.", flush=True)


if __name__ == "__main__":
    main()
Kết luận và Khuyến nghị Hệ thống
Kiến trúc phân rã đề xuất giải quyết triệt để vấn đề mất mát gói tin UDP bằng việc cô lập hoàn toàn tác vụ điều khiển phần cứng ra khỏi luồng xử lý dữ liệu và AI. Việc loại bỏ bộ đệm vòng thủ công ở phía Python giúp tiết kiệm bộ nhớ RAM, trong khi cơ chế Hàng đợi Giới hạn tự động loại bỏ phần tử cũ (Drop-Oldest) đảm bảo hệ thống luôn hoạt động với độ trễ thấp nhất.   

Khi tiến hành triển khai thực tế và cấu hình AI Agent lập trình hệ thống này, các khuyến nghị kỹ thuật sau cần được tuân thủ nghiêm ngặt:

Khởi tạo Tiến trình con: Luôn gọi multiprocessing.freeze_support() ngay tại điểm khởi chạy hệ thống để đảm bảo tính tương thích tuyệt đối khi chạy trên hệ điều hành Windows, tránh hiện tượng sinh tiến trình vô hạn.   

Quản lý Import trong Tiến trình con: Các hàm liên quan đến driver phần cứng (DCA1000 và TI) bắt buộc phải được đặt trong hàm import_dependencies() và gọi ngay khi tiến trình con khởi động thay vì import ở phạm vi toàn cục. Điều này ngăn chặn việc Python cố gắng tuần tự hóa (pickle) các đối tượng liên kết C++ hoặc handle hệ thống không thể serialize.   

Giải phóng Tài nguyên khi Có Lỗi: Toàn bộ quá trình khởi tạo, đọc ghi của trình điều khiển phần cứng phải được đặt trong khối lệnh try...finally. Điều này đảm bảo khi hệ thống gặp lỗi đột xuất hoặc nhận tín hiệu dừng, các cổng nối tiếp (COM Port) và socket mạng UDP của card DCA1000 sẽ được giải phóng lập tức, ngăn ngừa lỗi khóa cổng (Port Lockout) ở các phiên làm việc kế tiếp [3, 3].   

