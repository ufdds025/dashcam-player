"""速度測速相關資料模型"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class SpeedPoint:
    """單一速度資料點"""

    time: float          # canonical video 相對秒
    raw_vision: float | None      # m/s
    corrected: float | None       # m/s
    fused: float | None           # m/s（顯示/匯出前轉 km/h）
    confidence: float
    source: str          # vision|gps|ocr|fused|estimated|none
    period_hypothesis: str = "D"  # D/2/D/2D
    strip_votes: str = ""         # 例 "3/4"
    reject_reason: str = ""       # 例 "harmonic_margin"

    def to_dict(self) -> dict:
        return {
            "time": self.time,
            "raw_vision": self.raw_vision,
            "corrected": self.corrected,
            "fused": self.fused,
            "confidence": self.confidence,
            "source": self.source,
            "period_hypothesis": self.period_hypothesis,
            "strip_votes": self.strip_votes,
            "reject_reason": self.reject_reason,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SpeedPoint:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class SpeedSettings:
    """軟體測速設定（已棄用，統一使用 capture_settings.SpeedSettings）

    保留僅為向後相容，請新程式碼一律 import dashcam_player.models.capture_settings.SpeedSettings
    預設 False（省資源，手動開啟）。
    """  # noqa: D400

    enabled: bool = False
    distance_per_cycle: float = 10.0
    smoothing_window: int = 5
    burn_speed_on_export: bool = False


@dataclass
class CalibrationProfile:
    """校正檔案"""
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
    updated_at: float = field(default_factory=time.time)

    # 需配合 AppSettings 儲存的額外欄位
    resolution_w: int = 0
    resolution_h: int = 0
    file_format: str = ""

    @property
    def key(self) -> str:
        return f"{self.camera_profile}/{self.resolution_w}x{self.resolution_h}_{self.file_format.upper().lstrip('.')}"


@dataclass
class SpeedTrack:
    """完整速度軌跡"""
    points: list[SpeedPoint]
    profile: str = "default"
    coverage: float = 0.0  # 0.0-1.0
    status: str = "OK"     # OK/PARTIAL

    def to_dict(self) -> dict:
        return {
            "points": [p.to_dict() for p in self.points],
            "profile": self.profile,
            "coverage": self.coverage,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SpeedTrack:
        pts = [SpeedPoint.from_dict(p) for p in data.get("points", [])]
        return cls(
            points=pts,
            profile=data.get("profile", "default"),
            coverage=data.get("coverage", 0.0),
            status=data.get("status", "OK"),
        )
