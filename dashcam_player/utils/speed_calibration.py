"""速度校正模組 — 統一使用 models.speed.CalibrationProfile

EMA + Outlier Rejection + Quality Gates
此檔案的 CalibrationProfile 已棄用，重新導出自 models.speed 以避免雙類別不一致。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# 統一導出，避免雙定義導致 isinstance 失效
from dashcam_player.models.speed import CalibrationProfile  # noqa: F401

# 為向後相容，保留舊匯出路徑
__all__ = ["CalibrationProfile", "CalibrationCandidate"]


@dataclass
class CalibrationCandidate:
    """單次校正候選值（純視覺，不依賴 GPS）"""

    ratio: float
    offset: float
    sample_count: int
    speed_min: float
    speed_max: float
    correlation: float
    vision_quality: float


EMA_ALPHA = 0.8
OUTLIER_RATIO_THRESHOLD = 0.4  # 40% 偏差視為 outlier
MIN_SAMPLES_FOR_CALIB = 5
MIN_CORRELATION = 0.7
MIN_VISION_QUALITY = 0.5
MIN_SPEED_RANGE = 20.0  # km/h


def sanity_check_candidate(candidate: dict, old_profile: CalibrationProfile | None) -> bool:
    """候選值基本檢查"""
    ratio = candidate.get("ratio", 1.0)
    if ratio <= 0 or ratio > 5.0:
        return False

    if candidate.get("sample_count", 0) < MIN_SAMPLES_FOR_CALIB:
        return False

    if candidate.get("correlation", 0) < MIN_CORRELATION:
        return False

    if candidate.get("vision_quality", 0) < MIN_VISION_QUALITY:
        return False

    speed_range = candidate.get("speed_max", 0) - candidate.get("speed_min", 0)
    if speed_range < MIN_SPEED_RANGE:
        return False

    # Outlier 檢查：與舊值偏差過大
    if old_profile and old_profile.sample_count > 10:
        old_ratio = old_profile.ratio
        if old_ratio > 0:
            deviation = abs(ratio / old_ratio - 1.0)
            if deviation > OUTLIER_RATIO_THRESHOLD:
                return False

    return True


def ema_update(old: CalibrationProfile, candidate: dict, alpha: float = EMA_ALPHA) -> CalibrationProfile:
    """EMA 更新校正檔"""
    new = CalibrationProfile(
        camera_profile=old.camera_profile if old else "default",
        resolution_w=old.resolution_w if old else 0,
        resolution_h=old.resolution_h if old else 0,
        file_format=old.file_format if old else "",
    )

    new.ratio = alpha * old.ratio + (1 - alpha) * candidate["ratio"] if old else candidate["ratio"]
    new.offset = alpha * old.offset + (1 - alpha) * candidate["offset"] if old else candidate["offset"]

    # 更新統計
    new.sample_count = (old.sample_count if old else 0) + candidate.get("sample_count", 0)
    new.speed_min = min(old.speed_min, candidate.get("speed_min", 0)) if old else candidate.get("speed_min", 0)
    new.speed_max = max(old.speed_max, candidate.get("speed_max", 0)) if old else candidate.get("speed_max", 0)

    # 信心度加權
    new.confidence = alpha * old.confidence + (1 - alpha) * candidate.get("vision_quality", 0.5) if old else candidate.get("vision_quality", 0.5)

    # MAD/STD 近似更新 (使用簡單指數移動)
    if old:
        deviation = abs(candidate["ratio"] - old.ratio)
        new.ratio_mad = alpha * old.ratio_mad + (1 - alpha) * deviation
        new.ratio_std = math.sqrt(alpha * old.ratio_std**2 + (1 - alpha) * deviation**2)
        new.source_coverage = alpha * old.source_coverage + (1 - alpha) * min(1.0, candidate.get("sample_count", 0) / 50.0)
    else:
        new.ratio_mad = 0.0
        new.ratio_std = 0.0
        new.source_coverage = min(1.0, candidate.get("sample_count", 0) / 50.0)

    new.speed_min = min(old.speed_min, candidate.get("speed_min", 0)) if old else candidate.get("speed_min", 0)
    new.speed_max = max(old.speed_max, candidate.get("speed_max", 0)) if old else candidate.get("speed_max", 0)

    return new


def validate_and_ema_update(
    old: CalibrationProfile | None,
    candidate: dict,
) -> CalibrationProfile | None:
    """驗證候選值並更新校正檔

    Returns:
        更新後的 CalibrationProfile，若驗證失敗回傳 None
    """
    if not sanity_check_candidate(candidate, old):
        return None

    return ema_update(old, candidate) if old else CalibrationProfile(
        camera_profile=candidate.get("camera_profile", "default"),
        ratio=candidate["ratio"],
        offset=candidate["offset"],
        sample_count=candidate.get("sample_count", 0),
        speed_min=candidate.get("speed_min", 0),
        speed_max=candidate.get("speed_max", 0),
        confidence=candidate.get("vision_quality", 0.5),
    )


def build_candidate_from_window(
    vision_points: list,
    vision_quality: float,
    camera_profile: str,
    resolution_w: int,
    resolution_h: int,
    file_format: str,
) -> dict | None:
    """從時間窗口構建校正候選值（純視覺，不依賴 GPS）。

    使用多週期速度一致性作為品質指標，而非 GPS 速度。
    """
    if len(vision_points) < 3:
        return None

    v_speeds = [p.raw_vision for p in vision_points if p.raw_vision is not None]

    if len(v_speeds) < MIN_SAMPLES_FOR_CALIB:
        return None

    # 計算 period 一致性（MAD）作為品質替代指標
    if len(v_speeds) >= 3:
        median_v = float(np.median(v_speeds))
        mad = float(np.median(np.abs(np.array(v_speeds) - median_v)))
        consistency = 1.0 - min(1.0, mad / max(median_v, 1e-6))
    else:
        consistency = 0.5

    # 計算 ratio 基於 vision 自身多週期一致性
    # 使用 median of intervals 作為 ratio（相對於 1.0 的偏差）
    if len(v_speeds) >= 2:
        intervals = np.diff(v_speeds)
        ratio = float(np.median(intervals)) if len(intervals) > 0 else 1.0
    else:
        ratio = 1.0

    # 計算相關係數
    if len(v_speeds) > 1:
        corr = float(np.corrcoef(v_speeds[:-1], v_speeds[1:])[0, 1])
    else:
        corr = 0.0

    return {
        "ratio": ratio,
        "offset": 0.0,
        "sample_count": len(v_speeds),
        "speed_min": min(v_speeds) * 3.6,
        "speed_max": max(v_speeds) * 3.6,
        "correlation": corr,
        "vision_quality": vision_quality,
        "consistency": consistency,
        "camera_profile": camera_profile,
        "resolution_w": resolution_w,
        "resolution_h": resolution_h,
        "file_format": file_format,
    }


