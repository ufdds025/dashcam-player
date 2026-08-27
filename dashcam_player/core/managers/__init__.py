"""Manager 層：將 Controller 的職責拆分為獨立 QObject。

Controller 保留為薄 Orchestrator，持有各 Manager 並轉發 signal。
"""

from dashcam_player.core.managers.capture_manager import CaptureManager
from dashcam_player.core.managers.export_manager import ExportManager
from dashcam_player.core.managers.gps_facade import GPSFacade
from dashcam_player.core.managers.playback_manager import PlaybackManager
from dashcam_player.core.managers.speed_manager import SpeedManager

__all__ = ["PlaybackManager", "CaptureManager", "ExportManager", "GPSFacade", "SpeedManager"]
