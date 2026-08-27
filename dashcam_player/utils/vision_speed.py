"""視覺測速核心演算法

Multi-strip + Harmonic + Consensus 實現
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from dashcam_player.utils.cv_utils import preprocess_clahe_otsu
from dashcam_player.utils.log import get_logger

log = get_logger()


# 速度常數
V_MIN_MPS = 5.0 / 3.6  # ≈1.39 m/s (5 km/h)
V_MAX_MPS = 200.0 / 3.6  # ≈55.56 m/s (200 km/h)
MIN_SCORE = 0.60
MIN_MARGIN = 0.10
MIN_CONSENSUS = 3  # 4 條中至少 3 條一致
PERIOD_TOLERANCE = 0.10  # 10% 相對誤差


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


def detect_markings_multi_strip(roi_gray: np.ndarray) -> list[float]:
    """
    Multi-strip 偵測標線週期
    
    Args:
        roi_gray: ROI 灰階影像 (H, W)
    
    Returns:
        list of periods (seconds) for each strip
    """
    h, w = roi_gray.shape
    n_strips = 4
    strip_h = h // n_strips
    periods = []
    
    # CLAHE + Otsu
    enhanced, binary = preprocess_clahe_otsu(roi_gray)
    
    for i in range(n_strips):
        y1 = i * strip_h
        y2 = (i + 1) * strip_h
        strip = binary[y1:y2, :]
        
        # 水平投影
        proj = np.sum(strip, axis=0).astype(np.float32)
        
        # 平滑
        kernel = np.ones(5) / 5.0
        proj_smooth = np.convolve(proj, kernel, mode='same')
        
        # 峰值檢測
        peaks = find_peaks(proj_smooth)
        if len(peaks) < 2:
            continue
        
        # 計算峰值間距 (像素)
        periods_px = np.diff(peaks)
        if len(periods_px) == 0:
            continue
        
        # 取中位數作為該 strip 的代表週期
        period_px = float(np.median(periods_px))
        periods.append(period_px)
    
    return periods


def find_peaks(signal: np.ndarray, min_distance: int = 10, min_prominence: float = 0.1) -> np.ndarray:
    """簡易峰值檢測"""
    peaks = []
    for i in range(1, len(signal) - 1):
        if signal[i] > signal[i-1] and signal[i] > signal[i+1]:
            if signal[i] > min_prominence:
                if not peaks or i - peaks[-1] >= min_distance:
                    peaks.append(i)
    return np.array(peaks)


def cluster_periods(periods: list[float], tolerance: float = PERIOD_TOLERANCE) -> list[list[float]]:
    """將相近的週期聚類"""
    if not periods:
        return []
    sorted_periods = sorted(periods)
    clusters = [[sorted_periods[0]]]
    for p in sorted_periods[1:]:
        if abs(p - clusters[-1][0]) / clusters[-1][0] <= tolerance:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return clusters


def harmonic_hypothesis(period: float, candidates: list[str] = ["D/2", "D", "2D"]) -> dict[str, float]:
    """Harmonic 假設評分
    
    Returns: {hypothesis: score}
    """
    scores = {}
    for h in candidates:
        if h == "D/2":
            target = period * 2
        elif h == "2D":
            target = period / 2
        else:
            target = period
        # 這裡簡單用 1/target 的倒數作為代理分數
        # 實際應結合自相關強度
        scores[h] = 1.0 / (1.0 + abs(target - period))
    return scores


def select_best_hypothesis(
    periods: list[float],
    candidates: list[str] = ["D/2", "D", "2D"],
    min_score: float = MIN_SCORE,
    min_margin: float = MIN_MARGIN,
    min_consensus: int = MIN_CONSENSUS,
) -> tuple[str | None, float | None, float | None]:
    """
    選擇最佳 harmonic 假設
    
    Returns: (best_hypothesis, best_period, confidence) 或 (None, None, None)
    """
    if len(periods) < 2:
        return None, None, None
    
    # 聚類找共識
    clusters = cluster_periods(periods)
    consensus_group = max(clusters, key=len)
    consensus_ratio = len(consensus_group) / len(periods)
    
    if consensus_ratio < min_consensus / 4:
        return None, None, None
    
    base_period = float(np.median(consensus_group))
    
    # 評估各 harmonic 假設
    best_hyp = None
    best_score = 0.0
    second_score = 0.0
    
    for hyp in ["D/2", "D", "2D"]:
        if hyp == "D/2":
            target = base_period * 2
        elif hyp == "2D":
            target = base_period / 2
        else:
            target = base_period
        
        # 分數 = 自相關強度的代理 (這裡簡化)
        score = 1.0 / (1.0 + abs(target - base_period))
        
        if score > best_score:
            second_score = best_score
            best_score = score
            best_hyp = hyp
        elif score > second_score:
            second_score = score
    
    # 檢查門檻
    if best_score < min_score:
        return None, None, None
    if best_score - second_score < min_margin:
        return None, None, None
    if len(clusters[0]) < min_consensus:
        return None, None, None
    
    confidence = min(1.0, (best_score * consensus_ratio * 2))
    return best_hyp, base_period, confidence


def estimate_vision_speed(
    periods: list[float],
    pts: list[float] | None = None,
    distance_per_cycle: float = 10.0,
) -> list[VisionPoint]:
    """
    根據偵測到的週期估算速度
    
    Args:
        periods: 各 strip 偵測到的週期 (秒)
        pts: 對應的時間點 (秒)，若無則用索引推估
        distance_per_cycle: 一個週期的實際距離 (公尺)
    
    Returns:
        VisionPoint 列表
    """
    if not periods:
        return []
    
    hyp, base_period, confidence = select_best_hypothesis(periods)
    if hyp is None or base_period <= 0:
        return []
    
    # 速度 = 距離 / 時間
    speed_mps = distance_per_cycle / base_period
    
    # 檢查速度合理性
    if speed_mps < V_MIN_MPS or speed_mps > V_MAX_MPS:
        return [VisionPoint(
            time=pts[0] if pts else 0.0,
            raw_speed=None,
            confidence=0.0,
            period_hypothesis=hyp,
            strip_votes="",
            reject_reason="speed_out_of_range"
        )]
    
    # 構建結果
    if pts is None:
        pts = [i * base_period for i in range(len(periods))]
    
    result = []
    for i, p in enumerate(periods):
        t = pts[i] if i < len(pts) else i * base_period
        v = distance_per_cycle / p if p > 0 else None
        result.append(VisionPoint(
            time=t,
            raw_speed=v,
            confidence=confidence,
            period_hypothesis=hyp,
            strip_votes="",  # 待實現
            reject_reason=""
        ))
    return result


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