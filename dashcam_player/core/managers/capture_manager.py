"""CaptureManager — 截圖職責。

管理單張截圖 + 批次截圖（BatchCaptureWorker），
包含驗證邏輯（validate_batch_range / check_batch_conflict）。
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import cv2
from PyQt5.QtCore import QObject, pyqtSignal

if TYPE_CHECKING:
    import numpy as np

    from dashcam_player.core.batch_capture import BatchCaptureWorker
    from dashcam_player.core.gps_manager import GPSManager
    from dashcam_player.core.video_worker import VideoWorker
    from dashcam_player.models.capture_settings import CaptureSettings
    from dashcam_player.models.resolution import Resolution
    from dashcam_player.models.video_info import VideoInfo

from dashcam_player.utils.cv_utils import (
    apply_resolution,
    imwrite_safe,
    write_exif_gps,
)
from dashcam_player.utils.log import get_logger

log = get_logger()


class CaptureManager(QObject):
    """截圖管理器：單張 + 批次截圖。"""

    batch_progress = pyqtSignal(int, int)
    batch_finished = pyqtSignal(int, int)
    status_message = pyqtSignal(str)
    error_occurred = pyqtSignal(str, str)  # (title, message)

    def __init__(
        self,
        worker: VideoWorker,
        hw_backends: list[tuple[str, str]],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._worker = worker
        self._hw_backends = hw_backends
        self._batch_worker: BatchCaptureWorker | None = None
        self._gps_manager: GPSManager | None = None
        self._video_path: str = ""
        self._video_info: VideoInfo | None = None

    def set_gps_manager(self, mgr: GPSManager | None) -> None:
        self._gps_manager = mgr

    def set_video_context(self, video_path: str, video_info: VideoInfo | None) -> None:
        self._video_path = video_path
        self._video_info = video_info

    # ---- validation ----

    def validate_batch_range(
        self, total_frames: int, start_frame: int, end_frame: int
    ) -> str | None:
        if total_frames <= 0:
            return "請先開啟影片"
        if start_frame < 0 or end_frame < 0 or start_frame >= end_frame:
            return "請先正確設定開始與結束標記（開始 < 結束）"
        return None

    def check_batch_conflict(self, out_dir: str, prefix: str, ext: str) -> list[str]:
        """列出與批次截圖會衝突的既有檔案。

        批次命名為 ``{prefix}_frame{idx:06d}.{ext}``，單張為 ``{prefix}_{timestamp}.{ext}``，
        兩者皆以 ``{prefix}_`` 開頭，故以此為比對基準。
        """
        if not os.path.isdir(out_dir):
            return []
        return [
            f
            for f in os.listdir(out_dir)
            if f.startswith(f"{prefix}_") and f.endswith(f".{ext}")
        ]

    # ---- single capture ----

    def capture_single(
        self,
        frame: np.ndarray,
        out_dir: str,
        prefix: str,
        img_format: str,
        quality: int,
        resolution: Resolution,
        frame_idx: int,
        fps: float,
        gps_manager: GPSManager | None = None,
    ) -> str | None:
        res_str = resolution.value if hasattr(resolution, "value") else str(resolution)
        frame_out = apply_resolution(frame, res_str)
        gps_coord = self._get_gps_at_frame(frame_idx, fps, gps_manager)
        os.makedirs(out_dir or os.path.abspath("captures"), exist_ok=True)
        out_dir = out_dir or os.path.abspath("captures")
        ts = time.strftime("%Y%m%d_%H%M%S")
        ext = "jpg" if img_format == "JPEG" else "png"
        fname = f"{prefix}_{ts}.{ext}"
        path = os.path.join(out_dir, fname)
        params = []
        if img_format == "JPEG":
            params = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
        ok = imwrite_safe(path, frame_out, params)
        if not ok:
            self.error_occurred.emit("儲存失敗", f"無法寫入圖片:\n{path}")
            return None
        if gps_coord:
            try:
                write_exif_gps(path, gps_coord)
            except Exception as e:
                log.warning(f"寫入 EXIF GPS 失敗: {e}（{path}）")
        self.status_message.emit(f"Capture saved: {fname}")
        return path

    # ---- batch capture ----

    def start_batch_capture(
        self,
        start_frame: int,
        end_frame: int,
        capture_fps: int,
        settings: CaptureSettings,
        original_fps: float,
        gps_manager: GPSManager | None = None,
    ) -> None:
        if start_frame < 0 or end_frame < 0 or start_frame >= end_frame:
            return
        if capture_fps > original_fps:
            capture_fps = int(original_fps)

        if self._batch_worker is not None and self._batch_worker.isRunning():
            log.warning("批次截圖已在執行中，忽略重複請求")
            self.status_message.emit("批次截圖已在執行中")
            return

        gps_data: list = []
        if gps_manager and gps_manager.is_loaded():
            if gps_manager.has_gps_data():
                gps_data = list(gps_manager.extract_tracks_sync())
                self.status_message.emit(f"GPS tracks loaded: {len(gps_data)} points")
            else:
                self.status_message.emit("No GPS data, batch capture will skip GPS overlay")
        elif gps_manager:
            self.status_message.emit("GPS still loading, batch capture will skip GPS overlay")

        from dashcam_player.core.batch_capture import BatchCaptureWorker

        self._batch_worker = BatchCaptureWorker(
            video_path=self._video_path,
            start_frame=start_frame,
            end_frame=end_frame,
            capture_fps=capture_fps,
            fps=original_fps,
            output_dir=settings.output_dir or os.path.abspath("captures"),
            prefix=settings.prefix,
            img_format=settings.img_format,
            jpeg_quality=settings.jpeg_quality,
            resolution=settings.resolution,
            hw_backends=self._hw_backends,
            gps_data=gps_data,
            skip_overlay=settings.skip_gps_overlay,
        )
        self._batch_worker.progress.connect(self._on_batch_progress)
        self._batch_worker.finished.connect(self._on_batch_finished)
        self._batch_worker.failed.connect(self._on_batch_failed)
        if self._worker.isRunning():
            self._worker.release_cap_async()
        self._batch_worker.start()

    def stop_batch(self) -> None:
        if self._batch_worker and self._batch_worker.isRunning():
            self._safe_stop_worker(
                self._batch_worker,
                [self._batch_worker.progress, self._batch_worker.finished, self._batch_worker.failed],
            )
            self._batch_worker = None

    def _on_batch_progress(self, current: int, total: int) -> None:
        self.batch_progress.emit(current, total)

    def _on_batch_finished(self, success: int, total: int) -> None:
        log.info(f"批次截圖完成: {success}/{total}")
        self.batch_finished.emit(success, total)
        self._batch_worker = None

    def _on_batch_failed(self, msg: str) -> None:
        self.error_occurred.emit("批次截圖失敗", msg)
        self._batch_worker = None

    # ---- helpers ----

    def _get_gps_at_frame(
        self, frame_idx: int, fps: float, gps_manager: GPSManager | None
    ) -> tuple | None:
        if not gps_manager or not gps_manager.has_gps_data():
            return None
        if fps <= 0:
            return None
        seconds = frame_idx / fps
        return gps_manager.get_position_at_time(seconds)

    def _safe_stop_worker(self, worker, signals: list, timeout: int = 5000) -> None:
        if worker and worker.isRunning():
            worker.stop()
            for sig in signals:
                try:
                    sig.disconnect()
                except TypeError:
                    pass
            worker.wait(timeout)
