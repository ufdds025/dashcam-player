"""Speed ROI 資料模型

定義速度偵測 ROI 設定、偵測結果等資料結構。
與 GPS ROI 系統架構一致：綁定「解析度 + 檔案格式」作為查詢 key。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class SpeedROISetting:
    """Speed ROI 設定，綁定「解析度 + 檔案格式」作為查詢 key"""

    resolution_w: int
    resolution_h: int
    file_format: str

    roi_x: float          # 左邊界 (0.0-1.0)
    roi_y: float          # 上邊界 (0.0-1.0)
    roi_w: float          # 寬度 (0.0-1.0)
    roi_h: float          # 高度 (0.0-1.0)

    side: str = "both"    # "left" / "right" / "both" — 哪一側有標線
    source: str = "user_adjusted"
    confidence: float = 1.0
    timestamp: float = field(default_factory=time.time)
    hood_top_y: float | None = None  # 存檔當時的車體上緣（正規化 0-1）；
    # 跨機器同解析度 key 撞車時，載入端比對本次偵測 hood_top，偏差 >0.05 即棄用舊框

    @property
    def key(self) -> str:
        return f"{self.resolution_w}x{self.resolution_h}_{self.file_format.upper().lstrip('.')}"


@dataclass
class SpeedROIDetectionResult:
    """自動偵測結果"""

    video_path: str
    video_width: int
    video_height: int
    file_format: str

    detected_roi: tuple[float, float, float, float]   # (x, y, w, h) relative
    detected_side: str                                  # "left" / "right" / "both"
    confidence: float
    sample_frame_idx: int
    hood_top_y: float | None = None  # 本次偵測的車體上緣（None = 無車體簽名）
