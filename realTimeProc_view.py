"""
realTimeProc_view.py
====================
Local desktop viewer to PROVE the realtime pipeline ingests/decodes/processes
radar data correctly. Runs the exact same DSP as the web worker
(InsectRadarProcessor) and plots range-time waterfall + range-profile locally
with matplotlib — no web, smoother for verification.

Two sources:
  * replay : stream a saved .bin in 30-frame batches (default; testable offline)
  * live   : capture from DCA1000 + TI exactly like realTimeProc_infer.py

IQ order defaults to QQII (the live DCA1000 stream / DCA-captured .bin files).
Use --iq-order IIQQ only when replaying IIQQ-ordered training files.

Examples
--------
  # Replay a capture in a live window
  python realTimeProc_view.py --source replay --bin data_parse/raw_data_2026-06-17-12-11-02.bin

  # Headless proof: process N batches and save a static snapshot image
  python realTimeProc_view.py --source replay --bin <file> --snapshot 8 --out view.png

  # Live from hardware
  python realTimeProc_view.py --source live --com-port COM5 --cfg-path cfg128_128_100fps.cfg
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np

INT16_PER_FRAME = 131_072  # 1 TX x 4 RX x 128 ADC x 128 loops x 2 (I+Q)


# --------------------------------------------------------------------------- #
# Batch sources
# --------------------------------------------------------------------------- #
def replay_batches(bin_path: str, numframes: int, loop: bool, fps: float):
    """Yield (seq, raw_int16) batches of `numframes` frames from a .bin file."""
    path = Path(bin_path)
    frame_bytes = INT16_PER_FRAME * 2
    total_frames = path.stat().st_size // frame_bytes
    if total_frames < numframes:
        raise ValueError(f"{path} has only {total_frames} frames (< {numframes})")

    batch_period = numframes / fps if fps > 0 else 0.0
    seq = 0
    while True:
        start = 0
        while start + numframes <= total_frames:
            data = np.fromfile(
                path, dtype=np.int16,
                count=numframes * INT16_PER_FRAME,
                offset=start * frame_bytes,
            )
            if data.size < numframes * INT16_PER_FRAME:
                break
            yield seq, data
            seq += 1
            start += numframes
            if batch_period > 0:
                time.sleep(batch_period)
        if not loop:
            break


def live_batches(args):
    """Yield (seq, raw_int16) batches captured from the DCA1000/radar."""
    from mmwave.dataloader import DCA1000
    from mmwave.dataloader.radars import TI

    repo = Path(__file__).resolve().parent
    cfg_path = str((repo / "configFiles" / args.cfg_path)) if not Path(args.cfg_path).is_absolute() else args.cfg_path
    dca_cfg = str((repo / "configFiles" / args.dca_cfg)) if not Path(args.dca_cfg).is_absolute() else args.dca_cfg

    dca = DCA1000()
    dca.reset_radar()
    dca.reset_fpga()
    time.sleep(7.0)
    radar = TI(cli_loc=args.com_port, cli_baud=args.cli_baud,
               data_loc=args.com_port, data_baud=args.cli_baud,
               config_file=cfg_path, verbose=False)
    try:
        radar.setFrameCfg(0)
    except Exception:
        pass
    dca.configure(dca_cfg, cfg_path)
    dca.stream_start()
    dca.fastRead_in_Cpp_thread_start(max(128, args.numframes))
    radar.startSensor()
    print("[+] Live capture started.", flush=True)

    seq = 0
    try:
        while True:
            raw = dca.fastRead_in_Cpp_thread_get(numframes=args.numframes, timeOut=2, verbose=False, sortInC=True)
            if raw is not None and getattr(raw, "size", 0) > 0:
                yield seq, raw
                seq += 1
    finally:
        try:
            radar.stopSensor(); radar.close()
        except Exception:
            pass
        try:
            dca.fastRead_in_Cpp_thread_stop(); dca.stream_stop(); dca.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Processing helper
# --------------------------------------------------------------------------- #
class Pipeline:
    def __init__(self, args):
        from insect_radar_processor.insect_radar_processor import InsectRadarProcessor
        self.proc = InsectRadarProcessor(
            range_bin_min=args.range_bin_min,
            range_bin_max=args.range_bin_max,
            iq_order=args.iq_order,
        )
        self.range_res = float(self.proc.cfg.range_resolution)
        self.n_bins = int(self.proc.cfg.n_range_bins_keep)
        self.expected = args.numframes * INT16_PER_FRAME

        self.model = None
        self.feature_names = None
        if args.model != "none":
            import joblib
            md = Path(__file__).resolve().parent / "insect_radar_processor" / "models"
            files = {"svm": "svm_pipeline.pkl", "rf": "randomforest.pkl", "xgb": "xgboost.pkl"}
            self.model = joblib.load(md / files[args.model])
            self.feature_names = joblib.load(md / "feature_names.pkl")

    def run(self, raw):
        """Return (rtm_db [n_frames, n_bins], profile_db [n_bins], label, power)."""
        if raw.size != self.expected:
            return None
        result = self.proc.process_array(raw)
        rtm = np.asarray(result["viz"]["rtm_db"], dtype=np.float32)
        profile = rtm.mean(axis=0)
        label = "insect" if result["is_insect"] else "background"
        if result["is_insect"] and self.model is not None:
            X = np.array([result["features"][n] for n in self.feature_names], dtype=np.float64).reshape(1, -1)
            try:
                pred = self.model.predict(X)[0]
                if hasattr(self.model, "_label_encoder"):
                    pred = self.model._label_encoder.inverse_transform([pred])[0]
                label = str(pred)
            except Exception:
                pass
        return rtm, profile, label, float(result["power_threshold"])


# --------------------------------------------------------------------------- #
# Rolling waterfall buffer
# --------------------------------------------------------------------------- #
class Waterfall:
    def __init__(self, n_bins, history):
        self.buf = np.full((n_bins, history), np.nan, dtype=np.float32)

    def push(self, rtm):  # rtm [n_frames, n_bins]
        cols = rtm.T  # [n_bins, n_frames]
        k = cols.shape[1]
        self.buf = np.roll(self.buf, -k, axis=1)
        self.buf[:, -k:] = cols

    def clim(self):
        finite = self.buf[np.isfinite(self.buf)]
        if finite.size < 16:
            return 0.0, 1.0
        return float(np.percentile(finite, 5)), float(np.percentile(finite, 99))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv=None):
    p = argparse.ArgumentParser(description="Local range-time / range-profile viewer.")
    p.add_argument("--source", choices=["replay", "live"], default="replay")
    p.add_argument("--bin", dest="bin_path", type=str, default="")
    p.add_argument("--numframes", type=int, default=30)
    p.add_argument("--iq-order", choices=["QQII", "IIQQ"], default="QQII")
    p.add_argument("--range-bin-min", type=int, default=15)
    p.add_argument("--range-bin-max", type=int, default=20)
    p.add_argument("--model", choices=["svm", "rf", "xgb", "none"], default="svm")
    p.add_argument("--history", type=int, default=300, help="waterfall width (frames)")
    p.add_argument("--fps", type=float, default=100.0, help="replay frame rate for pacing")
    p.add_argument("--loop", action="store_true", help="loop the replay file")
    p.add_argument("--snapshot", type=int, default=0, help="headless: process N batches and save --out PNG")
    p.add_argument("--out", type=str, default="view.png")
    # live-only
    p.add_argument("--com-port", type=str, default="")
    p.add_argument("--cli-baud", type=int, default=921600)
    p.add_argument("--cfg-path", type=str, default="cfg128_128_100fps.cfg")
    p.add_argument("--dca-cfg", type=str, default="cf.json")
    args = p.parse_args(argv)

    if args.source == "replay" and not args.bin_path:
        p.error("--bin is required for --source replay")

    import matplotlib
    if args.snapshot > 0:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    pipe = Pipeline(args)
    wf = Waterfall(pipe.n_bins, args.history)
    range_axis = np.arange(pipe.n_bins) * pipe.range_res
    max_range = pipe.n_bins * pipe.range_res

    fig, (ax_wf, ax_prof) = plt.subplots(2, 1, figsize=(9, 6), facecolor="#101827")
    fig.suptitle("Radar pipeline verification — range-time & range-profile", color="#e5e7eb")

    im = ax_wf.imshow(wf.buf, aspect="auto", origin="lower", cmap="viridis",
                      extent=[0, args.history, 0, max_range], vmin=0, vmax=1, interpolation="nearest")
    ax_wf.set_ylabel("Range (m)", color="#cbd5e1")
    ax_wf.set_xlabel("Time (frames)", color="#cbd5e1")
    ax_wf.tick_params(colors="#cbd5e1")
    cbar = fig.colorbar(im, ax=ax_wf)
    cbar.set_label("dB", color="#cbd5e1")

    (line,) = ax_prof.plot(range_axis, np.zeros(pipe.n_bins), color="#38bdf8", lw=1.8)
    (peak_pt,) = ax_prof.plot([], [], "o", color="#fde047", ms=7)
    ax_prof.set_xlim(0, max_range)
    ax_prof.set_xlabel("Range (m)", color="#cbd5e1")
    ax_prof.set_ylabel("Amplitude (dB)", color="#cbd5e1")
    ax_prof.tick_params(colors="#cbd5e1")
    ax_prof.grid(True, color="#233047")
    for ax in (ax_wf, ax_prof):
        ax.set_facecolor("#101827")

    source = (replay_batches(args.bin_path, args.numframes, args.loop or args.snapshot == 0, args.fps)
              if args.source == "replay" else live_batches(args))

    def update(item):
        seq, raw = item
        out = pipe.run(raw)
        if out is None:
            print(f"[!] seq={seq}: wrong size {raw.size} (expected {pipe.expected}); skipped", flush=True)
            return
        rtm, profile, label, power = out
        wf.push(rtm)
        vmin, vmax = wf.clim()
        im.set_data(wf.buf)
        im.set_clim(vmin, vmax)
        line.set_ydata(profile)
        ax_prof.set_ylim(float(np.min(profile)) - 1, float(np.max(profile)) + 1)
        pk = int(np.argmax(profile))
        peak_pt.set_data([range_axis[pk]], [profile[pk]])
        ax_wf.set_title(f"seq={seq}  label={label}  power={power:,.0f}  peak={range_axis[pk]:.2f} m",
                        color="#9ca3af", fontsize=10)
        return im, line, peak_pt

    if args.snapshot > 0:
        n = 0
        for item in source:
            update(item)
            n += 1
            if n >= args.snapshot:
                break
        fig.savefig(args.out, dpi=120, facecolor=fig.get_facecolor())
        print(f"[+] Saved snapshot after {n} batches -> {args.out}", flush=True)
        return 0

    from matplotlib.animation import FuncAnimation
    _anim = FuncAnimation(fig, update, frames=source, interval=1, blit=False, cache_frame_data=False)
    plt.show()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
