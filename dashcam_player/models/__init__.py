from dashcam_player.models.capture_settings import (
    CaptureSettings,
    ExportSettings,
    GpsDisplaySettings,
    ScreenshotSettings,
)
from dashcam_player.models.capture_settings import (
    SpeedSettings as CaptureSpeedSettings,
)
from dashcam_player.models.resolution import Resolution
from dashcam_player.models.speed import CalibrationProfile, SpeedPoint, SpeedTrack
from dashcam_player.models.video_info import VideoInfo

# 統一預設：省資源（enabled=False），避免雙定義衝突
SpeedSettings = CaptureSpeedSettings  # noqa: F811

__all__ = [
    "CalibrationProfile",
    "CaptureSettings",
    "ExportSettings",
    "GpsDisplaySettings",
    "Resolution",
    "ScreenshotSettings",
    "SpeedPoint",
    "SpeedSettings",
    "SpeedTrack",
    "VideoInfo",
]
