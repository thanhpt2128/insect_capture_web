"""
realtime_bin_thingsboard.py
===========================
Phiên bản "replay từ file .bin" của luồng thời gian thực realTimeProc_infer.py.

Khác biệt duy nhất so với luồng thật:
    1. Tiến trình capture KHÔNG đọc UDP từ DCA1000 mà trích 30 frame một lần từ
       file .bin, theo chu kỳ cố định (mặc định 0.5 s).
    2. Thứ tự IQ giải mã là IIQQ (để test với các file .bin lưu theo IIQQ).
    3. Sau khi suy luận, kết quả được publish lên ThingsBoard qua MQTT
       (host / access-token do người dùng tự điền).

Mọi thứ còn lại được giữ y hệt luồng thật:
    - Kiến trúc 3 tiến trình tách rời + hàng đợi DropOldestQueue thật.
    - Chuỗi DSP InsectRadarProcessor.process_array (Range-FFT, STFT, đặc trưng).
    - Mô hình AI thật (SVM / RandomForest / XGBoost) nạp từ insect_radar_processor/models.

Payload gửi lên ThingsBoard gồm các trường kết quả hiện có cộng thêm "capture_ts"
là mốc thời gian của lô raw data ghi nhận NGAY TẠI luồng nhận.

Chạy:
    python realtime_bin_thingsboard.py \
        --bin-path data_parse/raw_data_2026-06-17-12-26-17.bin \
        --model svm \
        --tb-host  <THINGSBOARD_HOST> \
        --tb-token <ACCESS_TOKEN>

Nếu không truyền --tb-token, chương trình chạy ở chế độ dry-run: in payload ra
màn hình thay vì publish (tiện kiểm thử pipeline mà chưa có thông tin Cloud).
"""

import argparse
import json
import multiprocessing
import signal
import sys
import time
import traceback
from pathlib import Path
from queue import Empty, Full
from typing import Any, Dict, List, Optional

import numpy as np


# Ép UTF-8 cho stdout/stderr trong MỌI tiến trình (spawn re-import module này),
# tránh UnicodeEncodeError khi console Windows mặc định là cp1252 và worker in ra
# ký tự non-ASCII (ví dụ "→" trong InsectRadarProcessor, hay tiếng Việt có dấu).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


# Số int16 mỗi frame, phải khớp RadarConfig (1 TX x 4 RX x 128 ADC x 128 loop x 2 IQ).
_INT16_PER_FRAME = 131_072

# Sentinel báo hết luồng để các tiến trình xuôi dòng xả hết hàng đợi rồi dừng sạch.
_EOS = (None, None, None)


# ---------------------------------------------------------------------------
# Helpers (sao chép từ realTimeProc_infer.py để file độc lập, không kéo theo
# phần phụ thuộc phần cứng DCA1000/TI).
# ---------------------------------------------------------------------------
def _to_builtin(obj: Any) -> Any:
    """Chuyển đệ quy giá trị numpy / container về dạng JSON an toàn."""
    if obj is None or isinstance(obj, (int, float, str, bool)):
        return obj

    item = getattr(obj, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:
            pass

    if isinstance(obj, dict):
        return {str(k): _to_builtin(v) for k, v in obj.items()}

    if isinstance(obj, (list, tuple)):
        return [_to_builtin(x) for x in obj]

    tolist = getattr(obj, "tolist", None)
    if callable(tolist):
        try:
            return _to_builtin(tolist())
        except Exception:
            pass

    return str(obj)


def _resolve_repo_path(path_raw: str, default_dir: str = "") -> str:
    """Chấp nhận đường dẫn tuyệt đối, tương đối repo, hoặc tên file dưới thư mục mặc định."""
    path_raw = (path_raw or "").strip().strip("\"'")
    if not path_raw:
        return ""

    path = Path(path_raw)
    if path.is_absolute():
        return str(path)

    repo_root = Path(__file__).resolve().parent
    candidates = []
    if default_dir:
        candidates.append((repo_root / default_dir / path).resolve())
    candidates.append((repo_root / path).resolve())

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return str(candidates[0])


def _frame_int16_from_cfg(cfg_path: str) -> Optional[int]:
    """Suy số int16 mỗi frame từ tệp .cfg (None nếu không phân giải được)."""
    path = Path(_resolve_repo_path(cfg_path, default_dir="configFiles"))
    if not path.exists():
        return None

    rx_mask = samples = None
    chirp_start = chirp_end = loops = None
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as cfg_file:
            for raw_line in cfg_file:
                line = raw_line.strip()
                if not line or line.startswith(("%", "#")):
                    continue
                parts = line.split()
                cmd = parts[0]
                if cmd == "channelCfg" and len(parts) >= 3:
                    rx_mask = int(float(parts[1]))
                elif cmd == "profileCfg" and len(parts) >= 11:
                    samples = int(float(parts[10]))
                elif cmd == "frameCfg" and len(parts) >= 4:
                    chirp_start = int(float(parts[1]))
                    chirp_end = int(float(parts[2]))
                    loops = int(float(parts[3]))
    except Exception:
        return None

    if None in (rx_mask, samples, chirp_start, chirp_end, loops):
        return None

    num_rx = bin(int(rx_mask)).count("1")
    chirps_per_loop = int(chirp_end) - int(chirp_start) + 1
    if num_rx <= 0 or chirps_per_loop <= 0 or samples <= 0 or loops <= 0:
        return None
    return int(loops) * chirps_per_loop * num_rx * int(samples) * 2


def _build_range_plot(result: Dict[str, Any], range_resolution_m: float) -> Optional[Dict[str, Any]]:
    """Dựng range_plot tương thích web từ viz của InsectRadarProcessor (giống luồng thật)."""
    viz = result.get("viz") or {}
    rtm = viz.get("rtm_db")
    if rtm is None:
        return None

    rtm = np.asarray(rtm, dtype=np.float32)
    if rtm.ndim != 2 or rtm.size == 0:
        return None

    n_frames, n_bins = rtm.shape
    profile = rtm.mean(axis=0)
    finite = rtm[np.isfinite(rtm)]
    min_db = float(np.min(finite)) if finite.size else 0.0
    max_db = float(np.max(finite)) if finite.size else 1.0

    def _round_row(row: Any) -> List[float]:
        return [round(float(v), 3) for v in np.asarray(row).ravel().tolist()]

    bin_min = viz.get("range_bin_min")
    bin_max = viz.get("range_bin_max")
    return {
        "ready": True,
        "frame_count": int(n_frames),
        "range_bins": int(n_bins),
        "bin": list(range(int(n_bins))),
        "range_profile": _round_row(profile),
        "range_time": [_round_row(row) for row in rtm.tolist()],
        "min_db": round(min_db, 3),
        "max_db": round(max_db, 3),
        "range_resolution_m": round(float(range_resolution_m), 6),
        "range_bin_min": int(bin_min) if bin_min is not None else None,
        "range_bin_max": int(bin_max) if bin_max is not None else None,
    }


class DropOldestQueue:
    """Hàng đợi liên tiến trình có giới hạn, loại bỏ phần tử cũ nhất khi đầy."""

    def __init__(self, maxsize: int):
        self.queue = multiprocessing.Queue(maxsize=maxsize)
        self.lock = multiprocessing.Lock()

    def put(self, item: Any) -> None:
        with self.lock:
            try:
                self.queue.put(item, block=False)
                return
            except Full:
                pass
            try:
                self.queue.get(block=False)
            except Empty:
                pass
            try:
                self.queue.put(item, block=False)
            except Full:
                pass

    def get(self, block: bool = True, timeout: Optional[float] = None) -> Any:
        return self.queue.get(block=block, timeout=timeout)

    def close(self) -> None:
        try:
            self.queue.close()
            self.queue.join_thread()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Process 1 — Capture: trích 30 frame một lần từ file .bin theo chu kỳ.
# ---------------------------------------------------------------------------
def capture_worker_process(
    exit_event: multiprocessing.Event,
    preprocess_queue: DropOldestQueue,
    args_dict: Dict[str, Any],
) -> None:
    """Thay cho DCA1000: đọc tuần tự từng lô numframes khung từ file .bin."""
    print("[+] Capture Process (bin replay) initiated.", flush=True)

    bin_path = _resolve_repo_path(str(args_dict["bin_path"]), default_dir="data_parse")
    batch_int16 = int(args_dict["numframes"]) * _INT16_PER_FRAME
    interval = float(args_dict["interval"])
    loop_file = bool(args_dict["loop_file"])

    if not Path(bin_path).exists():
        print(f"[-] Bin file not found: {bin_path}", flush=True)
        exit_event.set()
        return

    print(f"[+] Replaying: {bin_path}  (batch = {args_dict['numframes']} frames, "
          f"interval = {interval}s, loop = {loop_file})", flush=True)

    seq = 0
    try:
        while not exit_event.is_set():
            with open(bin_path, "rb") as fh:
                while not exit_event.is_set():
                    raw = np.fromfile(fh, dtype=np.int16, count=batch_int16)
                    if raw.size < batch_int16:
                        # Hết file hoặc lô cuối không đủ frame.
                        break
                    # Mốc thời gian của lô raw ngay tại luồng nhận.
                    capture_ts = time.time()
                    preprocess_queue.put((seq, capture_ts, raw))
                    seq += 1
                    if interval > 0:
                        time.sleep(interval)

            if not loop_file:
                print("[+] End of bin file reached. Draining downstream...", flush=True)
                break
            print("[*] Looping bin file from start.", flush=True)

        # Kết thúc sạch: gửi sentinel để DSP/AI xử lý nốt hàng đợi rồi tự dừng,
        # thay vì set exit_event ngay (sẽ làm rớt các lô chưa xử lý).
        if not exit_event.is_set():
            preprocess_queue.put(_EOS)

    except Exception as exc:
        print(f"[-] Capture Process error: {exc}", flush=True)
        traceback.print_exc()
        exit_event.set()
    finally:
        print("[+] Capture Process terminated.", flush=True)


# ---------------------------------------------------------------------------
# Process 2 — DSP: y hệt luồng thật, chỉ khác iq_order = IIQQ.
# ---------------------------------------------------------------------------
def preprocessing_worker_process(
    exit_event: multiprocessing.Event,
    preprocess_queue: DropOldestQueue,
    ai_queue: DropOldestQueue,
    args_dict: Dict[str, Any],
) -> None:
    """Chuỗi DSP InsectRadarProcessor — Range-FFT, STFT, trích đặc trưng."""
    from insect_radar_processor.insect_radar_processor import InsectRadarProcessor

    print("[+] Preprocessing Process initiated.", flush=True)

    n_frames = int(args_dict["numframes"])
    expected_int16 = n_frames * _INT16_PER_FRAME
    if n_frames != 30:
        print(f"[!] WARNING: numframes={n_frames}. InsectRadarProcessor expects 30 frames.",
              flush=True)

    cfg_frame = _frame_int16_from_cfg(str(args_dict.get("cfg_path", "")))
    if cfg_frame is not None and cfg_frame != _INT16_PER_FRAME:
        print(f"[-] FATAL: cfg implies {cfg_frame} int16/frame, but the trained model "
              f"requires {_INT16_PER_FRAME}. Select a matching cfg. Stopping.", flush=True)
        exit_event.set()
        return

    # Giải mã theo IIQQ — phục vụ test với file .bin lưu theo thứ tự IIQQ.
    processor = InsectRadarProcessor(
        range_bin_min=int(args_dict.get("range_bin_min", 15)),
        range_bin_max=int(args_dict.get("range_bin_max", 20)),
        iq_order="IIQQ",
    )

    try:
        while not exit_event.is_set():
            try:
                seq, ts, raw_data = preprocess_queue.get(block=True, timeout=1.0)
            except Empty:
                continue
            except Exception as exc:
                if not exit_event.is_set():
                    print(f"[-] Preprocessing queue read failure: {exc}", flush=True)
                continue

            # Sentinel hết luồng: chuyển tiếp cho AI rồi dừng sạch.
            if raw_data is None:
                ai_queue.put(_EOS)
                break

            try:
                actual = int(getattr(raw_data, "size", 0) or 0)
                if actual != expected_int16:
                    print(f"[!] seq={seq}: expected {expected_int16} int16, got {actual}. Skip.",
                          flush=True)
                    continue

                result = processor.process_array(raw_data)
                range_plot = _build_range_plot(result, processor.cfg.range_resolution)

                proc_result = {
                    "is_insect":       result["is_insect"],
                    "power_threshold": result["power_threshold"],
                    "features":        result["features"],
                    "reason":          result.get("reason"),
                    "range_plot":      range_plot,
                }
                ai_queue.put((seq, ts, proc_result))

            except Exception as exc:
                print(f"[-] Preprocessing failure at seq={seq}: {exc}", flush=True)
                traceback.print_exc()

    finally:
        print("[+] Preprocessing Process terminated.", flush=True)


# ---------------------------------------------------------------------------
# Process 3 — AI: suy luận thật + publish ThingsBoard qua MQTT.
# ---------------------------------------------------------------------------
def ai_worker_process(
    exit_event: multiprocessing.Event,
    ai_queue: DropOldestQueue,
    args_dict: Dict[str, Any],
) -> None:
    """Nạp mô hình thật, suy luận và gửi kết quả lên ThingsBoard."""
    import joblib

    print("[+] AI Worker Process initiated.", flush=True)

    # ── Nạp mô hình một lần ──────────────────────────────────────────────
    models_dir = Path(__file__).resolve().parent / "insect_radar_processor" / "models"
    model_name = str(args_dict.get("model", "svm"))
    model_files = {"svm": "svm_pipeline.pkl", "rf": "randomforest.pkl", "xgb": "xgboost.pkl"}

    try:
        model = joblib.load(models_dir / model_files[model_name])
        feature_names = joblib.load(models_dir / "feature_names.pkl")
        print(f"[+] Model '{model_name}' loaded ({len(feature_names)} features).", flush=True)
    except Exception as exc:
        print(f"[-] Failed to load model '{model_name}': {exc}", flush=True)
        exit_event.set()
        return

    # ── Kết nối ThingsBoard MQTT (hoặc dry-run nếu thiếu token) ───────────
    tb_token = str(args_dict.get("tb_token") or "").strip()
    tb_host = str(args_dict.get("tb_host") or "").strip()
    tb_port = int(args_dict.get("tb_port", 1883))
    tb_topic = str(args_dict.get("tb_topic") or "v1/devices/me/telemetry")
    tb_qos = int(args_dict.get("tb_qos", 1))
    dry_run = (not tb_token) or (not tb_host)

    client = None
    if dry_run:
        print("[!] ThingsBoard token/host trống → chạy DRY-RUN: in payload thay vì publish.",
              flush=True)
    else:
        try:
            import paho.mqtt.client as mqtt
            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"insect_rt_{int(time.time())}",
            )
            client.username_pw_set(tb_token)
            client.connect(tb_host, tb_port, keepalive=60)
            client.loop_start()
            print(f"[+] Connected to ThingsBoard {tb_host}:{tb_port}, topic '{tb_topic}'.",
                  flush=True)
        except Exception as exc:
            print(f"[-] MQTT connect failed: {exc} → chuyển sang DRY-RUN.", flush=True)
            client = None
            dry_run = True

    try:
        while not exit_event.is_set():
            try:
                seq, ts, proc_result = ai_queue.get(block=True, timeout=1.0)
            except Empty:
                continue
            except Exception as exc:
                if not exit_event.is_set():
                    print(f"[-] AI queue read failure: {exc}", flush=True)
                continue

            # Sentinel hết luồng: dừng và báo cho main kết thúc.
            if proc_result is None:
                print("[+] End-of-stream reached at AI worker.", flush=True)
                exit_event.set()
                break

            # ── Suy luận (giống luồng thật) ──────────────────────────────
            try:
                if not proc_result["is_insect"]:
                    ai_result = {
                        "label":  "background",
                        "score":  None,
                        "proba":  None,
                        "reason": proc_result["reason"],
                    }
                else:
                    features = proc_result["features"]
                    X = np.array([features[n] for n in feature_names],
                                 dtype=np.float64).reshape(1, -1)
                    raw_pred = model.predict(X)[0]

                    if hasattr(model, "_label_encoder"):
                        pred = str(model._label_encoder.inverse_transform([raw_pred])[0])
                        classes = model._label_encoder.classes_
                    else:
                        pred = str(raw_pred)
                        classes = model.classes_

                    proba = None
                    if hasattr(model, "predict_proba"):
                        p = model.predict_proba(X)[0]
                        proba = {str(c): float(v) for c, v in zip(classes, p)}

                    ai_result = {
                        "label": pred,
                        "score": float(max(proba.values())) if proba else None,
                        "proba": proba,
                    }
            except Exception as exc:
                print(f"[-] Inference failure at seq={seq}: {exc}", flush=True)
                traceback.print_exc()
                ai_result = {"label": "error", "score": None, "proba": None}

            # ── Đóng gói telemetry cho ThingsBoard ───────────────────────
            # Các trường kết quả hiện có + capture_ts (mốc thời gian lô raw tại luồng nhận).
            values: Dict[str, Any] = {
                "seq":             int(seq),
                "capture_ts":      float(ts),          # giây, mốc thu lô raw
                "is_insect":       bool(proc_result["is_insect"]),
                "power_threshold": float(proc_result["power_threshold"]),
                "label":           ai_result["label"],
                "score":           ai_result["score"],
            }
            if ai_result.get("proba"):
                for cls, val in ai_result["proba"].items():
                    values[f"proba_{cls}"] = float(val)

            # ThingsBoard telemetry: ts (ms) = thời điểm thu lô raw.
            tb_payload = {"ts": int(float(ts) * 1000), "values": _to_builtin(values)}
            encoded = json.dumps(tb_payload, ensure_ascii=False)

            if dry_run or client is None:
                print(f"[DRY-RUN telemetry] {encoded}", flush=True)
            else:
                try:
                    res = client.publish(tb_topic, encoded, qos=tb_qos)
                    if res.rc != 0:
                        print(f"[!] MQTT publish rc={res.rc} (seq={seq}).", flush=True)
                except Exception as exc:
                    print(f"[-] Publish failed at seq={seq}: {exc}", flush=True)

    except Exception as exc:
        print(f"[-] AI Worker error: {exc}", flush=True)
        traceback.print_exc()
        exit_event.set()
    finally:
        if client is not None:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass
        print("[+] AI Worker Process terminated.", flush=True)


# ---------------------------------------------------------------------------
# main — tạo và điều phối các tiến trình (giống realTimeProc_infer.py).
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay .bin qua pipeline đa tiến trình + publish ThingsBoard."
    )
    parser.add_argument("--bin-path", type=str, required=True,
                        help="File .bin (IIQQ) để replay; tên file dưới data_parse/ cũng được.")
    parser.add_argument("--cfg-path", type=str, default="cfg128_128_100fps.cfg",
                        help="Tệp .cfg để kiểm tra hình học khung (mặc định 128x128).")
    parser.add_argument("--numframes", type=int, default=30)
    parser.add_argument("--interval", type=float, default=0.5,
                        help="Chu kỳ trích mỗi lô (giây). Mặc định 0.5s.")
    parser.add_argument("--loop-file", action=argparse.BooleanOptionalAction, default=False,
                        help="Lặp lại file .bin khi hết. Mặc định tắt.")
    parser.add_argument("--model", type=str, default="svm", choices=["svm", "rf", "xgb"])
    parser.add_argument("--range-bin-min", type=int, default=15)
    parser.add_argument("--range-bin-max", type=int, default=20)
    parser.add_argument("--preprocess-queue-size", type=int, default=10)
    parser.add_argument("--ai-queue-size", type=int, default=10)
    # ── ThingsBoard (người dùng tự điền host/token) ──────────────────────
    parser.add_argument("--tb-host", type=str, default="",
                        help="Địa chỉ ThingsBoard MQTT broker (để trống = dry-run).")
    parser.add_argument("--tb-port", type=int, default=1883)
    parser.add_argument("--tb-token", type=str, default="",
                        help="Access token thiết bị (để trống = dry-run).")
    parser.add_argument("--tb-topic", type=str, default="v1/devices/me/telemetry")
    parser.add_argument("--tb-qos", type=int, default=1, choices=[0, 1])

    args = parser.parse_args(argv)
    args_dict = vars(args)
    args_dict["bin_path"] = _resolve_repo_path(str(args_dict["bin_path"]), default_dir="data_parse")
    args_dict["cfg_path"] = _resolve_repo_path(str(args_dict["cfg_path"]), default_dir="configFiles")

    multiprocessing.freeze_support()

    exit_event = multiprocessing.Event()
    preprocess_queue = DropOldestQueue(maxsize=max(1, int(args.preprocess_queue_size)))
    ai_queue = DropOldestQueue(maxsize=max(1, int(args.ai_queue_size)))

    def signal_handler(signum: int, _frame: Any) -> None:
        print(f"[!] Signal {signum} caught. Stopping all subprocesses...", flush=True)
        exit_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal_handler)

    capture_proc = multiprocessing.Process(
        target=capture_worker_process,
        args=(exit_event, preprocess_queue, args_dict),
        name="BinReplayCaptureProcess",
    )
    preprocess_proc = multiprocessing.Process(
        target=preprocessing_worker_process,
        args=(exit_event, preprocess_queue, ai_queue, args_dict),
        name="DSPPreprocessingProcess",
    )
    ai_proc = multiprocessing.Process(
        target=ai_worker_process,
        args=(exit_event, ai_queue, args_dict),
        name="InferenceThingsBoardProcess",
    )
    processes = [capture_proc, preprocess_proc, ai_proc]

    print("[*] Bin-replay multi-process pipeline initializing...", flush=True)
    for proc in processes:
        proc.start()

    try:
        while not exit_event.is_set():
            if any(proc.exitcode not in (None, 0) for proc in processes):
                exit_event.set()
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        exit_event.set()

    print("[*] Exit flag set. Tearing down...", flush=True)
    for proc in processes:
        proc.join(timeout=3)
    for proc in processes:
        if proc.is_alive():
            print(f"[!] Force terminating {proc.name}...", flush=True)
            proc.terminate()
            proc.join(timeout=3)

    preprocess_queue.close()
    ai_queue.close()
    print("[*] Pipeline fully stopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
