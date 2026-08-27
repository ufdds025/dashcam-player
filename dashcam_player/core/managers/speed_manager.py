"""SpeedManager - 速度測速外觀

封裝 SpeedWorker、SpeedScheduler、校正邏輯
Controller 透過此 Facade 與速度系統互動
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt5.QtCore import QObject, pyqtSignal

if TYPE_CHECKING:
    from dashcam_player.config.settings import AppSettings
    from dashcam_player.core.gps_manager import GPSManager
    from dashcam_player.models.speed import SpeedTrack, SpeedSettings
    from dashcam_player.models.video_info import VideoInfo

from dashcam_player.utils.log import get_logger

log = get_logger()


class SpeedManager(QObject):
    """速度外觀：查詢/校正/掃描觸發"""
    
    speed_updated = pyqtSignal(float, float, str, str)   # (time, speed_kmh, confidence_level, source)
    speed_ready = pyqtSignal(str, object)                # (video_path, SpeedTrack)
    status_message = pyqtSignal(str)
    
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._worker = None
        self._scheduler = None
        self._app_settings: AppSettings | None = None
        self._video_path: str = ""
        self._video_info: VideoInfo | None = None
        self._gps_manager: GPSManager | None = None
        self._speed_settings: SpeedSettings | None = None
        self._closed = False
        
        # 校正狀態
        self._calibration_profile = None
        
    def set_app_settings(self, settings: AppSettings) -> None:
        self._app_settings = settings
    
    def set_video_context(self, video_path: str, video_info: VideoInfo | None) -> None:
        self._video_path = video_path
        self._video_info = video_info
    
    def set_gps_manager(self, gps_manager: GPSManager | None) -> None:
        self._gps_manager = gps_manager
    
    def set_speed_settings(self, settings: SpeedSettings) -> None:
        self._speed_settings = settings
    
    @property
    def speed_settings(self) -> SpeedSettings | None:
        return self._speed_settings
    
    @property
    def has_worker(self) -> bool:
        return self._worker is not None
    
    @property
    def is_scanning(self) -> bool:
        return self._worker is not None and self._worker.isRunning()
    
    def start_scan(self) -> None:
        """啟動背景速度掃描"""
        if self._closed:
            return
        if not self._video_path:
            return
        if self.is_scanning:
            log.warning("速度掃描已在進行中")
            return
        if not self._speed_settings or not self._speed_settings.enabled:
            return
        
        self.status_message.emit("開始視覺測速掃描...")
        
        # 建立 worker
        from dashcam_player.core.speed_worker import SpeedWorker
        from dashcam_player.core.speed_scheduler import SpeedScheduler
        
        self._worker = SpeedWorker(hw_backends=[])
        self._worker.result_ready.connect(self._on_speed_ready)
        self._worker.progress.connect(self._on_progress)
        self._worker.error_occurred.connect(self._on_error)
        self._worker.stage_changed.connect(self._on_stage_changed)
        
        # Scheduler
        self._scheduler = SpeedScheduler()
        self._scheduler.set_worker(self._worker)
        
        # 開啟掃描
        distance = self._speed_settings.distance_per_cycle if self._speed_settings else 10.0
        self._worker.open(self._video_path, distance)
    
    def stop_scan(self) -> None:
        """停止掃描"""
        if self._worker and self._worker.isRunning():
            self._worker.close()
            self._worker.wait(5000)
            if self._worker.isRunning():
                log.warning("SpeedWorker 未在 5 秒內停止，強制終止")
                self._worker.terminate()
                self._worker.wait(1000)
            self._worker = None
            self._scheduler = None
    
    def cancel_scan(self) -> None:
        """取消掃描 (保留部分結果)"""
        self.stop_scan()
    
    def _on_speed_ready(self, track) -> None:
        """掃描完成"""
        log.info(f"速度掃描完成: {track.status}, {len(track.points)} points")
        self.speed_ready.emit(self._video_path, track)
        self._worker = None
        self._scheduler = None
    
    def _on_progress(self, current: int, total: int) -> None:
        pass  # 進度由 Controller 處理
    
    def _on_error(self, msg: str) -> None:
        log.error(f"SpeedWorker 錯誤: {msg}")
        self.error_occurred.emit("速度掃描錯誤", msg)
        self._worker = None
        self._scheduler = None
    
    def _on_stage_changed(self, stage: str) -> None:
        if stage == "stage1":
            self.status_message.emit("速度掃描: 階段 1/2 - 粗探中...")
        elif stage == "stage2":
            self.status_message.emit("速度掃描: 階段 2/2 - 主掃中...")
        elif stage == "rescan":
            self.status_message.emit("速度掃描: 護網重掃中...")
    
    def set_playback_active(self, active: bool) -> None:
        """播放狀態變更"""
        if self._scheduler:
            self._scheduler.set_playback_active(active)
    
    def set_batch_active(self, active: bool) -> None:
        """批次截圖狀態變更"""
        if self._scheduler:
            self._scheduler.set_batch_active(active)
    
    # GPS 融合相關 (P2 階段實作)
    def recalibrate_with_gps(self) -> None:
        """GPS 融合校正 (待 P2 階段實作)"""
        pass
    
    def get_current_speed(self, frame_idx: int, fps: float) -> tuple[float | None, str]:
        """取得當前幀的速度 (km/h) + 來源
        
        Returns: (speed_kmh, source) 或 (None, "")
        """
        # 待實作：從 SpeedTrack 插值
        return None, ""
    
    def close(self) -> None:
        """關閉並釋放資源"""
        if self._closed:
            return
        self._closed = True
        self.stop_scan()
    
    # 為了相容 Controller 的 error_occurred 連接
    error_occurred = pyqtSignal(str, str)
    speed_updated = pyqtSignal(float, float, str, str)  # time, speed_kmh, level, source
    speed_ready = pyqtSignal(str, object)
    status_message = pyqtSignal(str)