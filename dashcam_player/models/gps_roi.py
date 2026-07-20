"""GPS ROI 資料模型

定義 ROI 設定、偵測結果等資料結構。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class GPSROISetting:
    """GPS ROI 設定，綁定「解析度 + 檔案格式」作為查詢 key"""

    resolution_w: int
    resolution_h: int
    file_format: str

    roi_x: float
    roi_y: float
    roi_w: float
    roi_h: float

    corner: str = ""
    layout: str = ""
    source: str = "user_adjusted"
    confidence: float = 1.0
    timestamp: float = field(default_factory=time.time)

    @property
    def key(self) -> str:
        return f"{self.resolution_w}x{self.resolution_h}_{self.file_format.upper().lstrip('.')}"


@dataclass
class GPSROIDetectionResult:
    """自動偵測結果"""

    video_path: str
    video_width: int
    video_height: int
    file_format: str

    detected_roi: tuple[float, float, float, float]
    detected_corner: str
    confidence: float
    sample_frame_idx: int
    raw_text: str = ""
