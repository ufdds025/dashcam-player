"""速度校正模組

EMA + Outlier Rejection + Quality Gates
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import math


@dataclass
class CalibrationCandidate:
    """單次校正候選值"""
    ratio: float
    offset: float
    sample_count: int
    speed_min: float
    speed_max: float
    correlation: float
    vision_quality: float
    gps_quality: float


@dataclass
class CalibrationProfile:
    """校正檔案 (持久化)"""
    camera_profile: str = "default"
    ratio: float = 1.0
    offset: float = 0.0
    a: float = 1.0
    sample_count: int = 0
    ratio_mad: float = 0.0
    ratio_std: float = 0.0
    source_coverage: float = 0.0
    speed_min: float = 0.0
    speed_max: float = 0.0
    confidence: float = 0.0
    updated_at: float = 0.0
    resolution_w: int = 0
    resolution_h: int = 0
    file_format: str = ""

    @property
    def key(self) -> str:
        return f"{self.camera_profile}/{self.resolution_w}x{self.resolution_h}_{self.file_format.upper().lstrip('.')}"


EMA_ALPHA = 0.8
OUTLIER_RATIO_THRESHOLD = 0.4  # 40% 偏差視為 outlier
MIN_SAMPLES_FOR_CALIB = 5
MIN_CORRELATION = 0.7
MIN_VISION_QUALITY = 0.5
MIN_GPS_QUALITY = 0.5
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
    
    if candidate.get("gps_quality", 0) < MIN_GPS_QUALITY:
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
    gps_points: list,
    vision_quality: float,
    gps_quality: float,
    camera_profile: str,
    resolution_w: int,
    resolution_h: int,
    file_format: str,
) -> dict | None:
    """從時間窗口構建校正候選值"""
    if len(vision_points) < 3 or len(gps_points) < 3:
        return None
    
    # 時間對齊 (假設已在同一 timeline)
    # 這裡簡化：假設兩者 time 已對齊
    v_speeds = [p.raw_vision for p in vision_points if p.raw_vision is not None]
    g_speeds = [p.fused for p in gps_points if p.fused is not None]
    
    if len(v_speeds) != len(g_speeds):
        # 簡單插值對齊
        pass
    
    if len(v_speeds) < MIN_SAMPLES_FOR_CALIB:
        return None
    
    # 計算 ratio (median of gps/vision)
    ratios = [g / v for v, g in zip(v_speeds, g_speeds) if v > 0]
    if not ratios:
        return None
    
    ratio = sorted(ratios)[len(ratios) // 2]
    
    # 計算相關係數
    if len(v_speeds) > 1:
        import numpy as np
        corr = np.corrcoef(v_speeds, g_speeds)[0, 1]
    else:
        corr = 0.0
    
    return {
        "ratio": ratio,
        "offset": 0.0,
        "sample_count": len(ratios),
        "speed_min": min(min(v_speeds), min(g_speeds)) * 3.6,
        "speed_max": max(max(v_speeds), max(g_speeds)) * 3.6,
        "correlation": corr,
        "vision_quality": vision_quality,
        "gps_quality": gps_quality,
        "camera_profile": camera_profile,
        "resolution_w": resolution_w,
        "resolution_h": resolution_h,
        "file_format": file_format,
    }


# 需要 import math
import math