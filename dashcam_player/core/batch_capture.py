from __future__ import annotations

import math
import os

import cv2
from PyQt5.QtCore import QElapsedTimer, QThread, pyqtSignal

from dashcam_player.models.resolution import Resolution
from dashcam_player.utils.cv_utils import (
    apply_resolution,
    configure_hw_acceleration,
    imwrite_safe,
    overlay_gps_text,
    overlay_osm_thumbnail,
    write_exif_gps,
)
from dashcam_player.utils.ffmpeg import win32_short_path
from dashcam_player.utils.gps_parser import GPSPoint, interpolate_gps
from dashcam_player.utils.log import get_logger
from dashcam_player.utils.osm_map import get_static_map

log = get_logger()


def _compute_sharpness(frame) -> float:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.Laplacian(gray, cv2.CV_64F).var()


class BatchCaptureWorker(QThread):
    """批次截圖 worker，支援清晰度選取 + GPS 疊加"""

    progress = pyqtSignal(int, int)
    finished = pyqtSignal(int, int)
    failed = pyqtSignal(str)

    def __init__(
        self,
        video_path: str,
        start_frame: int,
        end_frame: int,
        capture_fps: int,
        fps: float,
        output_dir: str,
        prefix: str,
        img_format: str = "JPEG",
        jpeg_quality: int = 95,
        resolution: Resolution = Resolution.ORIGINAL,
        hw_backends: list[tuple[str, str]] | None = None,
        gps_data: list[GPSPoint] | None = None,
        skip_overlay: bool = False,
        parent: object | None = None,
    ) -> None:
        super().__init__(parent)
        self._video_path = video_path
        self._start_frame = start_frame
        self._end_frame = end_frame
        self._capture_fps = capture_fps
        self._fps = fps
        self._output_dir = output_dir
        self._prefix = prefix
        self._img_format = img_format
        self._jpeg_quality = jpeg_quality
        self._resolution = resolution
        self._hw_backends = hw_backends or []
        self._stop = False
        self._success_count = 0
        self._gps_data: list[GPSPoint] = gps_data or []
        self._skip_overlay: bool = skip_overlay
        self._last_gps_key: str | None = None
        self._last_osm_bytes: bytes | None = None

    def _get_gps_at_frame(self, frame_idx: int) -> tuple | None:
        """從 _gps_data 插值取得指定幀的 (lat, lon)"""
        if not self._gps_data or self._fps <= 0:
            return None
        target_time = frame_idx / self._fps
        return interpolate_gps(target_time, self._gps_data)

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        if self._start_frame < 0 or self._end_frame <= self._start_frame:
            self.finished.emit(0, 0)
            return

        os.makedirs(self._output_dir, exist_ok=True)
        is_jpeg = self._img_format == "JPEG"
        ext = "jpg" if is_jpeg else "png"

        cap = cv2.VideoCapture(win32_short_path(self._video_path), cv2.CAP_FFMPEG)
        configure_hw_acceleration(cap, self._hw_backends)

        try:
            if not cap.isOpened():
                err = f"無法開啟影片: {self._video_path}"
                log.error(err)
                self.failed.emit(err)
                return

            # 無條件捨去，確保實際取樣率不高於設定的 capture_fps
            # （round 的銀行家捨入在 fps=25, capture_fps=10 時會取 2，變成 12.5fps）
            step = max(1, int(self._fps / self._capture_fps))
            total_groups = max(1, int(math.ceil((self._end_frame - self._start_frame + 1) / step)))
            log.info(
                f"批次截圖：fps={self._fps}  capture_fps={self._capture_fps}  step={step}  總群組={total_groups}"
            )

            next_idx = self._start_frame
            processed = 0

            cap.set(cv2.CAP_PROP_POS_FRAMES, self._start_frame)

            throttle = QElapsedTimer()
            throttle.start()
            THROTTLE_MS = 100  # 限流 10Hz

            while next_idx <= self._end_frame and not self._stop:
                group_size = min(step, self._end_frame - next_idx + 1)
                frames = []
                indices = []

                for _ in range(group_size):
                    ret, frame = cap.read()
                    if not ret:
                        break
                    frames.append(frame)
                    indices.append(next_idx)
                    next_idx += 1

                if not frames:
                    break

                best_score = -1.0
                best_frame = None
                best_idx = indices[0]

                for i, frame in enumerate(frames):
                    score = _compute_sharpness(frame)
                    if score > best_score:
                        best_score = score
                        best_frame = frame
                        best_idx = indices[i]

                if best_frame is not None:
                    best_frame = apply_resolution(best_frame, self._resolution)

                    # GPS 疊加（文字 + OSM 靜態縮圖）
                    gps_coord = self._get_gps_at_frame(best_idx) if not self._skip_overlay else None
                    if gps_coord:
                        best_frame = overlay_gps_text(best_frame, gps_coord)
                        # 位置未改變時重用上次 OSM 縮圖，避免重複 HTTP 請求
                        gps_key = f"{gps_coord[0]:.4f}_{gps_coord[1]:.4f}"
                        osm_bytes: bytes | None = None
                        if gps_key == self._last_gps_key and self._last_osm_bytes is not None:
                            osm_bytes = self._last_osm_bytes
                        else:
                            osm_bytes = get_static_map(
                                gps_coord[0], gps_coord[1], zoom=15, width=160, height=160
                            )
                            self._last_gps_key = gps_key
                            self._last_osm_bytes = osm_bytes
                        if osm_bytes:
                            best_frame = overlay_osm_thumbnail(best_frame, osm_bytes)

                    filename = f"{self._prefix}_frame{best_idx:06d}.{ext}"
                    path = os.path.join(self._output_dir, filename)

                    try:
                        if is_jpeg:
                            ok = imwrite_safe(
                                path, best_frame, [int(cv2.IMWRITE_JPEG_QUALITY), self._jpeg_quality]
                            )
                        else:
                            ok = imwrite_safe(path, best_frame, [int(cv2.IMWRITE_PNG_COMPRESSION), 3])
                        if ok:
                            # EXIF GPS 寫入（無論是否疊加文字）
                            if gps_coord:
                                write_exif_gps(path, gps_coord)
                            self._success_count += 1
                        else:
                            log.warning(f"寫入失敗: {path}")
                    except Exception:
                        log.warning(f"寫入異常: {path}")

                processed += 1
                if throttle.elapsed() >= THROTTLE_MS or processed >= total_groups:
                    self.progress.emit(processed, total_groups)
                    throttle.start()

            # 確保進度條到達 100%
            if processed < total_groups:
                self.progress.emit(total_groups, total_groups)
            self.finished.emit(self._success_count, total_groups)
        finally:
            cap.release()
