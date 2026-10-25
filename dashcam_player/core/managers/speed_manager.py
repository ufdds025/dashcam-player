"""SpeedManager - 速度測速外觀

封裝 SpeedWorker、SpeedScheduler、校正邏輯
Controller 透過此 Facade 與速度系統互動
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PyQt5.QtCore import QObject, pyqtSignal

if TYPE_CHECKING:
    from dashcam_player.config.settings import AppSettings
    from dashcam_player.core.gps_manager import GPSManager
    from dashcam_player.models.speed import SpeedSettings
    from dashcam_player.models.video_info import VideoInfo

from dashcam_player.utils.log import get_logger
from dashcam_player.utils.vision_speed import MARKING_CYCLE_LENGTH_M  # noqa: E402

log = get_logger()


class SpeedManager(QObject):
    """速度外觀：查詢/校正/掃描觸發"""

    speed_updated = pyqtSignal(float, float, float, str)   # (time, speed_kmh, confidence, source)
    speed_ready = pyqtSignal(str, object)                # (video_path, SpeedTrack)
    error_occurred = pyqtSignal(str, str)                # (title, message)
    status_message = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._worker: Any | None = None  # SpeedWorker
        self._scheduler: Any | None = None  # SpeedScheduler
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
        self._debug_roi: tuple[int, int, int, int] | None = None  # (x1, y1, x2, y2)
        self._debug_speed_kmh: float | None = None
        self._debug_confidence: float = 0.0
        self._debug_displacements: list[float] = []  # type: ignore[no-redef]

        # ROI 快取（避免每幀 QSettings I/O）
        self._roi_cache_key: str | None = None
        self._roi_cache_val: tuple[float, float, float, float] | None = None

    def set_app_settings(self, settings: AppSettings) -> None:
        self._app_settings = settings

    def set_video_context(self, video_path: str, video_info: VideoInfo | None) -> None:
        self._video_path = video_path
        self._video_info = video_info
        # 影片切換時清除 ROI 快取，避免命中舊解析度的快取
        self._roi_cache_key = None
        self._roi_cache_val = None

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
        from dashcam_player.core.speed_scheduler import SpeedScheduler
        from dashcam_player.core.speed_worker import SpeedWorker

        self._worker = SpeedWorker(hw_backends=[])  # type: ignore[assignment]
        self._worker.result_ready.connect(self._on_speed_ready)  # type: ignore[attr-defined]
        self._worker.progress.connect(self._on_progress)  # type: ignore[attr-defined]
        self._worker.error_occurred.connect(self._on_error)  # type: ignore[attr-defined]
        self._worker.stage_changed.connect(self._on_stage_changed)  # type: ignore[attr-defined]
        try:
            self._worker.status_message.connect(self._on_status_message)  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            self._worker.speed_updated.connect(self._on_speed_updated)  # type: ignore[attr-defined]
        except Exception:
            pass

        # Scheduler
        self._scheduler = SpeedScheduler()  # type: ignore[assignment]
        self._scheduler.set_worker(self._worker)  # type: ignore[attr-defined]

        # 開啟掃描
        distance = self._speed_settings.distance_per_cycle if self._speed_settings else MARKING_CYCLE_LENGTH_M

        # 載入 ROI + 校準 ppm
        roi_tuple = None
        side = "both"
        ppm = 50.0
        if self._video_info and self._app_settings:
            import os
            fmt = os.path.splitext(self._video_path)[1].lstrip(".").lower() or "mp4"
            roi_setting = self._app_settings.load_speed_roi(
                self._video_info.width, self._video_info.height, fmt
            )
            if roi_setting:
                roi_tuple = (roi_setting.roi_x, roi_setting.roi_y, roi_setting.roi_w, roi_setting.roi_h)
                side = roi_setting.side
                log.info(f"使用已儲存 SpeedROI: {roi_tuple}, side={side}")
            ppm = self._app_settings.load_speed_ppm(
                self._video_info.width, self._video_info.height, fmt
            )
            if ppm != 50.0:
                log.info(f"使用已校準 ppm={ppm:.1f}")

        self._worker.open(self._video_path, distance, roi=roi_tuple, side=side, pixels_per_meter=ppm)

    def stop_scan(self) -> None:
        """停止掃描"""
        if self._worker and self._worker.isRunning():  # type: ignore[attr-defined]
            self._worker.close()  # type: ignore[attr-defined]
            self._worker.wait(5000)  # type: ignore[attr-defined]
            if self._worker.isRunning():  # type: ignore[attr-defined]
                log.warning("SpeedWorker 未在 5 秒內停止，強制終止")
                self._worker.terminate()  # type: ignore[attr-defined]
                self._worker.wait(1000)  # type: ignore[attr-defined]
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

    def _on_speed_updated(self, time_sec: float, speed_kmh: float, confidence: float, source: str) -> None:
        """即時速度更新（供 UI 疊加與 debug 快取）"""
        try:
            # 更新 debug 快取，供 VideoPlayer 每幀 overlay 讀取
            self._debug_speed_kmh = speed_kmh
            # confidence 可能是字串或 float，統一轉 float
            try:
                self._debug_confidence = float(confidence)
            except Exception:
                self._debug_confidence = 0.7 if speed_kmh and speed_kmh > 0 else 0.0
            # 保留最近位移（若 worker 有提供）
            if hasattr(self._worker, "_displacements"):
                self._debug_displacements = list(getattr(self._worker, "_displacements", []) or [])
        except Exception:
            pass
        self.speed_updated.emit(time_sec, speed_kmh, confidence, source)

    def _on_stage_changed(self, stage: str) -> None:
        if stage == "stage1":
            self.status_message.emit("速度掃描: 階段 1/2 - 粗探中...")
        elif stage == "stage2":
            self.status_message.emit("速度掃描: 階段 2/2 - 主掃中...")
        elif stage == "rescan":
            self.status_message.emit("速度掃描: 護網重掃中...")

    def _on_status_message(self, msg: str) -> None:
        """Worker 狀態訊息轉發（ROI 污染棄用通知等）"""
        self.status_message.emit(msg)

    def set_playback_active(self, active: bool) -> None:
        """播放狀態變更"""
        if self._scheduler:
            self._scheduler.set_playback_active(active)

    def set_batch_active(self, active: bool) -> None:
        """批次截圖狀態變更"""
        if self._scheduler:
            self._scheduler.set_batch_active(active)

    # GPS 僅允許作為候選消歧的外部參照（方法 #7），非固定先驗；
    # 不調整偵測門檻、不修改演算法邏輯。
    def recalibrate_with_gps(self) -> None:
        """GPS 候選消歧（僅當 GPS 可用時，對候選清單做外部参照；非調參）"""
        pass

    def get_current_speed(self, frame_idx: int, fps: float) -> tuple[float | None, str]:
        """取得當前幀的速度 (km/h) + 來源

        Returns: (speed_kmh, source) 或 (None, "")
        """
        if not self._current_track or not self._current_track.points:
            return None, ""
        # 從 frame_idx 計算時間
        sec = frame_idx / fps if fps > 0 else 0.0
        points = self._current_track.points
        # 小於第一點或大於最後一點
        if sec <= points[0].time:
            return self._to_kmh(points[0].fused or points[0].corrected), points[0].source
        if sec >= points[-1].time:
            return self._to_kmh(points[-1].fused or points[-1].corrected), points[-1].source
        # 二分搜尋找最近點（使用最近鄰插值，反應更快）
        lo, hi = 0, len(points) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if points[mid].time <= sec:
                lo = mid
            else:
                hi = mid
        # 選擇時間上更近的點（最近鄰插值）
        p0, p1 = points[lo], points[hi]
        dist0 = abs(sec - p0.time)
        dist1 = abs(sec - p1.time)
        nearest = p0 if dist0 <= dist1 else p1
        speed = nearest.fused or nearest.corrected
        return self._to_kmh(speed), nearest.source

    @staticmethod
    def _to_kmh(speed_ms: float | None) -> float | None:
        if speed_ms is None:
            return None
        return speed_ms * 3.6

    @property
    def current_track(self):
        return self._current_track

    @property
    def current_roi(self) -> tuple[float, float, float, float] | None:
        """取得目前使用的 speed ROI（帶快取，避免每幀 QSettings I/O）"""
        if not self._video_info or not self._app_settings:
            return None
        import os
        fmt = os.path.splitext(self._video_path)[1].lstrip(".").lower() or "mp4"
        key = f"{self._video_info.width}x{self._video_info.height}_{fmt}"
        if getattr(self, "_roi_cache_key", None) == key:
            return getattr(self, "_roi_cache_val", None)
        roi = self._app_settings.load_speed_roi(
            self._video_info.width, self._video_info.height, fmt
        )
        val = (roi.roi_x, roi.roi_y, roi.roi_w, roi.roi_h) if roi else None
        self._roi_cache_key = key
        self._roi_cache_val = val
        return val

    def invalidate_roi_cache(self) -> None:
        """ROI 對話框儲存後呼叫，強制下次重讀"""
        self._roi_cache_key = None
        self._roi_cache_val = None

    def get_detection_debug(self) -> dict:
        """取得最新偵測除錯資訊，供 VideoPlayer 疊加畫面"""
        return {
            "roi": self._debug_roi,
            "speed_kmh": self._debug_speed_kmh,
            "confidence": self._debug_confidence,
            "displacements": self._debug_displacements,
        }

    def update_detection_debug(
        self,
        roi: tuple[int, int, int, int] | None = None,
        speed_kmh: float | None = None,
        confidence: float = 0.0,
        displacements: list[float] | None = None,
    ) -> None:
        """更新偵測除錯資訊"""
        self._debug_roi = roi
        self._debug_speed_kmh = speed_kmh
        self._debug_confidence = confidence
        self._debug_displacements = displacements or []

    def close(self) -> None:
        """關閉並釋放資源"""
        if self._closed:
            return
        self._closed = True
        self.stop_scan()
