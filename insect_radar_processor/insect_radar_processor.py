"""
insect_radar_processor.py
=========================
Framework xử lý tín hiệu radar AWR1843 để phát hiện và phân loại côn trùng.

LUỒNG XỬ LÝ (pipeline):
    File .raw (30 frames)
        → [1] Đọc raw → complex IQ
        → [2] Reshape → radar cube [Frame, Ant, Loop, Sample]
        → [3] Static clutter removal
        → [4] Hanning window + Range FFT → range_fft [Frame, Ant, Loop, RangeBin]
        → [5] Peak tracking trên Range-Time Map
        → [6] Extract slow-time signal tại 3 bins: center-1, center, center+1
        → [7] High-pass filter (loại clutter di chuyển chậm)
        → [8] STFT tổng hợp 12 spectrograms (3 bins × 4 anten)
        → [9] Kiểm tra power_threshold
               > 45000  → có côn trùng → tiếp tục
               ≤ 45000  → background (return dict với is_insect=False)
        → [10] Trích xuất features (band power, MFCC, WBF, v.v.)
        → [11] Return dict đầy đủ

OUTPUT của process():
    {
        'is_insect'              : bool,
        'power_threshold'        : float,
        'reason'                 : str,       # chỉ khi is_insect=False
        'features'               : dict,      # None khi is_insect=False
        'viz': {
            # Dữ liệu để vẽ Range-Time Map
            'rtm_db'       : ndarray [n_frames, n_range_bins]
            'rtm_vmin'     : float
            'rtm_vmax'     : float
            'smooth_peaks' : ndarray [n_frames]
            'range_bin_min': int
            'range_bin_max': int
            # Dữ liệu để vẽ Spectrogram
            'f_axis'  : ndarray [n_freq]
            't_axis'  : ndarray [n_time]
            'Sxx_db'  : ndarray [n_freq, n_time]
        }
    }
"""

# =============================================================================
# SECTION 1 — IMPORTS
# =============================================================================

import numpy as np
from dataclasses import dataclass
from typing import Optional
from scipy import signal as scipy_signal
from scipy.signal import medfilt, butter, filtfilt
from scipy.fft import dct
from scipy.stats import trim_mean as scipy_trim_mean


# =============================================================================
# SECTION 2 — RADAR CONFIG
# =============================================================================

@dataclass
class RadarConfig:
    """
    Tập hợp toàn bộ thông số cấu hình radar AWR1843.

    CÁCH DÙNG:
        # Dùng mặc định (60fps, cấu hình đo côn trùng hiện tại)
        cfg = RadarConfig()

        # Hoặc tuỳ chỉnh 1 tham số
        cfg = RadarConfig(highpass_cutoff_hz=50)

    THÔNG SỐ NÀO CẦN QUAN TÂM KHI THAY ĐỔI SETUP:
        - range_bin_min/max  : điều chỉnh theo khoảng cách đặt côn trùng
        - highpass_cutoff_hz : tăng nếu muốn loại bỏ nhiều clutter hơn
        - nperseg/noverlap   : ảnh hưởng resolution của spectrogram
    """
    # --- Cấu hình anten ---
    num_tx: int = 1
    num_rx: int = 4

    # --- Cấu hình ADC và chirp ---
    num_adc_samples: int = 128       # số sample mỗi chirp
    num_loops_per_frame: int = 128   # số chirp mỗi frame

    # --- Thông số RF ---
    f_c: float = 77e9           # tần số trung tâm (Hz)
    Fs: float = 4e6             # tần số sampling ADC (Hz)
    B: float = 2586.24e6        # bandwidth (Hz)
    idle_time: float = 5e-6     # thời gian nghỉ giữa chirps (s)
    S: float = 80.820e12        # hệ số slope chirp (Hz/s)
    c: float = 3e8              # tốc độ ánh sáng (m/s)

    # --- Range FFT ---
    n_range_bins_keep: int = 32  # giữ 32 bin đầu (phần tử quan tâm)

    # --- STFT (Spectrogram) ---
    nperseg: int = 128           # số sample mỗi cửa sổ STFT
    noverlap: int = 100          # số sample overlap giữa 2 cửa sổ
    nfft: int = 2048             # số điểm FFT (zero-padding → tăng resolution tần số)
    stft_window: str = 'hamming' # loại cửa sổ

    # --- High-pass filter ---
    highpass_cutoff_hz: float = 50.0  # tần số cắt (Hz)
    highpass_order: int = 4            # bậc lọc Butterworth

    # --- Spectrogram reference power ---
    P_ref_global: float = 150.0

    def __post_init__(self):
        """Tính các tham số dẫn xuất sau khi khởi tạo."""
        self.num_chirps_per_frame = self.num_tx * self.num_loops_per_frame
        # Tc = thời gian 1 chirp = idle + ramp
        self.Tc = self.idle_time + (self.B / self.S)
        # PRF = Pulse Repetition Frequency = 1/Tc
        # Đây cũng là tần số sampling của slow-time signal
        self.fs_doppler = 1.0 / self.Tc
        # Range resolution (m/bin)
        self.range_resolution = self.c / (2 * self.B)
        # Wavelength
        self.lambda_val = self.c / self.f_c


# =============================================================================
# SECTION 3 — SIGNAL PROCESSING
# =============================================================================

def _int16_to_complex(raw_data: np.ndarray, iq_order: str = "IIQQ") -> np.ndarray:
    """
    Chuyển mảng int16 thô (đã nằm trong RAM) sang complex signal.

    Mỗi nhóm 4 mẫu int16 ứng với 2 RX × (I, Q). Thứ tự byte phụ thuộc nguồn dữ
    liệu, nên cùng một tín hiệu vật lý có thể được lưu/truyền theo 2 cách:
        - "IIQQ": [I_rx0, I_rx1, Q_rx0, Q_rx1]  (quy ước dữ liệu train model)
        - "QQII": [Q_rx0, Q_rx1, I_rx0, I_rx1]  (luồng nhận realtime từ DCA1000)
    Mỗi chế độ giải mã đúng theo thứ tự nguồn của nó sẽ phục hồi *cùng* một tín
    hiệu phức I+jQ. Mặc định IIQQ để khớp với pipeline đã train; luồng realtime
    truyền iq_order="QQII".

    Parameters
    ----------
    raw_data : ndarray int16, shape (N*4,)
    iq_order : "IIQQ" (mặc định, quy ước train) hoặc "QQII" (realtime DCA1000)

    Returns
    -------
    complex_data : ndarray, dtype=complex64, shape (N,)
    """
    data = np.asarray(raw_data, dtype=np.int16).reshape(-1, 4)
    if iq_order.upper() == "IIQQ":
        raw_I = data[:, [0, 1]].flatten()
        raw_Q = data[:, [2, 3]].flatten()
    elif iq_order.upper() == "QQII":
        raw_I = data[:, [2, 3]].flatten()
        raw_Q = data[:, [0, 1]].flatten()
    else:
        raise ValueError(f"iq_order phải là 'QQII' hoặc 'IIQQ', nhận: {iq_order!r}")
    complex_data = np.empty(raw_I.shape, dtype=np.complex64)
    complex_data.real = raw_I
    complex_data.imag = raw_Q
    return complex_data


def _raw_to_complex(path: str, iq_order: str = "IIQQ") -> np.ndarray:
    """
    Đọc file .raw/.bin (int16) rồi chuyển sang complex signal.

    Wrapper quanh _int16_to_complex để phục vụ luồng offline (đọc từ đĩa).

    Parameters
    ----------
    path     : str — đường dẫn file .raw hoặc .bin
    iq_order : "IIQQ" (mặc định, quy ước train) hoặc "QQII"

    Returns
    -------
    complex_data : ndarray, dtype=complex64, shape (N,)
    """
    return _int16_to_complex(np.fromfile(path, dtype=np.int16), iq_order=iq_order)


def _process_to_cube(complex_data: np.ndarray, cfg: RadarConfig) -> np.ndarray:
    """
    Reshape complex signal 1D → radar data cube 4D.

    Thứ tự chiều output: [Frame, Antenna, Loop, Sample]

    Giải thích reshape:
        - Dữ liệu gốc: [Frame × Chirp × RX × Sample]
        - Trong AWR1843, TX xen kẽ với Loop (MIMO mode), nhưng ở đây
          num_tx=1 nên num_chirps = num_loops.
        - Sau transpose: trục Antenna được đưa lên trước Loop để
          dễ truy cập theo anten.

    Returns
    -------
    cube : ndarray, shape [n_frames, num_tx*num_rx, num_loops, num_adc_samples]
    """
    num_tx    = cfg.num_tx
    num_rx    = cfg.num_rx
    num_adc   = cfg.num_adc_samples
    num_loops = cfg.num_loops_per_frame
    num_chirps = num_tx * num_loops

    # Bước 1: reshape đơn giản nhất [Frame, Chirp, RX, Sample]
    cube = complex_data.reshape(-1, num_chirps, num_rx, num_adc)

    # Bước 2: tách TX ra khỏi Chirp → [Frame, Loop, TX, RX, Sample]
    cube = cube.reshape(-1, num_loops, num_tx, num_rx, num_adc)

    # Bước 3: đưa TX × RX lên trước Loop → [Frame, TX, RX, Loop, Sample]
    cube = cube.transpose(0, 2, 3, 1, 4)

    # Bước 4: gộp TX và RX → [Frame, Antenna, Loop, Sample]
    cube = cube.reshape(cube.shape[0], num_tx * num_rx, num_loops, num_adc)

    return cube


def _static_clutter_removal(radar_cube: np.ndarray) -> np.ndarray:
    """
    Loại bỏ clutter tĩnh bằng mean subtraction theo chiều slow-time.

    Nguyên lý: vật đứng yên → tín hiệu không thay đổi theo chirp
               → mean theo chiều Loop = tín hiệu vật tĩnh
               → trừ đi mean → chỉ còn tín hiệu từ vật chuyển động

    Parameters
    ----------
    radar_cube : ndarray [n_frames, n_ant, n_loops, n_samples]

    Returns
    -------
    ndarray cùng shape (không thay đổi inplace)
    """
    # Mean theo chiều n_loops (axis=2), giữ nguyên shape để broadcast
    mean_clutter = np.mean(radar_cube, axis=2, keepdims=True)
    return radar_cube - mean_clutter


def _compute_range_fft(radar_cube: np.ndarray, cfg: RadarConfig) -> np.ndarray:
    """
    Áp dụng Hanning window + FFT theo chiều range, giữ n_range_bins_keep bins.

    Tại sao Hanning window?
        FFT thuần tuý giả định tín hiệu tuần hoàn → gây spectral leakage.
        Hanning window làm mượt 2 đầu mỗi chirp → giảm leakage.

    Tại sao chỉ giữ 32 bins đầu?
        Range FFT cho ra 128 bins (=num_adc_samples), nhưng côn trùng
        thường ở gần (0.5–2m) → chỉ cần bins đầu. Giảm memory đáng kể.

    Returns
    -------
    range_fft : ndarray [n_frames, n_ant, n_loops, n_range_bins_keep], complex
    """
    hanning_win = np.hanning(cfg.num_adc_samples)
    # Broadcast window vào chiều cuối (sample dimension)
    windowed = radar_cube * hanning_win
    rfft = np.fft.fft(windowed, axis=3)
    return rfft[..., :cfg.n_range_bins_keep]


def _track_peaks(range_fft: np.ndarray,
                 range_bin_min: int,
                 range_bin_max: int,
                 kernel_size: int = 5) -> tuple:
    """
    Theo dõi range bin chứa côn trùng theo từng frame.

    Thuật toán 3 bước:
        1. Tính magnitude map = mean(|range_fft|) qua anten và chirps
           → shape [n_frames, n_range_bins]
        2. Tìm argmax trong vùng [range_bin_min, range_bin_max)
           → raw_peaks: bin có amplitude lớn nhất mỗi frame
        3. Smooth bằng median filter kích thước kernel_size
           → loại bỏ peak nhảy đột ngột do nhiễu nhất thời

    Parameters
    ----------
    range_fft     : ndarray [n_frames, n_ant, n_loops, n_bins]
    range_bin_min : int
    range_bin_max : int
    kernel_size   : int — phải là số lẻ (3, 5, 7, ...)

    Returns
    -------
    smooth_peaks  : ndarray [n_frames], int
    rtm_db        : ndarray [n_frames, n_bins], float — magnitude dB cho plot
    rtm_vmin      : float — giá trị min colorbar (percentile 5)
    rtm_vmax      : float — giá trị max colorbar (percentile 99)
    """
    # Tính magnitude map [n_frames, n_range_bins]
    magnitude_map = np.mean(np.abs(range_fft), axis=(1, 2))

    # Giới hạn vùng tìm kiếm
    magnitude_roi = magnitude_map[:, range_bin_min:range_bin_max]

    # Tìm peak trong ROI, cộng lại offset để ra chỉ số bin thật
    raw_peaks = np.argmax(magnitude_roi, axis=1) + range_bin_min

    # Đảm bảo kernel_size là số lẻ
    if kernel_size % 2 == 0:
        kernel_size += 1

    # Median filter smooth
    smooth_peaks = medfilt(raw_peaks.astype(float), kernel_size=kernel_size)
    smooth_peaks = np.round(smooth_peaks).astype(int)

    # Chuẩn bị dữ liệu cho Range-Time Map
    rtm_db   = 20 * np.log10(magnitude_map + 1e-6)
    rtm_vmin = float(np.percentile(rtm_db, 5))
    rtm_vmax = float(np.percentile(rtm_db, 99))

    return smooth_peaks, rtm_db, rtm_vmin, rtm_vmax


def _extract_slow_time_signals(range_fft: np.ndarray,
                                smooth_peaks: np.ndarray,
                                cfg: RadarConfig) -> list:
    """
    Trích xuất slow-time signal tại 3 range bins: center-1, center, center+1.

    Lý do dùng 3 bins: côn trùng chuyển động nhẹ có thể "tràn" sang bin
    liền kề. Cộng năng lượng từ 3 bins giúp bắt đủ tín hiệu.

    Output: 3 groups × 4 anten = 12 slow-time signals.
    Mỗi signal có shape: (n_frames × n_loops,) = chuỗi thời gian liên tục.

    Parameters
    ----------
    range_fft    : ndarray [n_frames, n_ant, n_loops, n_bins]
    smooth_peaks : ndarray [n_frames], int
    cfg          : RadarConfig

    Returns
    -------
    signals : list of 3 elements, mỗi element là list of 4 complex arrays
              signals[0] = lo  (center-1)
              signals[1] = center
              signals[2] = hi  (center+1)
    """
    n_frames  = range_fft.shape[0]
    n_bins    = range_fft.shape[3]
    frame_idx = np.arange(n_frames)

    # Clip để không ra khỏi mảng
    peaks_lo = np.clip(smooth_peaks - 1, 0, n_bins - 1)
    peaks_hi = np.clip(smooth_peaks + 1, 0, n_bins - 1)

    # Fancy indexing: lấy đúng bin tương ứng với từng frame
    # Shape mỗi extracted: (n_frames, n_ant, n_loops)
    ext_center = range_fft[frame_idx, :, :, smooth_peaks]
    ext_lo     = range_fft[frame_idx, :, :, peaks_lo]
    ext_hi     = range_fft[frame_idx, :, :, peaks_hi]

    # Ghép n_frames × n_loops thành chuỗi 1D cho mỗi anten
    def _flatten_by_ant(extracted):
        return [extracted[:, ant, :].reshape(-1) for ant in range(cfg.num_rx)]

    return [_flatten_by_ant(ext_lo),
            _flatten_by_ant(ext_center),
            _flatten_by_ant(ext_hi)]


def _highpass_complex(x: np.ndarray, fs: float,
                      cutoff_hz: float = 100.0,
                      order: int = 4) -> np.ndarray:
    """
    Lọc high-pass Butterworth cho tín hiệu complex slow-time.

    Tại sao cần high-pass?
        Sau static clutter removal, vẫn còn clutter "quasi-static" (vật di chuyển
        chậm: dây điện rung, lá cây, v.v.) tạo năng lượng ở tần số thấp (<100Hz).
        High-pass loại bỏ phần này, giữ lại tín hiệu đập cánh (>100Hz).

    Dùng filtfilt (zero-phase) để không gây lệch pha — quan trọng cho STFT.

    Parameters
    ----------
    x          : complex ndarray (N,)
    fs         : tần số sampling (Hz) = PRF
    cutoff_hz  : tần số cắt
    order      : bậc lọc

    Returns
    -------
    ndarray complex, cùng shape với x
    """
    nyq = fs / 2.0
    if cutoff_hz <= 0:
        return x
    if cutoff_hz >= nyq:
        raise ValueError(
            f"cutoff_hz={cutoff_hz:.1f} Hz phải nhỏ hơn Nyquist={nyq:.1f} Hz")

    b, a = butter(order, cutoff_hz / nyq, btype='highpass')

    # Xử lý riêng phần real và imag, sau đó ghép lại
    x_real = filtfilt(b, a, np.real(x))
    x_imag = filtfilt(b, a, np.imag(x))
    return x_real + 1j * x_imag


def _apply_highpass_all(signal_groups: list, cfg: RadarConfig) -> list:
    """Áp dụng high-pass filter cho tất cả 12 signals (3 groups × 4 anten)."""
    return [
        [_highpass_complex(s,
                           fs=cfg.fs_doppler,
                           cutoff_hz=cfg.highpass_cutoff_hz,
                           order=cfg.highpass_order)
         for s in group]
        for group in signal_groups
    ]


# =============================================================================
# SECTION 4 — SPECTROGRAM
# =============================================================================

def _compute_combined_spectrogram(signal_groups: list,
                                   cfg: RadarConfig,
                                   frame_start: int = 0,
                                   frame_end: Optional[int] = None) -> tuple:
    """
    Tính spectrogram tổng hợp từ 3 bins × 4 anten = 12 spectrograms.

    Tại sao cộng (power summing)?
        Mỗi anten độc lập đo cùng côn trùng nhưng có phase khác nhau.
        Cộng power (|S|²) thay vì cộng complex → tránh triệt tiêu nhau,
        tăng SNR lên ~√12 lần so với dùng 1 anten.

    Spectrogram dùng return_onesided=False (two-sided) + fftshift để
    giữ cả tần số dương và âm (phản ánh chuyển động lại/tiến).

    Parameters
    ----------
    signal_groups : list — output của _apply_highpass_all()
    cfg           : RadarConfig
    frame_start   : int — frame bắt đầu (0-indexed)
    frame_end     : int — frame kết thúc (inclusive)

    Returns
    -------
    f_axis  : ndarray [n_freq] — trục tần số sau fftshift (Hz), âm đến dương
    t_axis  : ndarray [n_time] — trục thời gian (s)
    Sxx_sum : ndarray [n_freq, n_time] — power sum (linear scale)
    Sxx_db  : ndarray [n_freq, n_time] — power sum (dB)
    """
    num_chirps = cfg.num_loops_per_frame

    if frame_end is None:
        frame_end = (len(signal_groups[0][0]) // num_chirps) - 1

    sample_start = frame_start * num_chirps
    sample_end   = (frame_end + 1) * num_chirps

    Sxx_sum      = None
    f_axis_final = None
    t_axis_final = None

    for group in signal_groups:
        for sig in group:
            sig_slice = sig[sample_start:sample_end]

            f_ax, t_ax, Sxx = scipy_signal.spectrogram(
                sig_slice,
                fs=cfg.fs_doppler,
                window=cfg.stft_window,
                nperseg=cfg.nperseg,
                noverlap=cfg.noverlap,
                nfft=cfg.nfft,
                detrend=False,
                return_onesided=False,   # giữ cả tần số âm
                scaling='density',
                mode='psd'
            )
            # fftshift: đưa tần số 0 ra giữa (âm bên trái, dương bên phải)
            Sxx = np.fft.fftshift(Sxx, axes=0)

            if Sxx_sum is None:
                Sxx_sum      = Sxx.copy()
                f_axis_final = np.fft.fftshift(f_ax)
                t_axis_final = t_ax
            else:
                Sxx_sum += Sxx

    # Chuyển sang dB, dùng P_ref_global để normalize
    Sxx_db = 10 * np.log10(Sxx_sum / cfg.P_ref_global + 1e-12)

    return f_axis_final, t_axis_final, Sxx_sum, Sxx_db


# =============================================================================
# SECTION 5 — FEATURE EXTRACTION
# =============================================================================

def _trimmed_mean(arr, p=0.10):
    """Bỏ p*100% ở mỗi đầu trước khi tính mean. p=0.10 → giữ 80% trung tâm."""
    return float(scipy_trim_mean(arr, p))


def _stats(arr, trimmed=False, p=0.10):
    """
    Tính 3 thống kê mô tả cho 1 chuỗi thời gian.

    trimmed=True → thay mean bằng trimmed_mean.
    Dùng cho các feature dễ bị outlier kéo lệch (centroid, bandwidth, ridge).
    """
    if trimmed:
        return {
            "mean": _trimmed_mean(arr, p),
            "median"      : float(np.median(arr)),
            "std"         : float(np.std(arr)),
        }
    return {
        "mean"  : float(np.mean(arr)),
        "median": float(np.median(arr)),
        "std"   : float(np.std(arr)),
    }


def _fold_symmetric(f_axis, Sxx_sum, fmin, fmax):
    """
    Gom Sxx(+f) + Sxx(-f) → Sxx_abs(|f|) cho |f| ∈ [fmin, fmax].

    Tại sao fold?
        Tín hiệu radar côn trùng đập cánh tạo cả tần số dương lẫn âm
        (vì cánh đập 2 chiều). Fold cộng năng lượng 2 phía → tăng SNR
        và đơn giản hóa phân tích (chỉ còn |f|).

    Dùng np.add.at để vectorize an toàn (không có race condition với
    các bin bị map vào cùng 1 vị trí).

    Returns
    -------
    f_pos   : ndarray [M] — tần số dương unique
    Sxx_pos : ndarray [M, T] — power đã fold
    """
    abs_f       = np.abs(f_axis)
    abs_f_round = np.round(abs_f, decimals=6)
    mask        = (abs_f_round >= fmin) & (abs_f_round <= fmax)

    f_all  = abs_f_round[mask]
    S_all  = Sxx_sum[mask, :]

    f_pos   = np.unique(f_all)
    bin_idx = np.clip(np.searchsorted(f_pos, f_all), 0, len(f_pos) - 1)

    Sxx_pos = np.zeros((len(f_pos), Sxx_sum.shape[1]), dtype=Sxx_sum.dtype)
    np.add.at(Sxx_pos, bin_idx, S_all)

    return f_pos, Sxx_pos


def _spectral_entropy(power, axis=0, eps=1e-12):
    """
    H_norm = -Σ(p·log₂p) / log₂(N) ∈ [0, 1]
    Gần 1 = noise-like (phổ phẳng đều)
    Gần 0 = tonal (năng lượng tập trung vào vài tần số)
    Côn trùng đập cánh → tonal → entropy thấp
    """
    P = power / (np.sum(power, axis=axis, keepdims=True) + eps)
    H = -np.sum(P * np.log2(P + eps), axis=axis)
    return H / np.log2(power.shape[axis])


def _spectral_flatness(power, axis=0, eps=1e-12):
    geo   = np.exp(np.mean(np.log(power + eps), axis=axis))
    arith = np.mean(power, axis=axis) + eps
    return geo / arith


def _build_mel_filterbank(freq_axis, n_filters=16, fmin=50, fmax=800, eps=1e-12):
    """
    Xây dựng Mel filterbank với n_filters bộ lọc tam giác.

    Thang Mel: Hz → Mel = 2595·log₁₀(1 + f/700)
    Khoảng cách đều nhau trên thang Mel ≈ khoảng cách log-uniform trên Hz.
    Phù hợp với cách côn trùng sử dụng các harmonic của WBF.
    """
    mel_min = 2595 * np.log10(1 + fmin / 700)
    mel_max = 2595 * np.log10(1 + fmax / 700)
    centers = 700 * (10 ** (np.linspace(mel_min, mel_max, n_filters + 2) / 2595) - 1)
    f       = freq_axis[np.newaxis, :]
    left, center, right = centers[:-2, None], centers[1:-1, None], centers[2:, None]
    return np.maximum(0.0, np.minimum(
        (f - left)   / (center - left  + eps),
        (right - f)  / (right - center + eps)
    ))


def _compute_delta(feature_matrix, N=2):
    """
    HTK delta với edge-padding.
    Δ[t] = Σ n·(c[t+n] - c[t-n]) / (2·Σ n²)   cho n=1..N
    """
    padded = np.pad(feature_matrix, ((0, 0), (N, N)), mode='edge')
    denom  = 2.0 * np.sum(np.arange(1, N + 1) ** 2)
    T      = feature_matrix.shape[1]
    num    = sum(n * (padded[:, N+n: N+n+T] - padded[:, N-n: N-n+T])
                 for n in range(1, N + 1))
    return num / denom


def _compute_mfcc(f_axis, Sxx_sum, fmin=50., fmax=800.,
                   n_filters=16, n_mfcc=8, eps=1e-12):
    """
    Tính MFCC từ spectrogram:
        1. Fold symmetric (tần số âm về dương)
        2. Nhân với Mel filterbank → log Mel energy
        3. DCT type-II → MFCC coefficients
        4. Tính delta và delta-delta
    """
    f_pos, Sxx_abs = _fold_symmetric(f_axis, Sxx_sum, fmin, fmax)
    fb = _build_mel_filterbank(f_pos, n_filters, fmin, fmax)
    fb = fb / (np.sum(fb, axis=1, keepdims=True) + eps)   # normalize

    log_mel     = np.log(fb @ Sxx_abs + eps)              # (n_filters, T)
    mfcc_matrix = dct(log_mel.T, type=2, norm='ortho')[:, :n_mfcc].T  # (n_mfcc, T)
    delta       = _compute_delta(mfcc_matrix, N=2)
    delta2      = _compute_delta(delta, N=2)
    return mfcc_matrix, delta, delta2


def _add_mfcc_to_dict(features, f_axis, Sxx_sum,
                       fmin=50., fmax=800., n_filters=16, n_mfcc=8,
                       use_delta=False, use_delta2=False, eps=1e-12):
    """
    Thêm MFCC features vào dict.
    8 coefficients × 3 stats (mean, std, median) = 24 features.
    """
    mfcc, delta, delta2 = _compute_mfcc(f_axis, Sxx_sum, fmin, fmax,
                                         n_filters, n_mfcc, eps)
    matrices = [("mfcc", mfcc)]
    if use_delta:  matrices.append(("delta", delta))
    if use_delta2: matrices.append(("delta2", delta2))

    for prefix, mat in matrices:
        for i in range(n_mfcc):
            c = mat[i, :]
            features[f"{prefix}_{i:02d}_mean"]   = float(np.mean(c))
            features[f"{prefix}_{i:02d}_std"]    = float(np.std(c))
            features[f"{prefix}_{i:02d}_median"] = float(np.median(c))
    return features


def _safe_divide(a, b, eps=1e-12):
    return a / (b + eps)


def _extract_features(f_axis, Sxx_sum,
                       fmin=50, fmax=800,
                       band_edges=None, eps=1e-12,
                       use_mfcc=True,
                       mfcc_n_filters=16, mfcc_n_coeffs=8,
                       mfcc_use_delta=False, mfcc_use_delta2=False) -> dict:
    """
    Trích xuất toàn bộ features từ spectrogram (không có label/metadata).

    Nhóm features:
        - absBandRatio (11)       : % năng lượng mỗi băng (bất biến khoảng cách)
        - bandRatio + ratio (4)   : so sánh vùng bee (150-350) vs vùng cao
        - spectral_entropy (3)    : mức độ "tonal"
        - spectral_flatness (3)   : đo tonal bằng geo/arith mean
        - spectral_centroid (3)   : tần số đặc trưng trung bình
        - spectral_bandwidth (5)  : độ rộng phổ quanh centroid
        - ridge_freq (5)          : tần số nổi bật nhất mỗi frame
        - [mfcc] (24)             : hình dạng phổ theo thang Mel

    Returns
    -------
    features : dict — tất cả features (không bao gồm label/metadata)
    """
    if band_edges is None:
        band_edges = [50, 100, 150, 200, 250, 300, 350, 400, 450, 500, 600, 800]

    features = {}

    # Fold symmetric: cộng tần số âm và dương
    f_pos, P_main = _fold_symmetric(f_axis, Sxx_sum, fmin, fmax)
    abs_f_main = f_pos
    total_power   = float(np.sum(P_main))

    # ── Band Power ────────────────────────────────────────────────────────────
    for lo, hi in zip(band_edges[:-1], band_edges[1:]):
        bm = (f_pos >= lo) & (f_pos < hi)
        bp = float(np.sum(P_main[bm, :]))
        features[f"absBandRatio_{lo}_{hi}"] = _safe_divide(bp, total_power, eps)

    features["bandRatio_150_350"] = sum(
        features[f"absBandRatio_{lo}_{hi}"]
        for lo, hi in [(150,200),(200,250),(250,300),(300,350)])
    features["bandRatio_200_300"] = sum(
        features[f"absBandRatio_{lo}_{hi}"]
        for lo, hi in [(200,250),(250,300)])
    features["bandRatio_350_800"] = sum(
        features[f"absBandRatio_{lo}_{hi}"]
        for lo, hi in [(350,400),(400,450),(450,500),(500,600),(600,800)])
    features["lowHighRatio_150_350_to_350_800"] = _safe_divide(
        features["bandRatio_150_350"], features["bandRatio_350_800"], eps)

    # ── Spectral Shape ────────────────────────────────────────────────────────
    P_norm = P_main / (np.sum(P_main, axis=0, keepdims=True) + eps)

# Entropy — plain stats (compact distribution, low outlier risk)
    ent = _spectral_entropy(P_main, axis=0, eps=eps)
    for k, v in {**{"spectral_entropy_" + s: val
                    for s, val in _stats(ent).items()}}.items():
        features[k] = v

    # Flatness — plain stats
    flat = _spectral_flatness(P_main, axis=0, eps=eps)
    for k, v in {"spectral_flatness_" + s: val
                 for s, val in _stats(flat).items()}.items():
        features[k] = v

    # Centroid — TRIMMED (outlier: mất tín hiệu → centroid nhảy về ~50Hz)
    centroid = np.sum(abs_f_main[:, None] * P_norm, axis=0)
    for k, v in {"spectral_centroid_" + s: val
                 for s, val in _stats(centroid, trimmed=True).items()}.items():
        features[k] = v
    features["spectral_centroid_std"]           = float(np.std(centroid))

    # Bandwidth — TRIMMED (outlier: mất tín hiệu → bandwidth = 0)
    bandwidth = np.sqrt(np.sum(
        ((abs_f_main[:, None] - centroid[None, :]) ** 2) * P_norm, axis=0))
    for k, v in {"spectral_bandwidth_" + s: val
                 for s, val in _stats(bandwidth, trimmed=True).items()}.items():
        features[k] = v
    features["spectral_bandwidth_std"] = float(np.std(bandwidth))
    features["spectral_bandwidth_max"] = float(np.max(bandwidth))
    features["spectral_bandwidth_p95"] = float(np.percentile(bandwidth, 95))

    # Ridge freq — TRIMMED (outlier: mất tín hiệu → ridge = 50Hz; noise → ridge nhảy cao)
    ridge_freq = abs_f_main[np.argmax(P_main, axis=0)]
    for k, v in {"ridge_freq_" + s: val
                 for s, val in _stats(ridge_freq, trimmed=True).items()}.items():
        features[k] = v
    features["ridge_freq_std"]           = float(np.std(ridge_freq))
    features["ridge_freq_range"]         = float(np.ptp(ridge_freq))
    features["ridge_freq_mean_abs_diff"] = float(
        np.mean(np.abs(np.diff(ridge_freq))) if len(ridge_freq) > 1 else 0.)

    # ── MFCC ─────────────────────────────────────────────────────────────────
    if use_mfcc:
        features = _add_mfcc_to_dict(
            features, f_axis, Sxx_sum, fmin, fmax,
            mfcc_n_filters, mfcc_n_coeffs,
            mfcc_use_delta, mfcc_use_delta2, eps)

    return features


# =============================================================================
# SECTION 6 — InsectRadarProcessor (Main Class)
# =============================================================================

class InsectRadarProcessor:
    """
    Framework xử lý tín hiệu radar AWR1843 để phát hiện và phân loại côn trùng.

    CÁCH DÙNG CƠ BẢN:
    ------------------
        from insect_radar_processor import InsectRadarProcessor

        processor = InsectRadarProcessor(
            range_bin_min=15,
            range_bin_max=20
        )
        result = processor.process("path/to/file.raw")

        if result['is_insect']:
            features = result['features']  # dict → đưa vào model
        else:
            print(result['reason'])        # tại sao bị loại

    Parameters
    ----------
    cfg                : RadarConfig — thông số radar (None → dùng default AWR1843)
    power_threshold    : float       — ngưỡng power_threshold (default 45000)
    range_bin_min      : int         — bin bắt đầu vùng tìm peak (default 15)
    range_bin_max      : int         — bin kết thúc vùng tìm peak (default 20)
    use_mfcc           : bool        — bật 24 MFCC features (default True)
    """

    def __init__(
        self,
        cfg: RadarConfig = None,
        power_threshold: float = 45000,
        range_bin_min: int = 15,
        range_bin_max: int = 20,
        use_mfcc: bool = True,
        iq_order: str = "IIQQ",
    ):
        self.cfg                = cfg if cfg is not None else RadarConfig()
        self.power_threshold    = power_threshold
        self.range_bin_min      = range_bin_min
        self.range_bin_max      = range_bin_max
        self.use_mfcc           = use_mfcc
        self.iq_order           = iq_order

        # In thông số khi khởi tạo để dễ kiểm tra
        c = self.cfg
        print(f"[InsectRadarProcessor] Initialized")
        print(f"  PRF          : {c.fs_doppler:.2f} Hz")
        print(f"  Range res    : {c.range_resolution*100:.2f} cm/bin")
        print(f"  Range window : bin {range_bin_min} → {range_bin_max}")
        print(f"  power thr : {power_threshold}")
        print(f"  IQ order     : {iq_order}")
        print(f"  MFCC         : {use_mfcc}")

    def process(self, raw_path: str) -> dict:
        """
        Chạy toàn bộ pipeline xử lý cho 1 file .raw (30 frames).

        Parameters
        ----------
        raw_path : str — đường dẫn tới file .raw

        Returns
        -------
        result : dict
            Luôn có:
                'is_insect'              : bool
                'power_threshold'        : float

            Nếu is_insect = False, thêm:
                'reason'                 : str   (giải thích tại sao bị loại)
                'features'               : None

            Nếu is_insect = True, thêm:
                'features'               : dict  (sẵn sàng đưa vào model)

            Luôn có (dù insect hay background):
                'viz': {
                    'rtm_db'       : ndarray [n_frames, n_range_bins]
                    'rtm_vmin'     : float
                    'rtm_vmax'     : float
                    'smooth_peaks' : ndarray [n_frames]
                    'range_bin_min': int
                    'range_bin_max': int
                    'f_axis'       : ndarray [n_freq]
                    't_axis'       : ndarray [n_time]
                    'Sxx_db'       : ndarray [n_freq, n_time]
                }
        """
        return self.process_array(np.fromfile(raw_path, dtype=np.int16))

    def process_array(self, raw_int16: np.ndarray) -> dict:
        """
        Chạy pipeline cho mảng int16 thô đã có sẵn trong RAM (không qua đĩa).

        Tương đương process() nhưng nhận thẳng numpy int16 từ luồng realtime,
        tránh round-trip ghi/đọc file tạm. Dùng trong realTimeProc_infer.py.

        Parameters
        ----------
        raw_int16 : ndarray int16 — dữ liệu ADC thô (numframes × 131 072 với 128/128)

        Returns
        -------
        result : dict — giống hệt process()
        """
        cfg = self.cfg

        # ── BƯỚC 1: int16 thô → complex signal ───────────────────────────────
        complex_data = _int16_to_complex(raw_int16, iq_order=self.iq_order)

        # Tính số frame thực tế (phòng trường hợp dữ liệu hơi thừa/thiếu mẫu)
        n_samples_per_frame = (cfg.num_tx * cfg.num_rx *
                               cfg.num_adc_samples * cfg.num_loops_per_frame)
        n_frames = len(complex_data) // n_samples_per_frame

        # Cắt đúng bội số, bỏ phần dư (nếu có)
        complex_data = complex_data[:n_frames * n_samples_per_frame]

        # ── BƯỚC 2: Reshape → radar cube [Frame, Ant, Loop, Sample] ──────────
        cube = _process_to_cube(complex_data, cfg)
        del complex_data   # giải phóng memory

        # ── BƯỚC 3: Static clutter removal ───────────────────────────────────
        cube = _static_clutter_removal(cube)

        # ── BƯỚC 4: Hanning window + Range FFT ───────────────────────────────
        range_fft = _compute_range_fft(cube, cfg)
        del cube

        # ── BƯỚC 5: Range-Time Map + Peak tracking ───────────────────────────
        smooth_peaks, rtm_db, rtm_vmin, rtm_vmax = _track_peaks(
            range_fft,
            range_bin_min=self.range_bin_min,
            range_bin_max=self.range_bin_max,
        )

        # ── BƯỚC 6: Extract slow-time signal tại 3 bins ──────────────────────
        signal_groups = _extract_slow_time_signals(range_fft, smooth_peaks, cfg)
        del range_fft

        # ── BƯỚC 7: High-pass filter ──────────────────────────────────────────
        signal_groups = _apply_highpass_all(signal_groups, cfg)

        # ── BƯỚC 8: STFT → spectrogram tổng hợp ─────────────────────────────
        f_axis, t_axis, Sxx_sum, Sxx_db = _compute_combined_spectrogram(
            signal_groups, cfg,
            frame_start=0,
            frame_end=n_frames - 1,
        )

        # ── BƯỚC 9: Tính total_power_50_800hz để kiểm tra ngưỡng ───────────
        # trích xuất features → đảm bảo nhất quán 100% với CSV training
        f_pos_check, P_check = _fold_symmetric(f_axis, Sxx_sum, 50.0, 800.0)
        total_power = float(np.sum(P_check))
        power_threshold = total_power

        # Chuẩn bị viz dict (trả về dù là insect hay background)
        viz = {
            'rtm_db'       : rtm_db,
            'rtm_vmin'     : rtm_vmin,
            'rtm_vmax'     : rtm_vmax,
            'smooth_peaks' : smooth_peaks,
            'range_bin_min': self.range_bin_min,
            'range_bin_max': self.range_bin_max,
            'f_axis'       : f_axis,
            't_axis'       : t_axis,
            'Sxx_db'       : Sxx_db,
        }

        # ── BƯỚC 10: Kiểm tra ngưỡng power_threshold ──────────────────
        if power_threshold < self.power_threshold:
            return {
                'is_insect'              : False,
                'power_threshold'        : power_threshold,
                'reason'                 : (
                    f"power_threshold = {power_threshold:.4f} "
                    f"< threshold = {self.power_threshold} → background"
                ),
                'features'               : None,
                'viz'                    : viz,
            }

        # ── BƯỚC 11: Trích xuất features ─────────────────────────────────────
        features = _extract_features(
            f_axis, Sxx_sum,
            fmin=50, fmax=800,
            use_mfcc=self.use_mfcc,
        )

        return {
            'is_insect'              : True,
            'power_threshold'        : power_threshold,
            'features'               : features,
            'viz'                    : viz,
        }

    def get_feature_names(self) -> list:
        """
        Trả về danh sách tên features theo đúng thứ tự.
        Dùng để tạo DataFrame từ features dict.

        Returns
        -------
        list of str
        """
        names = []
        band_edges = [50, 100, 150, 200, 250, 300, 350, 400, 450, 500, 600, 800]

        for lo, hi in zip(band_edges[:-1], band_edges[1:]):
            names.append(f'absBandRatio_{lo}_{hi}')
 
        names += ['bandRatio_150_350', 'bandRatio_200_300',
                  'bandRatio_350_800', 'lowHighRatio_150_350_to_350_800']
 
        for prefix in ['spectral_entropy', 'spectral_flatness']:
            for s in ['mean', 'median', 'std']:
                names.append(f'{prefix}_{s}')
 
        # spectral_centroid (3)
        for s in ['mean', 'median', 'std']:
            names.append(f'spectral_centroid_{s}')
 
        # spectral_bandwidth (5): std trước, max/p95 sau
        for s in ['mean', 'median', 'std']:
            names.append(f'spectral_bandwidth_{s}')
        names += ['spectral_bandwidth_max', 'spectral_bandwidth_p95']
 
        # ridge_freq (5): std trước, range/mean_abs_diff sau
        for s in ['mean', 'median', 'std']:
            names.append(f'ridge_freq_{s}')
        names += ['ridge_freq_range', 'ridge_freq_mean_abs_diff']
 
        if self.use_mfcc:
            for i in range(8):
                for s in ['mean', 'std', 'median']:
                    names.append(f'mfcc_{i:02d}_{s}')
                    
        return names
