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
    error_occurred = pyqtSignal(str, str)                # (title, message)
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
        self._current_track = None

        # 除錯資訊（最新一幀的偵測結果）
        self._debug_roi = None  # (x1, y1, x2, y2)
        self._debug_periods: list[float] = []
        self._debug_speed_kmh: float | None = None
        self._debug_confidence: float = 0.0
        
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
        self._current_track = track
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
        if not self._current_track or not self._current_track.points:
            return None, ""
        # 從 frame_idx 計算時間，線性插值
        sec = frame_idx / fps if fps > 0 else 0.0
        points = self._current_track.points
        # 小於第一點或大於最後一點
        if sec <= points[0].time:
            return self._to_kmh(points[0].fused or points[0].corrected), points[0].source
        if sec >= points[-1].time:
            return self._to_kmh(points[-1].fused or points[-1].corrected), points[-1].source
        # 二分搜尋找區間
        lo, hi = 0, len(points) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if points[mid].time <= sec:
                lo = mid
            else:
                hi = mid
        p0, p1 = points[lo], points[hi]
        dt = p1.time - p0.time
        if dt <= 0:
            return self._to_kmh(p0.fused or p0.corrected), p0.source
        t = (sec - p0.time) / dt
        v0 = p0.fused or p0.corrected or 0.0
        v1 = p1.fused or p1.corrected or 0.0
        speed_ms = v0 + t * (v1 - v0)
        return self._to_kmh(speed_ms), p0.source

    @staticmethod
    def _to_kmh(speed_ms: float | None) -> float | None:
        if speed_ms is None:
            return None
        return speed_ms * 3.6

    @property
    def current_track(self):
        return self._current_track

    def get_detection_debug(self) -> dict:
        """取得最新偵測除錯資訊，供 VideoPlayer 疊加畫面"""
        return {
            "roi": self._debug_roi,
            "periods": self._debug_periods,
            "speed_kmh": self._debug_speed_kmh,
            "confidence": self._debug_confidence,
        }

    def update_detection_debug(
        self,
        roi: tuple[int, int, int, int] | None = None,
        periods: list[float] | None = None,
        speed_kmh: float | None = None,
        confidence: float = 0.0,
    ) -> None:
        """更新偵測除錯資訊"""
        self._debug_roi = roi
        self._debug_periods = periods or []
        self._debug_speed_kmh = speed_kmh
        self._debug_confidence = confidence

    def close(self) -> None:
        """關閉並釋放資源"""
        if self._closed:
            return
        self._closed = True
        self.stop_scan()