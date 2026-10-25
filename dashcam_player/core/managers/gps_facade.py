"""GPSFacade — GPS 查詢/疊加/ROI 對話職責。

封裝 GPSManager 的查詢、疊加、匯出功能，
Controller 透過此 Facade 與 GPS 系統互動。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt5.QtCore import QObject, pyqtSignal

if TYPE_CHECKING:

    from dashcam_player.config.settings import AppSettings
    from dashcam_player.core.gps_manager import GPSManager
    from dashcam_player.models.video_info import VideoInfo

from dashcam_player.utils.log import get_logger

log = get_logger()


class GPSFacade(QObject):
    """GPS 外觀：查詢/疊加/ROI 對話觸發。"""

    gps_position_updated = pyqtSignal(float, float)
    gps_ready = pyqtSignal(str, bool, int)
    status_message = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._gps_manager: GPSManager | None = None
        self._app_settings: AppSettings | None = None
        self._video_path: str = ""
        self._video_info: VideoInfo | None = None

    def set_gps_manager(self, mgr: GPSManager | None) -> None:
        """設定 GPSManager 並管理其信號生命週期。

        disconnect 舊 manager 的已連信號，再 connect 新 manager 的三組信號，
        Controller 不需自行操作信號。
        """
        old = self._gps_manager
        if old is not None:
            try:
                old.gps_loaded.disconnect(self.on_gps_loaded)
            except TypeError:
                pass
            try:
                old.gps_status_message.disconnect(self._on_gps_status_message)
            except TypeError:
                pass
            try:
                old.gps_roi_setup_needed.disconnect(self.on_roi_setup_needed)
            except TypeError:
                pass
        self._gps_manager = mgr
        if mgr is not None:
            mgr.gps_loaded.connect(self.on_gps_loaded)
            mgr.gps_status_message.connect(self._on_gps_status_message)
            mgr.gps_roi_setup_needed.connect(self.on_roi_setup_needed)

    def _on_gps_status_message(self, msg: str) -> None:
        self.status_message.emit(msg)

    @property
    def manager(self) -> GPSManager | None:
        return self._gps_manager

    def set_app_settings(self, settings: AppSettings) -> None:
        self._app_settings = settings

    def set_video_context(self, video_path: str, video_info: VideoInfo | None) -> None:
        self._video_path = video_path
        self._video_info = video_info

    @property
    def has_roi_setting(self) -> bool:
        if self._gps_manager is None:
            return False
        return self._gps_manager.has_roi_setting

    def is_loaded(self) -> bool:
        if self._gps_manager is None:
            return False
        return self._gps_manager.is_loaded()

    def get_track_count(self) -> int:
        if self._gps_manager is None:
            return 0
        return self._gps_manager.get_track_count()

    @property
    def has_manager(self) -> bool:
        return self._gps_manager is not None

    # ---- signal handlers (connected to GPSManager) ----

    def on_gps_loaded(self, path: str, has_gps: bool, track_count: int) -> None:
        if path != self._video_path:
            return
        self.gps_ready.emit(path, has_gps, track_count)
        msg = f"GPS loaded: {'yes' if has_gps else 'no'} ({track_count} points)"
        self.status_message.emit(msg)

    def on_roi_setup_needed(self, path: str, w: int, h: int, fmt: str) -> None:
        if path != self._video_path:
            return
        from dashcam_player.ui.dialogs.gps_roi_adjust import GPSROIAdjustDialog

        dialog = GPSROIAdjustDialog(
            path, w, h, fmt,
            self._app_settings,
            parent=None,  # GPSFacade is QObject, not QWidget
        )
        dialog.exec_()

    def open_roi_adjust_dialog(self) -> None:
        if not self._video_info:
            return
        import os

        from dashcam_player.ui.dialogs.gps_roi_adjust import GPSROIAdjustDialog

        fmt = os.path.splitext(self._video_path)[1]
        dialog = GPSROIAdjustDialog(
            self._video_path,
            self._video_info.width,
            self._video_info.height,
            fmt,
            self._app_settings,
            parent=None,  # GPSFacade is QObject, not QWidget
        )
        dialog.exec_()

    # ---- GPS query ----

    def emit_gps_position(self, sec: float) -> None:
        gps = self._gps_manager.get_position_at_time(sec) if self._gps_manager else None
        if gps:
            self.gps_position_updated.emit(gps[0], gps[1])

    def get_current_gps(self, frame_idx: int, fps: float) -> tuple | None:
        if not self._gps_manager or not self._gps_manager.has_gps_data():
            return None
        if fps <= 0:
            return None
        seconds = frame_idx / fps
        return self._gps_manager.get_position_at_time(seconds)

    def get_current_speed(self, frame_idx: int, fps: float) -> float | None:
        """取得當前幀的 GPS 速度 (km/h)，供視覺測速失效時備援。"""
        if not self._gps_manager or not self._gps_manager.has_gps_data():
            return None
        if fps <= 0:
            return None
        seconds = frame_idx / fps
        try:
            return self._gps_manager.get_speed_at_time(seconds)
        except Exception:
            return None

    # ---- GPS data access ----

    def has_gps_data(self) -> bool:
        return self._gps_manager is not None and self._gps_manager.has_gps_data()

    def get_gps_tracks(self) -> list:
        if not self.has_gps_data():
            return []
        assert self._gps_manager is not None
        self.status_message.emit("正在載入 GPS 軌跡資料...")
        tracks = self._gps_manager.extract_tracks_sync()
        if tracks:
            self.status_message.emit(f"GPS 軌跡: {len(tracks)} 個點")
        return tracks

    def get_gps_bounds(self):
        if not self.has_gps_data():
            return None
        return self._gps_manager.get_bounds()

    def export_gpx(self, output_path: str) -> bool:
        if not self.has_gps_data():
            return False
        assert self._gps_manager is not None
        return self._gps_manager.export_gpx(output_path)

    # ---- cleanup ----

    def close_gps_manager(self) -> None:
        if self._gps_manager:
            self._gps_manager.close()
            self.set_gps_manager(None)
