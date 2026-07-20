"""GPSFacade — GPS 查詢/疊加/ROI 對話職責。

封裝 GPSManager 的查詢、疊加、匯出功能，
Controller 透過此 Facade 與 GPS 系統互動。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt5.QtCore import QObject, pyqtSignal

if TYPE_CHECKING:
    import numpy as np

    from dashcam_player.config.settings import AppSettings
    from dashcam_player.core.gps_manager import GPSManager
    from dashcam_player.models.video_info import VideoInfo

from dashcam_player.utils.cv_utils import (
    overlay_gps_text as _overlay_gps_text_shared,
)
from dashcam_player.utils.cv_utils import (
    overlay_osm_thumbnail,
)
from dashcam_player.utils.log import get_logger
from dashcam_player.utils.osm_map import get_static_map

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
        self._gps_manager = mgr

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
            parent=self.parent(),  # type: ignore[arg-type]
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
            parent=self.parent(),  # type: ignore[arg-type]
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

    def overlay_gps_text(self, frame: np.ndarray, gps_coord: tuple) -> np.ndarray:
        lat, lon = gps_coord
        frame = _overlay_gps_text_shared(frame, gps_coord)
        osm_bytes = get_static_map(lat, lon, zoom=15, width=160, height=160)
        if osm_bytes:
            frame = overlay_osm_thumbnail(frame, osm_bytes)
        return frame

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
            self._gps_manager = None
