"""
Correctness tests for the realtime inference pipeline (realTimeProc_infer.py +
insect_radar_processor). Run:  python test_realtime_infer.py
Needs the data_parse/*.bin captures (128x128, QQII on disk) for the end-to-end checks.
"""

import importlib.util
import json
from pathlib import Path

import numpy as np

# Load the worker module and give it the numpy handle that import_dependencies()
# would normally set inside the worker processes.
_spec = importlib.util.spec_from_file_location("rt", "realTimeProc_infer.py")
rt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rt)
rt.np = np

import insect_radar_processor.insect_radar_processor as irp

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    ok = bool(cond)
    PASS += ok
    FAIL += (not ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({extra})" if extra else ""))


# ----------------------------------------------------------------------------
# T1 — IQ decode (QQII, the realtime convention)
# ----------------------------------------------------------------------------
# One group of 4 int16 = [Q_rx0, Q_rx1, I_rx0, I_rx1]
raw = np.array([7, 8, 3, 4], dtype=np.int16)
z = irp._int16_to_complex(raw, "QQII")
# QQII: I = cols[2,3] = [3,4], Q = cols[0,1] = [7,8]  ->  z = I + jQ
check("T1 QQII decode: real=I(cols2,3), imag=Q(cols0,1)",
      np.array_equal(z, np.array([3 + 7j, 4 + 8j], dtype=np.complex64)), f"z={z}")

# ----------------------------------------------------------------------------
# T2 — QQII-decode(QQII bytes) recovers the SAME signal as IIQQ-decode(IIQQ bytes)
# ----------------------------------------------------------------------------
phys_iiqq = np.array([3, 4, 7, 8], dtype=np.int16)              # [I0,I1,Q0,Q1] on disk
z_iiqq = irp._int16_to_complex(phys_iiqq, "IIQQ")
phys_qqii = phys_iiqq.reshape(-1, 4)[:, [2, 3, 0, 1]].reshape(-1)  # same signal, QQII order
z_qqii = irp._int16_to_complex(phys_qqii, "QQII")
check("T2 QQII(QQII bytes) == IIQQ(IIQQ bytes) (model trained IIQQ, live QQII)",
      np.array_equal(z_iiqq, z_qqii))

# ----------------------------------------------------------------------------
# T3 — Range FFT peaks at the injected range bin (range processing correctness)
# ----------------------------------------------------------------------------
cfg = irp.RadarConfig()
NS, NL, NRX = cfg.num_adc_samples, cfg.num_loops_per_frame, cfg.num_rx
k0 = 17
n = np.arange(NS)
tone = np.exp(1j * 2 * np.pi * k0 * n / NS).astype(np.complex64)      # fast-time tone -> bin k0
doppler = np.exp(1j * 2 * np.pi * 0.1 * np.arange(NL)).astype(np.complex64)  # vary across chirps
cube = np.ones((1, NRX, NL, NS), dtype=np.complex64) * tone[None, None, None, :] * doppler[None, None, :, None]
rfft = irp._compute_range_fft(irp._static_clutter_removal(cube), cfg)  # [1, ant, loop, 32]
mag = np.mean(np.abs(rfft), axis=(1, 2))[0]                            # [32]
check("T3 range-FFT peak at injected bin k0=17 (after clutter removal)",
      int(np.argmax(mag)) == k0, f"argmax={int(np.argmax(mag))}")

# ----------------------------------------------------------------------------
# T4 — Cube reshape produces [n_frames, tx*rx, loops, samples]
# ----------------------------------------------------------------------------
flat = np.arange(1 * NL * NRX * NS).astype(np.complex64)
cube = irp._process_to_cube(flat, cfg)
check("T4 cube shape == [1, tx*rx, loops, samples]",
      cube.shape == (1, cfg.num_tx * NRX, NL, NS), f"{cube.shape}")

# ----------------------------------------------------------------------------
# T5/6/7 — End-to-end on a real capture (128x128, QQII on disk)
# ----------------------------------------------------------------------------
INT16 = rt._INT16_PER_FRAME
real = Path("data_parse/raw_data_2026-06-17-12-11-02.bin")
if real.exists():
    raw = np.fromfile(real, dtype=np.int16, count=30 * INT16)
    proc = irp.InsectRadarProcessor(range_bin_min=15, range_bin_max=20, iq_order="QQII")
    result = proc.process_array(raw)
    rp = rt._build_range_plot(result, proc.cfg.range_resolution)
    rtm = np.asarray(result["viz"]["rtm_db"], dtype=np.float64)

    check("T5a range_time shape == [30, 32]", np.asarray(rp["range_time"]).shape == (30, 32))
    check("T5b range_profile length == 32", len(rp["range_profile"]) == 32)
    # range_profile is defined as the per-bin mean over the batch's frames of rtm_db
    colmean = np.round(rtm.mean(axis=0), 3)
    check("T5c range_profile == mean-over-frames of rtm_db (dB)",
          np.allclose(np.asarray(rp["range_profile"]), colmean, atol=1e-2))
    check("T5d min_db/max_db bracket rtm",
          rp["min_db"] <= rtm.min() + 1e-3 and rp["max_db"] >= rtm.max() - 1e-3,
          f"min={rp['min_db']} max={rp['max_db']}")
    check("T5e range_resolution_m == c/(2B)",
          abs(rp["range_resolution_m"] - 3e8 / (2 * cfg.B)) < 1e-4, f"{rp['range_resolution_m']} m/bin")

    # physical sanity: moving-target peak near the benchtop range (~0.65 m)
    peak_bin = int(np.argmax(rp["range_profile"]))
    peak_m = peak_bin * rp["range_resolution_m"]
    check("T5f profile peak at a plausible near range (0.3-1.2 m)",
          0.3 <= peak_m <= 1.2, f"bin {peak_bin} ~ {peak_m:.2f} m")

    # T6 — full payload is JSON-serializable after _to_builtin
    payload = {
        "type": "inference", "seq": 1, "is_insect": result["is_insect"],
        "power_threshold": result["power_threshold"], "range_plot": rp,
        "result": {"label": "bee", "score": 0.9, "proba": {"bee": 0.9, "fly": 0.1}},
    }
    s = json.dumps(rt._to_builtin(payload))
    check("T6 payload JSON-serializable via _to_builtin", isinstance(s, str) and len(s) > 200,
          f"{len(s)} chars")
else:
    print(f"[SKIP] real-capture tests — {real} not found")

# ----------------------------------------------------------------------------
# T7 — cfg frame-size parsing (F2 guard)
# ----------------------------------------------------------------------------
check("T7a cfg128_128_100fps -> 131072 int16/frame",
      rt._frame_int16_from_cfg("configFiles/cfg128_128_100fps.cfg") == 131072)
check("T7b cfg128_256_cli -> 262144 (would be rejected)",
      rt._frame_int16_from_cfg("configFiles/cfg128_256_cli.cfg") == 262144)

print(f"\n==== {PASS} passed, {FAIL} failed ====")
raise SystemExit(1 if FAIL else 0)
