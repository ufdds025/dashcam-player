"""視覺測速核心演算法

Multi-strip + Harmonic + Consensus 實現
"""

from __future__ import annotations

# mypy: disable-error-code="var-annotated,operator,arg-type,assignment"
import logging
from dataclasses import dataclass

import cv2
import numpy as np

from dashcam_player.utils.log import get_logger

log = get_logger()
log.setLevel(logging.INFO)


# 速度常數
V_MIN_MPS = 5.0 / 3.6  # ≈1.39 m/s (5 km/h)
V_MAX_MPS = 200.0 / 3.6  # ≈55.56 m/s (200 km/h)
MIN_SCORE = 0.60
MIN_MARGIN = 0.10
MIN_CONSENSUS = 3  # 4 條中至少 3 條一致
PERIOD_TOLERANCE = 0.10  # 10% 相對誤差

# 台灣高速公路局規範：白實線 4m、間隔 6m、一組黑白 10m
MARKING_WHITE_LENGTH_M = 4.0
MARKING_GAP_LENGTH_M = 6.0
MARKING_CYCLE_LENGTH_M = 10.0  # 一黑一白 = 一白一黑 = 10m


@dataclass
class VisionPoint:
    """單一視覺速度估計點"""
    time: float
    raw_speed: float | None
    confidence: float
    period_hypothesis: str
    strip_votes: str
    reject_reason: str


def video_suitability_check(frame: np.ndarray) -> str:
    """
    影片適合度檢查
    Returns: "SUPPORTED" / "LIMITED" / "UNSUITABLE"
    """
    # 亮度/對比/邊緣強度簡易統計
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    brightness = float(np.mean(gray))
    contrast = float(np.std(gray))

    # Laplacian 方差測銳度
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    sharpness = float(lap.var())

    # 簡單閾值
    if brightness < 30 or brightness > 220:
        return "UNSUITABLE"
    if contrast < 15 or sharpness < 50:
        return "LIMITED"
    return "SUPPORTED"






def compute_vision_confidence(
    marking_strength: float,
    strip_consensus: float,
    periodicity: float,
    speed_plausibility: float,
    temporal_stability: float,
) -> tuple[float, str]:
    """
    計算綜合信心分數

    Returns: (confidence, level) where level in ["HIGH", "MEDIUM", "LOW"]
    """
    C = (
        0.30 * marking_strength +
        0.25 * strip_consensus +
        0.20 * periodicity +
        0.15 * speed_plausibility +
        0.10 * temporal_stability
    )
    C = max(0.0, min(1.0, C))

    if C >= 0.75:
        level = "HIGH"
    elif C >= 0.45:
        level = "MEDIUM"
    else:
        level = "LOW"
    return C, level


def overlay_detection_debug(
    frame: np.ndarray,
    roi_gray: np.ndarray | None = None,
    periods: list[float] | None = None,
    speed_kmh: float | None = None,
    confidence: float = 0.0,
    roi: tuple[float, float, float, float] | None = None,
    **kwargs: object,
) -> np.ndarray:
    """在畫面上疊加視覺測速偵測除錯資訊

    綠框 = 車道標線 ROI（車體上緣之上的近場地面，0.45-0.64 非全畫面）
    青框 = 時間域薄帶 0.55-0.62（標線週期實際取樣位置，10m 週期法）
    右上角文字 = 速度/信心度

    中文標示採用 PIL 繪製（支援 Windows CP950 中文環境，cv2 Hershey 僅 ASCII 會亂碼）。
    任務 1 的標線偵測對象是「靠近車體的地面車道標線（白虛線 10m 週期）」，
    非全畫面所有地面標示（箭頭/停止線不在此範圍）。
    """
    import cv2

    # 延後 import 避免循環
    try:
        from dashcam_player.utils.cv_utils import put_text_cn
    except ImportError:
        put_text_cn = None  # type: ignore[assignment]

    frame = frame.copy()
    h, w = frame.shape[:2]

    # 繪製 ROI 區域（綠框）— 優先使用真實 ROI，否則用預設近場路面 ROI
    if roi is not None:
        rx, ry, rw, rh = roi
        roi_x1 = int(rx * w)
        roi_y1 = int(ry * h)
        roi_x2 = int((rx + rw) * w)
        roi_y2 = int((ry + rh) * h)
    else:
        # 預設與 speed_worker._extract_roi 一致：近場路面 0.45-0.64（車體上緣之上）
        roi_x1 = int(w * 0.05)
        roi_y1 = int(h * 0.45)
        roi_x2 = int(w * 0.95)
        roi_y2 = int(h * 0.64)
    cv2.rectangle(frame, (roi_x1, roi_y1), (roi_x2, roi_y2), (0, 255, 0), 2)
    if put_text_cn is not None:  # type: ignore[truthy-function]
        put_text_cn(frame, "車道標線 ROI", (roi_x1, max(0, roi_y1 - 22)), font_size=16, color=(0, 255, 0), stroke_color=(0, 0, 0))
    else:
        cv2.putText(frame, "ROI", (roi_x1, roi_y1 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
    # 時間域薄帶 0.55-0.62（青色細框）— 標線亮/暗週期實際取樣位置
    thin_y1 = int(h * 0.55)
    thin_y2 = int(h * 0.62)
    cv2.rectangle(frame, (roi_x1, thin_y1), (roi_x2, thin_y2), (255, 255, 0), 1)
    if put_text_cn is not None:  # type: ignore[truthy-function]
        put_text_cn(frame, "取樣薄帶", (roi_x1, max(0, thin_y1 - 18)), font_size=13, color=(255, 255, 0), stroke_color=(0, 0, 0))
    else:
        cv2.putText(frame, "thin", (roi_x1, thin_y1 - 2), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 0), 1)

    # 如果有偵測結果，疊加資訊
    if periods and speed_kmh is not None:
        # 速度資訊（右上角）
        text = f"{speed_kmh:.1f} km/h"
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 0.7
        thickness = 2
        text_size = cv2.getTextSize(text, font, scale, thickness)[0]
        tx = w - text_size[0] - 10
        ty = 30
        # 黑底白字
        cv2.rectangle(frame, (tx - 5, ty - 20), (tx + text_size[0] + 5, ty + 5), (0, 0, 0), -1)
        cv2.putText(frame, text, (tx, ty), font, scale, (0, 255, 0), thickness, cv2.LINE_AA)

        # 信心度（中文環境用 PIL 保證不亂碼）
        conf_text = f"信心度: {confidence:.0%}"
        if put_text_cn is not None:  # type: ignore[truthy-function]
            put_text_cn(frame, conf_text, (tx, ty + 25), font_size=14, color=(200, 200, 200), stroke_color=(0, 0, 0))
            put_text_cn(frame, f"標線數: {len(periods)}", (tx, ty + 45), font_size=14, color=(200, 200, 200))
        else:
            cv2.putText(frame, f"Conf: {confidence:.0%}", (tx, ty + 25), font, 0.5, (200, 200, 200), 1, cv2.LINE_AA)
            cv2.putText(frame, f"Strips: {len(periods)}", (tx, ty + 45), font, 0.5, (200, 200, 200), 1, cv2.LINE_AA)
    else:
        # 無偵測結果 — 中文提示
        if put_text_cn is not None:  # type: ignore[truthy-function]
            put_text_cn(frame, "未偵測到標線", (10, 30), font_size=18, color=(0, 0, 255), stroke_color=(255, 255, 255))
        else:
            cv2.putText(frame, "No markings detected", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2, cv2.LINE_AA)

    return frame


# --- 兼容舊管線的存根（供 speed_worker / tests 引用，實際邏輯已由時間域取代） ---
def _optical_flow_displacement(prev: np.ndarray, curr: np.ndarray, near_ratio: float = 0.6, min_shift: float = 0.2) -> float | None:
    if prev is None or curr is None:
        return None
    if prev.shape != curr.shape:
        return None
    h, w = prev.shape[:2]
    if h < 30 or w < 30:
        return None
    y0 = int(h * (1.0 - near_ratio))
    if y0 >= h:
        return None
    p = prev[y0:, :]
    c = curr[y0:, :]
    max_side = 800.0
    scale = min(1.0, max_side / max(p.shape))
    if scale < 1.0:
        p = cv2.resize(p, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        c = cv2.resize(c, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    try:
        flow = cv2.calcOpticalFlowFarneback(p, c, None, 0.5, 3, 15, 3, 5, 1.2, 0)  # type: ignore[call-overload]
    except cv2.error:
        return None
    vy = flow[..., 1].flatten()
    if len(vy) < 100:
        return None
    min_px = max(50, int(0.03 * len(vy)))
    pos = vy[vy > min_shift]
    neg = vy[vy < -min_shift]
    if len(pos) >= min_px:
        robust = float(np.median(pos))
    elif len(neg) >= min_px:
        robust = float(np.median(neg))
    else:
        return None
    robust /= scale if scale > 0 else 1.0
    if abs(robust) < min_shift:
        return None
    return robust


def _gaussian_smooth(signal: np.ndarray, sigma: float = 2.0) -> np.ndarray:
    kernel_size = int(sigma * 6) | 1
    if kernel_size < 3:
        return signal
    x = np.arange(kernel_size) - kernel_size // 2
    kernel = np.exp(-0.5 * (x / sigma) ** 2)
    kernel /= kernel.sum()
    return np.convolve(signal, kernel, mode="same")


def _find_peaks_robust(signal: np.ndarray, min_distance: int = 10, min_prominence: float = 0.1) -> np.ndarray:
    """簡易峰值檢測（inline 實現，替代已移除的 find_peaks）"""
    peaks: list[int] = []
    for i in range(1, len(signal) - 1):
        if signal[i] > signal[i - 1] and signal[i] > signal[i + 1]:
            if signal[i] > min_prominence:
                if not peaks or i - peaks[-1] >= min_distance:
                    peaks.append(i)
    return np.array(peaks)


def compute_brightness_threshold(roi: np.ndarray) -> float:
    """依訊號分佈直方圖計算二值化門檻（黑↔白雙峰中點）。

    依據：黑約 80~125、白約 240~255（工作單規則 3），
    門檻取兩簇均值中點，不用速度結果回推。
    """
    hist, bin_edges = np.histogram(roi.ravel(), bins=256, range=(0, 256))
    # 找兩個主要峰的位置
    peak_idx = int(np.argmax(hist))
    # 向右找白峰（亮度較高）
    white_region = hist[peak_idx + 1:]
    if len(white_region) > 0:
        white_peak = np.argmax(white_region) + peak_idx + 1
    else:
        white_peak = peak_idx
    # 向左找黑峰（亮度較低）
    black_region = hist[:peak_idx]
    if len(black_region) > 0:
        black_peak = np.argmax(black_region)
    else:
        black_peak = peak_idx
    # 兩峰中點
    thr = (black_peak + white_peak) / 2.0
    return float(thr)


def _linear_interpolate_edge(
    signal: np.ndarray,
    frame_idx: int,
    direction: str,
) -> float:
    """線性內插求邊緣穿越的次幀精度。

    direction: "bw"（黑→白）或 "wb"（白→黑）
    返回浮點幀號（可小數）。
    """
    if frame_idx <= 0 or frame_idx >= len(signal) - 1:
        return float(frame_idx)
    v0 = float(signal[frame_idx])
    v1 = float(signal[frame_idx + 1])
    if direction == "bw":
        # 從低到高：找 v = (v0+v1)/2 的位置
        mid = (v0 + v1) / 2.0
        if v1 == v0:
            return float(frame_idx) + 0.5
        frac = (mid - v0) / (v1 - v0)
    else:
        # 從高到低：同上（對稱）
        mid = (v0 + v1) / 2.0
        if v1 == v0:
            return float(frame_idx) + 0.5
        frac = (v0 - mid) / (v0 - v1)
    frac = max(0.0, min(1.0, frac))
    return float(frame_idx) + frac


def detect_edge_crossings(
    signal: np.ndarray,
    threshold: float | None = None,
) -> tuple[np.ndarray, np.ndarray, float]:
    """偵測時域亮度訊號的黑→白與白→黑邊緣穿越。

    輸入：固定參考帶逐幀亮度值（一維 array）
    輸出：
      bw_frames — 黑→白穿越的幀號（含線性內插）
      wb_frames — 白→黑穿越的幀號（含線性內插）
      threshold — 使用的門檻值
    """
    if threshold is None:
        threshold = compute_brightness_threshold(signal)

    bw_frames: list[float] = []
    wb_frames: list[float] = []

    for i in range(len(signal) - 1):
        v0 = float(signal[i])
        v1 = float(signal[i + 1])
        if v0 < threshold <= v1:
            bw_frames.append(_linear_interpolate_edge(signal, i, "bw"))
        elif v0 > threshold >= v1:
            wb_frames.append(_linear_interpolate_edge(signal, i, "wb"))

    return (
        np.array(bw_frames) if bw_frames else np.array([], dtype=float),
        np.array(wb_frames) if wb_frames else np.array([], dtype=float),
        threshold,
    )


def _validate_duty_cycle(
    bw_frames: np.ndarray,
    wb_frames: np.ndarray,
    fps: float,
    expected_ratio: float = 0.4,
    tolerance: float = 0.15,
) -> float:
    """驗證 duty cycle（白線佔比）是否在合理範圍。

    台灣高速規範：白線 4m / 週期 10m = 40% ± 15%。
    返回 duty cycle 值（0~1），超出範圍表示訊號品質差。
    """
    if len(bw_frames) < 2 or len(wb_frames) < 2:
        return 0.0
    # 配對最近的黑白交界
    pairs = []
    wi = 0
    for b in bw_frames:
        while wi < len(wb_frames) and wb_frames[wi] < b:
            wi += 1
        if wi < len(wb_frames):
            pairs.append(wb_frames[wi] - b)
    if not pairs:
        return 0.0
    # 白線時間 / 週期時間
    white_mean = float(np.median(pairs))
    cycle_mean = white_mean  # 第一個白線段結束到下一個白線段結算 = 一週期
    # 取相鄰同型邊界的間隔作為週期
    if len(bw_frames) >= 3:
        bw_intervals = np.diff(bw_frames) / fps
        cycle_mean = float(np.median(bw_intervals))
    if cycle_mean <= 0:
        return 0.0
    duty = (white_mean / fps) / cycle_mean
    return float(duty)


def estimate_speed_from_edge_crossings(
    bw_frames: np.ndarray,
    wb_frames: np.ndarray,
    fps: float,
    distance_per_cycle: float = MARKING_CYCLE_LENGTH_M,
) -> list[VisionPoint]:
    """從邊緣穿越幀號估算速度。

    雙軌道量測：
      - 1黑1白：B→W 間隔（= 10m 週期）
      - 1白1黑：W→B 間隔（= 10m 週期）
    交叉驗證取中位數，duty cycle 超出範圍則標記為低信心。
    """
    results: list[VisionPoint] = []

    if len(bw_frames) < 3 or fps <= 0:
        return results

    # B→W 週期（1黑1白）
    bw_intervals = np.diff(bw_frames) / fps if len(bw_frames) >= 3 else np.array([])
    # W→B 週期（1白1黑）
    wb_intervals = np.diff(wb_frames) / fps if len(wb_frames) >= 3 else np.array([])

    def _median_T(intervals: np.ndarray) -> float:
        if len(intervals) < 2:
            return 0.0
        med = float(np.median(intervals))
        if len(intervals) >= 5:
            filt = intervals[(intervals > med * 0.4) & (intervals < med * 2.5)]
            if len(filt) >= 3:
                med = float(np.median(filt))
        return med

    bw_median_T = _median_T(bw_intervals)
    wb_median_T = _median_T(wb_intervals)

    # Duty cycle 驗證
    duty = _validate_duty_cycle(bw_frames, wb_frames, fps)
    duty_ok = abs(duty - 0.4) <= 0.15

    # 雙軌道取有效週期
    valid_Ts = []
    if bw_median_T > 0:
        valid_Ts.append(bw_median_T)
    if wb_median_T > 0:
        valid_Ts.append(wb_median_T)

    if not valid_Ts:
        return results

    median_T = float(np.median(valid_Ts))
    speed_mps = distance_per_cycle / median_T
    speed_kmh = speed_mps * 3.6

    if speed_kmh < 5 or speed_kmh > 200:
        results.append(VisionPoint(
            time=float(bw_frames[0] / fps) if len(bw_frames) else 0.0,
            raw_speed=None,
            confidence=0.0,
            period_hypothesis=f"{median_T:.3f}s",
            strip_votes=f"{len(bw_frames)}/{len(wb_frames)}",
            reject_reason="speed_out_of_range",
        ))
        return results

    mad = float(np.median(np.abs(np.array(valid_Ts) - median_T))) if valid_Ts else 0.0
    stability = 1.0 - min(1.0, mad / max(median_T, 1e-6))
    conf = min(1.0, 0.5 + 0.08 * len(valid_Ts) + 0.3 * stability)
    if not duty_ok:
        conf *= 0.7

    period_str = f"BW:{bw_median_T:.3f}s/WB:{wb_median_T:.3f}s"
    results.append(VisionPoint(
        time=float(bw_frames[0] / fps) if len(bw_frames) else 0.0,
        raw_speed=speed_mps,
        confidence=float(conf),
        period_hypothesis=period_str,
        strip_votes=f"{len(bw_frames)}/{len(wb_frames)}",
        reject_reason="" if duty_ok else "duty_cycle_warning",
    ))
    return results


# --- 兼容舊管線的存根（已證偽，方法 #1，保留僅供 speed_worker 引用） ---
# 工作單 1.5 #1 明確禁止位移法（CV 68%/149%、負位移），此為 intentional stub。
# SpeedWorker Stage1 若取不到位移會回退到 default FPS=15，Stage2 主路為時間域 100m 法，
# 因此存根不會導致整條管線失效，僅影響 Stage1 自適應 FPS 精度（待任務 A 邊緣法補足）。

def _find_marking_band(binary: np.ndarray, min_density: float = 0.02, margin: int = 30) -> tuple[int, int]:
    return (0, h) if (h := binary.shape[0]) else (0, 0)


def build_wiper_mask(roi_gray: np.ndarray, n_frames: int = 5, threshold: float = 30.0) -> np.ndarray:
    """已停用：雨刷遮罩需形態學，工作單 1.5 #2 已證偽水平 kernel，現預設關閉直接回全白 mask。"""
    h, w = roi_gray.shape[:2]
    return np.full((h, w), 255, dtype=np.uint8)


def detect_marking_displacement(roi_prev: np.ndarray, roi_curr: np.ndarray, wiper_mask: np.ndarray | None = None, apply_morph: bool = True) -> float | None:
    """已證偽的位移法存根 — 工作單 1.5 #1 禁止重啟，保留簽章僅為相容。"""
    return None


def estimate_speed_from_displacements(displacements: list[float], fps: float, pixels_per_meter: float, pts: list[float] | None = None) -> list[VisionPoint]:
    """已證偽的位移法存根 — 同上，回退由時間域法承擔。"""
    return []


def build_temporal_brightness_signal(
    roi_series: list[np.ndarray],
    lane_xs: list[int] | None = None,
    half_width: int = 40,
) -> np.ndarray:
    """時間域 100m 計時用亮度訊號：固定薄帶多點變異選優"""
    per_x_signals: dict[int, list[float]] = {}
    if lane_xs is None:
        w0 = roi_series[0].shape[1] if roi_series and roi_series[0].size else 2560
        candidates_x = [int(w0*0.28), int(w0*0.35), int(w0*0.50), int(w0*0.65), int(w0*0.72), int(w0*0.82)]
    else:
        candidates_x = lane_xs
    for x in candidates_x:
        per_x_signals[x] = []
    for roi in roi_series:
        if roi is None or roi.size == 0:
            for x in candidates_x:
                per_x_signals[x].append(0.0)
            continue
        h, w = roi.shape[:2]
        thr = compute_brightness_threshold(roi)
        _, bin_roi = cv2.threshold(roi, thr, 255, cv2.THRESH_BINARY)
        for x in candidates_x:
            x1 = max(0, x - half_width)
            x2 = min(w, x + half_width)
            strip = bin_roi[:, x1:x2]
            cnt = int(np.count_nonzero(strip))
            mean_strip = float(np.mean(roi[:, x1:x2])) if strip.size else 0.0
            per_x_signals[x].append(float(cnt) + mean_strip * 0.05)
    best_x = max(candidates_x, key=lambda x: float(np.std(per_x_signals[x])) if len(per_x_signals[x]) > 5 else 0.0)
    variances = {x: float(np.std(per_x_signals[x])) for x in candidates_x}
    best_var = variances[best_x]
    high_vars = [x for x, v in variances.items() if v >= best_var * 0.7 and v > 5.0]
    if len(high_vars) >= 2:
        arrs = [np.array(per_x_signals[x], dtype=np.float64) for x in high_vars]
        arr = np.mean(arrs, axis=0)
    else:
        arr = np.array(per_x_signals[best_x], dtype=np.float64)
    if len(arr) >= 15:
        win = max(15, int(len(arr) * 0.05))
        if win % 2 == 0:
            win += 1
        # ⚠️ 不可用 np.convolve(..., mode="same")：邊界隱性補零會把趨勢線拉向 0，
        # 去趨勢後產生假斜坡（見 marking_detector.detrend_moving_average 說明）。
        # 此處 inline reflect 補邊版（本模組被 marking_detector import，不可反向 import）。
        _k = np.ones(win) / float(win)
        _pad = win // 2
        _padded = np.pad(arr, _pad, mode="reflect")
        _full = np.convolve(_padded, _k, mode="valid")
        _start = (len(_full) - len(arr)) // 2
        detrended = arr - _full[_start:_start + len(arr)]
    else:
        detrended = arr - float(np.mean(arr))
    # _gaussian_smooth 定義在後方，先用簡易卷積
    sigma = 2.0
    kernel_size = int(sigma * 6) | 1
    if kernel_size < 3:
        return detrended
    x = np.arange(kernel_size) - kernel_size // 2
    kernel = np.exp(-0.5 * (x / sigma) ** 2)
    kernel /= kernel.sum()
    return np.convolve(detrended, kernel, mode="same")


def estimate_speed_from_temporal_peaks(
    peaks: np.ndarray,
    fps: float,
    distance_per_cycle: float = MARKING_CYCLE_LENGTH_M,
    n_cycles: int = 10,
    signal: np.ndarray | None = None,
) -> list[VisionPoint]:
    """時間域峰值間距直推（方法 #8，無 FFT/諧波）。

    僅用於兼容舊管線。請優先使用 estimate_speed_from_edge_crossings()。
    """
    # 靜止檢測：訊號變異過小或峰數過少 => 視為靜止
    if signal is not None:
        if float(np.std(signal)) < 3.0:
            return [
                VisionPoint(
                    time=float(peaks[0] / fps) if peaks is not None and len(peaks) else 0.0,
                    raw_speed=0.0,
                    confidence=0.9,
                    period_hypothesis="stationary",
                    strip_votes=str(len(peaks)) if peaks is not None else "0",
                    reject_reason="stationary_low_var",
                )
            ]
    if peaks is None or len(peaks) < 3 or fps <= 0:
        return []
    intervals = np.diff(peaks) / fps
    if len(intervals) < 3:
        return []
    # 靜止檢測2：峰間距變異過大 => 雜訊主導，視為不可信，速度 0
    cv = float(np.std(intervals) / max(np.mean(intervals), 1e-6)) if len(intervals) else 0
    if cv > 0.6 and len(peaks) < 6:
        return [
            VisionPoint(
                time=float(peaks[0] / fps),
                raw_speed=0.0,
                confidence=0.8,
                period_hypothesis=f"{float(np.median(intervals)):.3f}s",
                strip_votes=str(len(peaks)),
                reject_reason="stationary_high_cv",
            )
        ]
    median_T = float(np.median(intervals))
    if len(intervals) >= 5 and median_T > 0:
        filt = intervals[(intervals > median_T * 0.4) & (intervals < median_T * 2.5)]
        if len(filt) >= 3:
            median_T = float(np.median(filt))
    if median_T <= 0:
        return []
    speed_mps = distance_per_cycle / median_T
    speed_kmh = speed_mps * 3.6
    if speed_kmh < 5 or speed_kmh > 200:
        return [
            VisionPoint(
                time=float(peaks[0] / fps),
                raw_speed=None,
                confidence=0.0,
                period_hypothesis=f"{median_T:.3f}s",
                strip_votes=str(len(peaks)),
                reject_reason="speed_out_of_range",
            )
        ]
    mad = float(np.median(np.abs(intervals - median_T))) if len(intervals) else 0.0
    stability = 1.0 - min(1.0, mad / max(median_T, 1e-6))
    conf = min(1.0, 0.5 + 0.08 * len(peaks) + 0.3 * stability)
    return [
        VisionPoint(
            time=float(peaks[0] / fps),
            raw_speed=speed_mps,
            confidence=float(conf),
            period_hypothesis=f"{median_T:.3f}s",
            strip_votes=str(len(peaks)),
            reject_reason="",
        )
    ]
