"""
usage_guide.py
==============
Huong dan su dung insect_radar_processor.py.

Cac ham co the import tu module khac:
    from usage_guide import plot_range_time_map, plot_spectrogram
    from usage_guide import predict_insect, batch_process
    from usage_guide import debug_result, check_flatness_threshold

Chay truc tiep de demo (can co file .raw that):
    python usage_guide.py
"""

import numpy as np
import matplotlib.pyplot as plt
import pandas as pd
from insect_radar_processor import InsectRadarProcessor, RadarConfig


# =============================================================================
# PHAN 2 -- VE RANGE-TIME MAP
# =============================================================================

def plot_range_time_map(result: dict, title: str = "Range-Time Map"):
    """
    Ve Range-Time Map tu viz data trong result dict.

    Parameters
    ----------
    result : dict -- output cua processor.process() hoac inference.predict()
                     Can co cac key: 'viz', 'power_threshold', 'is_insect'
    title  : str  -- tieu de plot
    """
    viz = result['viz']

    rtm_db       = viz['rtm_db']        # [n_frames, n_range_bins]
    vmin         = viz['rtm_vmin']
    vmax         = viz['rtm_vmax']
    smooth_peaks = viz['smooth_peaks']  # [n_frames]
    rmin         = viz['range_bin_min']
    rmax         = viz['range_bin_max']

    n_frames = rtm_db.shape[0]

    plt.figure(figsize=(12, 5))

    plt.imshow(
        rtm_db.T,
        aspect='auto',
        origin='lower',
        cmap='viridis',
        vmin=vmin,
        vmax=vmax,
    )

    plt.plot(
        np.arange(n_frames),
        smooth_peaks,
        color='cyan',
        linewidth=1.8,
        label='Peak track',
    )

    plt.axhline(y=rmin, color='white', linestyle='--', linewidth=1.0, alpha=0.7)
    plt.axhline(y=rmax, color='white', linestyle='--', linewidth=1.0, alpha=0.7)

    plt.colorbar(label='Bien do (dB)')
    plt.title(
        f"{title}\n"
        f"[power_threshold={result['power_threshold']:.4f}, "
        f"is_insect={result['is_insect']}]"
    )
    plt.xlabel("Frame Index (thoi gian)")
    plt.ylabel("Range Bin (khoang cach)")
    plt.legend()
    plt.tight_layout()
    plt.show()


# =============================================================================
# PHAN 3 -- VE SPECTROGRAM (MICRO-DOPPLER)
# =============================================================================

def plot_spectrogram(result: dict,
                     ylim_hz: float = 800.0,
                     vmin_db: float = -25.0,
                     vmax_db: float = 0.0,
                     title: str = "Micro-Doppler Spectrogram"):
    """
    Ve spectrogram micro-Doppler tu viz data.

    Parameters
    ----------
    result   : dict   -- output cua processor.process() hoac inference.predict()
                         Can co cac key: 'viz', 'power_threshold', 'is_insect'
    ylim_hz  : float  -- gioi han truc Y (Hz), thuong 800 Hz du cho con trung
    vmin_db  : float  -- gioi han duoi mau (dB)
    vmax_db  : float  -- gioi han tren mau (dB)
    title    : str
    """
    viz = result['viz']

    f_axis = viz['f_axis']   # [n_freq]
    t_axis = viz['t_axis']   # [n_time]
    Sxx_db = viz['Sxx_db']   # [n_freq, n_time]

    plt.figure(figsize=(12, 5))
    plt.pcolormesh(
        t_axis,
        f_axis,
        Sxx_db,
        shading='gouraud',
        cmap='jet',
        vmin=vmin_db,
        vmax=vmax_db,
    )

    plt.ylim(-ylim_hz, ylim_hz)

    plt.colorbar(label='Power (dB)')
    plt.title(
        f"{title}\n"
        f"[power_threshold={result['power_threshold']:.4f}, "
        f"is_insect={result['is_insect']}]"
    )
    plt.xlabel("Time (s)")
    plt.ylabel("Doppler Frequency (Hz)")
    plt.tight_layout()
    plt.show()


# =============================================================================
# PHAN 4 -- DUA VAO MODEL PHAN LOAI
# =============================================================================

def predict_insect(raw_path: str, model, scaler=None, feat_cols=None,
                   processor: InsectRadarProcessor = None):
    """
    End-to-end: doc file .raw -> preprocess -> predict loai con trung.

    Parameters
    ----------
    raw_path  : str    -- duong dan file .raw
    model     : object -- model da train (sklearn-compatible)
    scaler    : object -- StandardScaler (None neu model khong can scale)
    feat_cols : list   -- danh sach ten features theo dung thu tu khi train
    processor : InsectRadarProcessor -- neu None, tu dong khoi tao voi mac dinh

    Returns
    -------
    dict voi keys:
        'prediction'      : str hoac None (None neu background)
        'is_insect'       : bool
        'power_threshold' : float
        'reason'          : str (neu background)
    """
    if processor is None:
        processor = InsectRadarProcessor(range_bin_min=15, range_bin_max=20)

    result = processor.process(raw_path)

    if not result['is_insect']:
        return {
            'prediction': None,
            'is_insect' : False,
            'power_threshold'  : result['power_threshold'],
            'reason'    : result['reason'],
        }

    features = result['features']
    if feat_cols is None:
        X = np.array(list(features.values()), dtype=np.float32).reshape(1, -1)
    else:
        X = np.array([features[col] for col in feat_cols],
                     dtype=np.float32).reshape(1, -1)

    if scaler is not None:
        X = scaler.transform(X)

    pred = model.predict(X)[0]

    return {
        'prediction': pred,
        'is_insect' : True,
        'power_threshold'  : result['power_threshold'],
    }


# =============================================================================
# PHAN 5 -- XU LY NHIEU FILE VA XUAT CSV
# =============================================================================

def batch_process(file_list: list, processor: InsectRadarProcessor,
                  save_csv: str = None) -> pd.DataFrame:
    """
    Xu ly nhieu file .raw va gop features thanh DataFrame.

    Parameters
    ----------
    file_list  : list of dict -- moi dict gom:
                    {'path': str, 'label': str, 'insect_id': str}
    processor  : InsectRadarProcessor
    save_csv   : str -- duong dan luu CSV (None = khong luu)

    Returns
    -------
    df : DataFrame chua tat ca features + metadata
    """
    rows = []

    for item in file_list:
        path      = item['path']
        label     = item.get('label', 'unknown')
        insect_id = item.get('insect_id', 'unknown')

        print(f"Processing: {path.split('/')[-1]} ...", end=" ")

        result = processor.process(path)

        if not result['is_insect']:
            print(f"SKIP ({result['reason']})")
            continue

        row = {
            'label'     : label,
            'insect_id' : insect_id,
            'path'      : path,
            'power'     : result['power_threshold'],
        }
        row.update(result['features'])
        rows.append(row)

        print(f"OK (power_threshold={result['power_threshold']:.4f})")

    if not rows:
        print("Khong co file nao qua duoc bo loc power threshold.")
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    if save_csv:
        df.to_csv(save_csv, index=False, encoding='utf-8-sig')
        print(f"\nSaved {len(df)} rows to: {save_csv}")

    return df


# =============================================================================
# PHAN 6 -- DEBUG KHI KET QUA KHONG NHU KY VONG
# =============================================================================

def debug_result(result: dict):
    """
    In thong tin debug day du cho 1 result dict.
    Dung khi muon hieu tai sao mot file bi classify sai / bi loai.
    """
    print("=" * 60)
    print(f"is_insect            : {result['is_insect']}")
    print(f"power_threshold: {result['power_threshold']:.6f}")

    if not result['is_insect']:
        print(f"reason               : {result['reason']}")

    viz = result['viz']
    print(f"\n--- Range-Time Map ---")
    print(f"  rtm_db shape  : {viz['rtm_db'].shape}")
    print(f"  rtm_vmin/vmax : {viz['rtm_vmin']:.2f} / {viz['rtm_vmax']:.2f} dB")
    print(f"  peak range    : bin {viz['smooth_peaks'].min()} to {viz['smooth_peaks'].max()}")
    print(f"  peak std      : {viz['smooth_peaks'].std():.2f}")

    print(f"\n--- Spectrogram ---")
    print(f"  f_axis shape  : {viz['f_axis'].shape} "
          f"({viz['f_axis'].min():.1f} to {viz['f_axis'].max():.1f} Hz)")
    print(f"  t_axis shape  : {viz['t_axis'].shape}")
    print(f"  Sxx_db shape  : {viz['Sxx_db'].shape}")
    print(f"  Sxx_db range  : {viz['Sxx_db'].min():.2f} to {viz['Sxx_db'].max():.2f} dB")

    if result.get('features'):
        print(f"\n--- Features ({len(result['features'])} total) ---")
        important = ['bandRatio_150_350', 'bandRatio_350_800',
                     'spectral_flatness_mean', 'spectral_entropy_mean',
                     'ridge_freq_mean', 'mfcc_00_mean']
        for k in important:
            if k in result['features']:
                print(f"  {k:35s} = {result['features'][k]:.6f}")
    print("=" * 60)


# =============================================================================
# PHAN 7 -- KIEM TRA NGUONG POWER VOI FILE NEN
# =============================================================================

def check_power_threshold(raw_paths: list, processor: InsectRadarProcessor):
    """
    Kiem tra gia tri power_threshold tren nhieu files de xac nhan
    nguong 45000 phu hop voi du lieu cua ban.
    """
    results = {}
    for path in raw_paths:
        old = processor.power_threshold
        processor.power_threshold = 1.0

        r  = processor.process(path)
        sf = r['power_threshold']

        processor.power_threshold = old
        results[path.split('/')[-1]] = sf

    print("\n--- Spectral Flatness Mean Check ---")
    print(f"{'File':<40} {'SF_mean':>10}")
    print("-" * 52)
    for fname, sf in sorted(results.items(), key=lambda x: x[1]):
        flag = "<- nen?" if sf < 45000 else "<- insect"
        print(f"{fname:<40} {sf:>10.4f}  {flag}")

    print(f"\nNguong hien tai: {processor.power_threshold}")
    values = list(results.values())
    print(f"Min: {min(values):.4f} | Max: {max(values):.4f} | Mean: {np.mean(values):.4f}")


# =============================================================================
# DEMO -- chi chay khi goi truc tiep: python usage_guide.py
# =============================================================================

if __name__ == "__main__":
    # PHAN 0 -- Khoi tao processor
    processor = InsectRadarProcessor(
        range_bin_min=15,
        range_bin_max=20,
    )

    # PHAN 1 -- Xu ly mot file (thay bang duong dan thuc)
    RAW_PATH = r"path/to/your/insect.raw"

    result = processor.process(RAW_PATH)

    print("=" * 50)
    print(f"is_insect            : {result['is_insect']}")
    print(f"power_threshold: {result['power_threshold']:.4f}")

    if result['is_insect']:
        print(f"So features          : {len(result['features'])}")
        for k, v in list(result['features'].items())[:5]:
            print(f"  {k:40s} = {v:.6f}")
    else:
        print(f"Ly do loai           : {result['reason']}")

    # Ve do thi
    plot_range_time_map(result, title="Range-Time Map -- file mau")
    plot_spectrogram(result,    title="Micro-Doppler -- file mau")

    # debug_result(result)

    # Batch processing example:
    # FILE_LIST = [
    #     {'path': r"data/bee_01_run1.raw", 'label': 'bee', 'insect_id': 'bee_01'},
    #     {'path': r"data/fly_01_run1.raw", 'label': 'fly', 'insect_id': 'fly_01'},
    # ]
    # df = batch_process(FILE_LIST, processor, save_csv="features_output.csv")

    # Flatness threshold check:
    # check_flatness_threshold(
    #     raw_paths=["bee_01.raw", "fly_01.raw", "nothing_01.raw"],
    #     processor=processor,
    # )
