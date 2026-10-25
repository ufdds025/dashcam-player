from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt5.QtCore import QObject, pyqtSignal

if TYPE_CHECKING:
    import numpy as np

    from dashcam_player.models.resolution import Resolution

from dashcam_player.config.settings import AppSettings
from dashcam_player.core.gps_manager import GPSManager
from dashcam_player.core.managers.capture_manager import CaptureManager
from dashcam_player.core.managers.export_manager import ExportManager
from dashcam_player.core.managers.gps_facade import GPSFacade
from dashcam_player.core.managers.playback_manager import PlaybackManager
from dashcam_player.core.managers.speed_manager import SpeedManager
from dashcam_player.core.video_worker import VideoWorker
from dashcam_player.models.capture_settings import CaptureSettings, SpeedSettings
from dashcam_player.models.video_info import VideoInfo
from dashcam_player.utils.cv_utils import detect_hw_backends
from dashcam_player.utils.filename_utils import source_stem
from dashcam_player.utils.log import get_logger
from dashcam_player.utils.time_utils import parse_time_input

log = get_logger()


class Controller(QObject):
    """薄 Orchestrator：持有各 Manager，轉發 signal，對外 API。

    VideoPlayer 僅經由 Controller 存取所有功能。
    """

    # ==================================================================
    # Signals（供 VideoPlayer 連接）
    # ==================================================================

    video_loaded = pyqtSignal(object)  # (VideoInfo)
    load_failed = pyqtSignal(str)
    error_occurred = pyqtSignal(str, str)  # (title, message) — 批次/儲存等非載入失敗的錯誤
    batch_progress = pyqtSignal(int, int)
    batch_finished = pyqtSignal(int, int)
    status_message = pyqtSignal(str)
    export_finished = pyqtSignal(bool, str)
    gps_position_updated = pyqtSignal(float, float)
    gps_ready = pyqtSignal(str, bool, int)  # (video_path, has_gps, track_count)
    frame_ready = pyqtSignal(object, int)  # (frame_bgr, frame_index)
    position_updated = pyqtSignal(float, int)  # (seconds, frame_index)
    play_finished = pyqtSignal()

    # Speed 相關
    speed_updated = pyqtSignal(float, float, float, str)  # (time, speed_kmh, confidence, source)
    speed_ready = pyqtSignal(str, object)  # (video_path, SpeedTrack)

    # ==================================================================
    # 初始化
    # ==================================================================

    def __init__(
        self,
        video_path: str = "",
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._video_path: str = video_path
        self.video_info: VideoInfo | None = None
        self._capture_start_frame: int = -1
        self._capture_end_frame: int = -1
        self._shutting_down: bool = False

        self._hw_backends = detect_hw_backends()
        self._app_settings = AppSettings()
        self._settings = self._app_settings.load_capture_settings()

        # Workers
        self._worker = VideoWorker(hw_backends=self._hw_backends)

        # Managers
        self._playback = PlaybackManager(self._worker, parent=self)
        self._capture = CaptureManager(self._worker, self._hw_backends, parent=self)
        self._export = ExportManager(parent=self)
        self._gps = GPSFacade(parent=self)
        self._gps.set_app_settings(self._app_settings)
        self._speed = SpeedManager(parent=self)
        self._speed.set_app_settings(self._app_settings)

        # Worker 輸出信號 → PlaybackManager（frame/position/video_info 轉發鏈）
        self._playback.connect_worker_signals()

        self._connect_manager_signals()

    def _connect_manager_signals(self) -> None:
        # Worker → Controller relay (load_failed 直接轉發)
        self._worker.load_failed.connect(self.load_failed)
        self._worker.error_occurred.connect(self.error_occurred)

        # PlaybackManager → Controller relay
        self._playback.frame_ready.connect(self._on_frame_ready)
        self._playback.position_updated.connect(self._on_position_changed)
        self._playback.video_info_ready.connect(self._on_video_info_ready)
        self._playback.play_finished.connect(self._on_play_finished)

        # CaptureManager → Controller relay
        self._capture.batch_progress.connect(self.batch_progress)
        self._capture.batch_finished.connect(self.batch_finished)
        self._capture.error_occurred.connect(self.error_occurred)
        self._capture.status_message.connect(self.status_message)

        # ExportManager → Controller relay
        self._export.export_finished.connect(self.export_finished)
        self._export.status_message.connect(self.status_message)

        # GPSFacade → Controller relay
        self._gps.gps_position_updated.connect(self.gps_position_updated)
        self._gps.gps_ready.connect(self.gps_ready)
        self._gps.status_message.connect(self.status_message)

        # SpeedManager → Controller relay
        self._speed.speed_updated.connect(self.speed_updated)
        self._speed.speed_ready.connect(self.speed_ready)
        self._speed.status_message.connect(self.status_message)
        self._speed.error_occurred.connect(self.error_occurred)

    # ==================================================================
    # 內部 signal handlers（轉發至 Controller signals）
    # ==================================================================

    def _on_frame_ready(self, frame: np.ndarray, idx: int) -> None:
        if self._shutting_down:
            return
        self.frame_ready.emit(frame, idx)

    def _on_video_info_ready(self, info: VideoInfo) -> None:
        if self._shutting_down:
            return
        info.path = self._video_path
        self.video_info = info
        self._capture.set_video_context(self._video_path, info)
        self._export.set_video_context(self._video_path, info)
        self._gps.set_video_context(self._video_path, info)
        self._speed.set_video_context(self._video_path, info)
        self.video_loaded.emit(self.video_info)

    def _on_position_changed(self, sec: float, frame_idx: int) -> None:
        if self._shutting_down:
            return
        self.position_updated.emit(sec, frame_idx)
        # GPS throttling
        if self._playback.increment_gps_throttle():
            self._gps.emit_gps_position(sec)

    def _on_play_finished(self) -> None:
        if self._shutting_down:
            return
        self.play_finished.emit()
        self.status_message.emit("Playback finished")

    # ==================================================================
    # 影片開啟與關閉
    # ==================================================================

    def open_video(self, path: str) -> None:
        self._export.cancel_export()
        self._capture.stop_batch()
        self._gps.close_gps_manager()
        self._speed.stop_scan()  # 停止速度掃描

        if self._worker.isRunning():
            self._worker.close()
            self._worker.wait(3000)
            if self._worker.isRunning():
                log.warning("VideoWorker not stopped within 3s, terminating")
                self._worker.terminate()
                self._worker.wait(1000)

        self._video_path = path
        self.video_info = None
        # 立即設定 facade 的 video context，避免 GPS 背景載入先於 video_info_ready 完成時，
        # on_gps_loaded 的 path 比對誤判為過期結果而丟棄（video_info 稍後由 _on_video_info_ready 更新）
        self._gps.set_video_context(path, None)
        self._speed.set_video_context(path, None)
        # 同步 SpeedManager 的 GPS 引用（供後續校準/GPS 備援），避免懸空
        try:
            self._speed.set_gps_manager(self._gps_manager if hasattr(self, '_gps') else None)  # type: ignore[attr-defined]
        except Exception:
            pass
        self._capture_start_frame = -1
        self._capture_end_frame = -1
        self._playback.reset_gps_throttle()

        self._worker.open(path)

        self._gps_manager = GPSManager(
            path, skip_ocr=False, app_settings=self._app_settings, parent=self,
        )
        self._gps_manager.start_background_load(run_ocr=False)
        # 保持 SpeedManager 與 GPSManager 同步（供視覺/GPS 融合）
        try:
            self._speed.set_gps_manager(self._gps_manager)
        except Exception:
            pass

        # 同步 SpeedManager 設定 — 尊重持久化設定（預設省資源，手動開啟）
        # 若使用者曾關閉速度/地圖，開啟新影片不自動覆蓋。
        self._speed.set_speed_settings(self._settings.speed)
        if self._settings.speed.enabled:
            self._speed.start_scan()
        else:
            log.info("視覺測速已關閉（尊重持久化設定），跳過自動掃描")

    # ==================================================================
    # 唯讀 properties（delegation）
    # ==================================================================

    @property
    def total_frames(self) -> int:
        return self.video_info.frame_count if self.video_info else 0

    @property
    def fps(self) -> float:
        return self.video_info.fps if self.video_info else 30.0

    @property
    def duration(self) -> float:
        return self.video_info.duration if self.video_info else 0.0

    @property
    def is_playing(self) -> bool:
        return self._playback.is_playing

    @property
    def capture_settings(self) -> CaptureSettings:
        return self._settings

    @property
    def settings(self) -> AppSettings:
        return self._app_settings

    @property
    def current_frame(self) -> np.ndarray | None:
        return self._playback.current_frame

    @property
    def capture_start_frame(self) -> int:
        return self._capture_start_frame

    @capture_start_frame.setter
    def capture_start_frame(self, val: int) -> None:
        self._capture_start_frame = val

    @property
    def capture_end_frame(self) -> int:
        return self._capture_end_frame

    @capture_end_frame.setter
    def capture_end_frame(self, val: int) -> None:
        self._capture_end_frame = val

    @property
    def hw_backends(self) -> list[tuple[str, str]]:
        return self._hw_backends

    @property
    def video_path(self) -> str:
        return self._video_path

    @property
    def speed_settings(self) -> SpeedSettings:  # type: ignore[return-value]
        return self._speed.speed_settings  # type: ignore[return-value]

    def get_current_speed(self, frame_idx: int) -> tuple[float | None, str]:
        """取得當前幀的測速結果 (km/h, source)

        優先回傳 GPS 速度；若 GPS 無結果則備援視覺測速，
        確保「有 GPS 時以 GPS 為準」的顯示策略。
        """
        # 優先：GPS 速度（NMEA 節轉 km/h，已在 GPSManager 處理）
        try:
            gps_speed = self._gps.get_current_speed(frame_idx, self.fps)
            if gps_speed is not None and gps_speed > 0:
                return gps_speed, "gps"
        except Exception:
            pass
        # 備援：視覺測速
        vision_speed, source = self._speed.get_current_speed(frame_idx, self.fps)
        if vision_speed is not None and vision_speed > 0:
            return vision_speed, source
        # 若 vision 有值但為 0，仍回傳 vision 以避免 GPS 0 覆蓋
        return vision_speed, source

    def get_detection_debug(self) -> dict:
        """取得視覺測速偵測除錯資訊"""
        return self._speed.get_detection_debug()

    def get_speed_roi(self) -> tuple[float, float, float, float] | None:
        """取得目前使用的 speed ROI (relative)"""
        return self._speed.current_roi

    def start_speed_scan(self) -> None:
        """啟動視覺測速掃描"""
        self._speed.start_scan()

    def stop_speed_scan(self) -> None:
        """停止視覺測速掃描"""
        self._speed.stop_scan()

    @property
    def has_roi_setting(self) -> bool:
        return self._gps.has_roi_setting

    @property
    def has_gps_manager(self) -> bool:
        return self._gps.has_manager

    def gps_is_loaded(self) -> bool:
        return self._gps.is_loaded()

    def gps_track_count(self) -> int:
        return self._gps.get_track_count()

    @property
    def _gps_manager(self) -> GPSManager | None:
        return self._gps.manager

    @_gps_manager.setter
    def _gps_manager(self, mgr: GPSManager | None) -> None:
        self._gps.set_gps_manager(mgr)
        self._capture.set_gps_manager(mgr)

    # ==================================================================
    # 播放控制（delegation）
    # ==================================================================

    def toggle_play(self) -> bool:
        return self._playback.toggle_play()

    def seek(self, frame_idx: int) -> None:
        self._playback.seek(frame_idx)

    def seek_to(self, frame_idx: int) -> None:
        self._playback.seek_to(frame_idx)

    def skip(self, seconds: float) -> None:
        self._playback.skip(seconds)

    def set_playback_speed(self, speed: float) -> None:
        self._playback.set_playback_speed(speed)

    def step_forward(self) -> None:
        self._playback.step_forward()

    def step_backward(self) -> None:
        self._playback.step_backward()

    def get_current_frame_index(self) -> int:
        return self._playback.get_current_frame_index()

    def get_current_time(self) -> float:
        return self._playback.get_current_time()

    # ==================================================================
    # 驗證方法
    # ==================================================================

    def validate_batch_range(self) -> str | None:
        return self._capture.validate_batch_range(
            self.total_frames, self.capture_start_frame, self.capture_end_frame
        )

    def check_batch_conflict(
        self, out_dir: str, prefix: str, ext: str, src_stem: str
    ) -> list[str]:
        return self._capture.check_batch_conflict(out_dir, prefix, ext, src_stem)

    @property
    def current_source_stem(self) -> str:
        """目前來源影片的淨化識別名（供檔名與衝突比對使用）。"""
        return source_stem(self._video_path)

    def validate_export_times(
        self, start_time: str, end_time: str
    ) -> tuple[float | None, float | None, str | None]:
        if self.total_frames <= 0:
            return None, None, "請先開啟影片"

        if start_time and end_time:
            start_sec = parse_time_input(start_time)
            end_sec = parse_time_input(end_time)
            if start_sec is None or end_sec is None:
                return None, None, "時間格式錯誤，請使用 hh:mm:ss 格式"
            if start_sec >= end_sec:
                return None, None, "開始時間必須小於結束時間"
            return start_sec, end_sec, None

        if self.capture_start_frame >= 0 and self.capture_end_frame >= 0:
            if self.capture_start_frame >= self.capture_end_frame:
                return None, None, "開始標記必須小於結束標記"
            start_sec = self.capture_start_frame / self.fps
            end_sec = self.capture_end_frame / self.fps
            return start_sec, end_sec, None

        return None, None, "請先設定開始與結束標記，或輸入時間範圍（hh:mm:ss）"

    # ==================================================================
    # 截圖（delegation）
    # ==================================================================

    def capture_single(
        self,
        out_dir: str,
        prefix: str,
        img_format: str,
        quality: int,
        resolution: Resolution,
        frame_idx: int,
    ) -> str | None:
        if not self.video_info:
            return None
        frame = self.current_frame
        if frame is None:
            return None
        return self._capture.capture_single(
            frame, out_dir, prefix, img_format, quality, resolution,
            frame_idx, self.fps, self._gps_manager,
        )

    def start_batch_capture(
        self,
        start_frame: int,
        end_frame: int,
        capture_fps: int,
        settings: CaptureSettings,
        original_fps: float,
    ) -> None:
        self._capture.start_batch_capture(
            start_frame, end_frame, capture_fps, settings,
            original_fps, self._gps_manager,
        )

    # ==================================================================
    # 匯出（delegation）
    # ==================================================================

    def start_video_export(
        self,
        start_sec: float,
        end_sec: float,
        settings: CaptureSettings,
        preserve_gps: bool = True,
    ) -> None:
        self._export.set_video_context(self._video_path, self.video_info)
        corrected = self._export.start_video_export(
            start_sec, end_sec, settings, preserve_gps
        )
        if corrected is not None:
            self._settings = corrected

    def stop_export(self) -> None:
        self._export.stop_export()

    def _cancel_export(self) -> None:
        self._export.cancel_export()

    # ==================================================================
    # GPS（delegation）
    # ==================================================================

    def open_roi_adjust_dialog(self) -> None:
        self._gps.open_roi_adjust_dialog()

    def has_gps_data(self) -> bool:
        return self._gps.has_gps_data()

    def get_gps_tracks(self) -> list:
        return self._gps.get_gps_tracks()

    def get_gps_bounds(self):
        return self._gps.get_gps_bounds()

    def start_gps_ocr_detection(self) -> None:
        """手動觸發 OCR GPS 偵測（使用者開啟「顯示 GPS 軌跡」時呼叫）"""
        if self._gps_manager is not None:
            self._gps_manager.start_ocr_detection()

    def export_gpx(self, output_path: str) -> bool:
        return self._gps.export_gpx(output_path)

    # ==================================================================
    # 設定
    # ==================================================================

    def update_capture_settings(self, new_settings: CaptureSettings) -> None:
        self._settings = new_settings

    # ==================================================================
    # 清除
    # ==================================================================

    def shutdown(self) -> None:
        # 先設定關閉旗標，阻止後續信號處理
        self._shutting_down = True
        # 斷開 Manager 到 Controller 的信號連接，防止關閉過程中仍有訊號觸發
        try:
            self._gps.gps_position_updated.disconnect(self.gps_position_updated)
        except TypeError:
            pass
        try:
            self._gps.gps_ready.disconnect(self.gps_ready)
        except TypeError:
            pass
        try:
            self._gps.status_message.disconnect(self.status_message)
        except TypeError:
            pass
        try:
            self._speed.speed_updated.disconnect(self.speed_updated)
        except TypeError:
            pass
        try:
            self._speed.speed_ready.disconnect(self.speed_ready)
        except TypeError:
            pass
        try:
            self._speed.status_message.disconnect(self.status_message)
        except TypeError:
            pass
        try:
            self._speed.error_occurred.disconnect(self.error_occurred)
        except TypeError:
            pass

        self._worker.close()
        if not self._worker.wait(3000):
            log.warning("VideoWorker 未在 3 秒內停止，強制終止")
            self._worker.terminate()
            self._worker.wait(1000)
        self._capture.stop_batch()
        self._export.cancel_export()
        self._gps.close_gps_manager()
        # 同步清空 CaptureManager 的殘留引用，避免單張/GPS 疊加拿到已關閉的 manager
        try:
            self._capture.set_gps_manager(None)
        except Exception:
            pass
        self._speed.close()
