from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

import cv2
from PyQt5.QtCore import QObject, pyqtSignal

if TYPE_CHECKING:
    import numpy as np

    from dashcam_player.models.resolution import Resolution

from dashcam_player.config.settings import AppSettings
from dashcam_player.core.batch_capture import BatchCaptureWorker
from dashcam_player.core.export_worker import ExportWorker
from dashcam_player.core.gps_manager import GPSManager
from dashcam_player.core.video_worker import VideoWorker
from dashcam_player.models.capture_settings import CaptureSettings
from dashcam_player.models.video_info import VideoInfo
from dashcam_player.utils.cv_utils import (
    detect_hw_backends,
    imwrite_safe,
    overlay_osm_thumbnail,
    write_exif_gps,
)
from dashcam_player.utils.cv_utils import (
    overlay_gps_text as _overlay_gps_text_shared,
)
from dashcam_player.utils.ffmpeg import (
    build_export_cmd,
    detect_input_format,
    find_ffmpeg,
    probe_media_info,
    suggest_container,
)
from dashcam_player.utils.log import get_logger
from dashcam_player.utils.osm_map import get_static_map
from dashcam_player.utils.time_utils import parse_time_input

log = get_logger()


class Controller(QObject):
    """Controller 層：負責 VideoPlayer (UI) 與 VideoWorker / ExportWorker / BatchCaptureWorker 之間的所有邏輯。
    VideoPlayer 僅負責 UI 佈局與事件綁定，所有「動作」委託給 Controller。
    """


    # ==================================================================
    # Signals
    # ==================================================================

    video_loaded = pyqtSignal(object)  # (VideoInfo)
    load_failed = pyqtSignal(str)
    batch_progress = pyqtSignal(int, int)
    batch_finished = pyqtSignal(int, int)
    status_message = pyqtSignal(str)
    export_finished = pyqtSignal(bool, str)
    gps_position_updated = pyqtSignal(float, float)
    gps_ready = pyqtSignal(str, bool, int)  # (video_path, has_gps, track_count)
    frame_ready = pyqtSignal(object, int)  # (frame_bgr, frame_index)
    position_updated = pyqtSignal(float, int)  # (seconds, frame_index)
    play_finished = pyqtSignal()

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
        self._current_idx: int = 0
        self._current_frame: np.ndarray | None = None
        self._playing: bool = False
        self.video_info: VideoInfo | None = None
        self._capture_start_frame: int = -1
        self._capture_end_frame: int = -1
        self._gps_throttle_counter: int = 0
        self._gps_throttle_interval: int = 5
        self._gps_manual_seek: bool = False

        self._hw_backends = detect_hw_backends()
        self._app_settings = AppSettings()
        self._settings = self._app_settings.load_capture_settings()
        self._export_params: dict = {}
        self._export_worker: ExportWorker | None = None
        self._batch_worker: BatchCaptureWorker | None = None
        self.gps_manager: GPSManager | None = None

        self.worker = VideoWorker(hw_backends=self._hw_backends)
        self._connect_worker_signals()

    def _connect_worker_signals(self) -> None:
        self.worker.frame_ready.connect(self._on_frame_ready)
        self.worker.position_changed.connect(self._on_position_changed)
        self.worker.load_failed.connect(self.load_failed)
        self.worker.load_finished.connect(self._on_play_finished)
        self.worker.video_info_ready.connect(self._on_video_info_ready)

    # ==================================================================
    # 影片開啟與關閉
    # ==================================================================

    def open_video(self, path: str) -> None:
        self._cancel_export()
        self._stop_batch()
        if self.gps_manager:
            self._close_gps_manager()

        if self.worker.isRunning():
            self.worker.close()
            self.worker.wait(3000)
            if self.worker.isRunning():
                log.warning("VideoWorker not stopped within 3s, terminating")
                self.worker.terminate()
                self.worker.wait(1000)

        self._video_path = path
        self._current_idx = 0
        self._playing = False
        self.video_info = None
        self._gps_throttle_counter = 0
        self._capture_start_frame = -1
        self._capture_end_frame = -1

        self.worker.open(path)
        self.gps_manager = GPSManager(
            path, skip_ocr=True, app_settings=self._app_settings, parent=self,
        )
        self.gps_manager.gps_loaded.connect(self._on_gps_loaded)
        self.gps_manager.gps_status_message.connect(self.status_message)
        self.gps_manager.gps_roi_setup_needed.connect(self._on_roi_setup_needed)
        self.gps_manager.start_background_load()

    def _close_gps_manager(self) -> None:
        if self.gps_manager:
            self.gps_manager.close()
            self.gps_manager = None

    def _on_gps_loaded(self, path: str, has_gps: bool, track_count: int) -> None:
        if path != self._video_path:
            return
        self.gps_ready.emit(path, has_gps, track_count)
        msg = f"GPS loaded: {'yes' if has_gps else 'no'} ({track_count} points)"
        self.status_message.emit(msg)

    def _on_roi_setup_needed(self, path: str, w: int, h: int, fmt: str) -> None:
        """ROI 自動偵測失敗，顯示手動調整對話框"""
        from dashcam_player.ui.dialogs.gps_roi_adjust import GPSROIAdjustDialog

        dialog = GPSROIAdjustDialog(
            path, w, h, fmt,
            self._app_settings,
            parent=self.parent(),
        )
        dialog.exec_()

    def open_roi_adjust_dialog(self) -> None:
        """手動開啟 ROI 調整對話框（從選單觸發）"""
        if not self.video_info:
            return
        import os

        from dashcam_player.ui.dialogs.gps_roi_adjust import GPSROIAdjustDialog

        fmt = os.path.splitext(self._video_path)[1]
        dialog = GPSROIAdjustDialog(
            self._video_path,
            self.video_info.width,
            self.video_info.height,
            fmt,
            self._app_settings,
            parent=self.parent(),
        )
        dialog.exec_()

    # ==================================================================
    # Worker signal handlers
    # ==================================================================

    def _on_frame_ready(self, frame: np.ndarray, idx: int) -> None:
        self._current_frame = frame
        self._current_idx = idx
        self.frame_ready.emit(frame, idx)

    def _on_video_info_ready(self, info: VideoInfo) -> None:
        info.path = self._video_path
        self.video_info = info
        self.video_loaded.emit(self.video_info)

    def _on_position_changed(self, sec: float, frame_idx: int) -> None:
        self._current_idx = frame_idx
        self.position_updated.emit(sec, frame_idx)
        # GPS 位置更新（節流：每 N 幀查詢一次，避免高頻插值拖慢播放）
        self._gps_throttle_counter += 1
        if self._gps_throttle_counter >= self._gps_throttle_interval or self._gps_manual_seek:
            self._gps_throttle_counter = 0
            self._gps_manual_seek = False
            self._emit_gps_position(sec)

    def _on_play_finished(self) -> None:
        self._playing = False
        self.play_finished.emit()
        self.status_message.emit("Playback finished")

    # ==================================================================
    # 播放控制
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
        return self._playing

    @property
    def capture_settings(self) -> CaptureSettings:
        return self._settings

    @property
    def settings(self) -> AppSettings:
        return self._app_settings

    @property
    def current_frame(self) -> np.ndarray | None:
        """唯讀：目前顯示的 frame（numpy ndarray 或 None）。"""
        return self._current_frame

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

    def toggle_play(self) -> bool:
        if not self.video_info:
            return False
        # 影片播畢後按播放，從頭開始
        if self._current_idx >= self.total_frames - 1:
            self.seek(0)
        self._playing = not self._playing
        self.worker.set_play(self._playing)
        return self._playing

    def seek(self, frame_idx: int) -> None:
        self._gps_manual_seek = True
        self._gps_throttle_counter = 0
        self.worker.seek(frame_idx)

    def seek_to(self, frame_idx: int) -> None:
        self.seek(frame_idx)

    def skip(self, seconds: float) -> None:
        if self.fps <= 0:
            return
        delta = int(seconds * self.fps)
        target = max(0, min(self.total_frames - 1, self._current_idx + delta))
        self.seek(target)

    def set_playback_speed(self, speed: float) -> None:
        self.worker.set_target_fps(self.fps * speed)

    def step_forward(self) -> None:
        self._gps_manual_seek = True
        self._gps_throttle_counter = 0
        self.worker.step_forward()

    def step_backward(self) -> None:
        self._gps_manual_seek = True
        self._gps_throttle_counter = 0
        self.worker.step_backward()

    def get_current_frame_index(self) -> int:
        return self._current_idx

    def get_current_time(self) -> float:
        return self._current_idx / self.fps if self.fps > 0 else 0.0

    # ==================================================================
    # 驗證方法（供 VideoPlayer 呼叫，UI 彈窗留在 VideoPlayer）
    # ==================================================================

    def validate_batch_range(self) -> str | None:
        """驗證批次截圖的標記範圍，回傳錯誤訊息（None = 通過）。"""
        if self.total_frames <= 0:
            return "請先開啟影片"
        start = self._capture_start_frame
        end = self._capture_end_frame
        if start < 0 or end < 0 or start >= end:
            return "請先正確設定開始與結束標記（開始 < 結束）"
        return None

    def check_batch_conflict(self, out_dir: str, prefix: str, ext: str) -> list[str]:
        """回傳與現有檔案衝突的清單（空 = 無衝突）。"""
        if not os.path.isdir(out_dir):
            return []
        return [
            f
            for f in os.listdir(out_dir)
            if f.startswith(f"{prefix}_frame") and f.endswith(f".{ext}")
        ]

    def validate_export_times(
        self, start_time: str, end_time: str
    ) -> tuple[float | None, float | None, str | None]:
        """驗證影片匯出時間範圍。

        回傳 (start_sec, end_sec, None) 成功，
        或 (None, None, error_msg) 失敗。
        """
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

        if self._capture_start_frame >= 0 and self._capture_end_frame >= 0:
            if self._capture_start_frame >= self._capture_end_frame:
                return None, None, "開始標記必須小於結束標記"
            start_sec = self._capture_start_frame / self.fps
            end_sec = self._capture_end_frame / self.fps
            return start_sec, end_sec, None

        return None, None, "請先設定開始與結束標記，或輸入時間範圍（hh:mm:ss）"

    # ==================================================================
    # 單張截圖
    # ==================================================================

    def capture_single(
        self,
        frame: np.ndarray,
        out_dir: str,
        prefix: str,
        img_format: str,
        quality: int,
        resolution: Resolution,
        frame_idx: int,
    ) -> str | None:
        if not self.video_info:
            return None
        from dashcam_player.utils.cv_utils import apply_resolution

        res_str = resolution.value if hasattr(resolution, "value") else str(resolution)
        frame_out = apply_resolution(frame, res_str)
        gps_coord = self._get_current_gps(frame_idx)
        os.makedirs(out_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        ext = "jpg" if img_format == "JPEG" else "png"
        fname = f"{prefix}_{ts}.{ext}"
        path = os.path.join(out_dir, fname)
        params = []
        if img_format == "JPEG":
            params = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
        ok = imwrite_safe(path, frame_out, params)
        if not ok:
            self.load_failed.emit(f"Save failed: {path}")
            return None
        if gps_coord:
            try:
                write_exif_gps(path, gps_coord)
            except Exception:
                pass
        self.status_message.emit(f"Capture saved: {fname}")
        return path

    # ==================================================================
    # 批次截圖
    # ==================================================================

    def start_batch_capture(
        self,
        start_frame: int,
        end_frame: int,
        capture_fps: int,
        settings: CaptureSettings,
        original_fps: float,
    ) -> None:
        if start_frame < 0 or end_frame < 0 or start_frame >= end_frame:
            return

        if capture_fps > original_fps:
            capture_fps = int(original_fps)

        # 準備 GPS 資料給 worker 使用
        gps_data: list = []
        if self.gps_manager and self.gps_manager.is_loaded():
            if self.gps_manager.has_gps_data():
                gps_data = list(self.gps_manager.extract_tracks_sync())
                self.status_message.emit(f"GPS tracks loaded: {len(gps_data)} points")
            else:
                self.status_message.emit("No GPS data, batch capture will skip GPS overlay")
        elif self.gps_manager:
            self.status_message.emit("GPS still loading, batch capture will skip GPS overlay")

        self._batch_worker = BatchCaptureWorker(
            video_path=self._video_path,
            start_frame=start_frame,
            end_frame=end_frame,
            capture_fps=capture_fps,
            fps=original_fps,
            output_dir=settings.output_dir,
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
        # A9: release main decoder during batch to halve resource usage
        if self.worker.isRunning():
            self.worker.release_cap_async()
        self._batch_worker.start()
    def _on_batch_progress(self, current: int, total: int) -> None:
        self.batch_progress.emit(current, total)

    def _on_batch_finished(self, success: int, total: int) -> None:
        log.info(f"批次截圖完成: {success}/{total}")
        self.batch_finished.emit(success, total)
        self._batch_worker = None

    def _on_batch_failed(self, msg: str) -> None:
        self.load_failed.emit(msg)
        self._batch_worker = None

    @property
    def hw_backends(self) -> list[tuple[str, str]]:
        return self._hw_backends

    @property
    def video_path(self) -> str:
        return self._video_path

    def _safe_stop_worker(self, worker: Any, signals: list, timeout: int = 5000) -> None:
        if worker and worker.isRunning():
            worker.stop()
            for sig in signals:
                try:
                    sig.disconnect()
                except TypeError:
                    pass
            worker.wait(timeout)

    def _stop_batch(self) -> None:
        if self._batch_worker and self._batch_worker.isRunning():
            self._safe_stop_worker(
                self._batch_worker,
                [self._batch_worker.progress, self._batch_worker.finished, self._batch_worker.failed],
            )
            self._batch_worker = None

    # ---- 影片匯出 ----

    def _ensure_probe_info(self) -> dict | None:
        """快取並回傳 ffprobe 結果，避免重複 probe"""
        if self.video_info and self.video_info.probe_info:
            return self.video_info.probe_info
        info = probe_media_info(self._video_path)
        if self.video_info:
            self.video_info.probe_info = info
        return info

    def start_video_export(
        self,
        start_sec: float,
        end_sec: float,
        settings: CaptureSettings,
        preserve_gps: bool = True,
    ) -> None:
        ffmpeg_path, _ = find_ffmpeg()
        if ffmpeg_path is None:
            self.export_finished.emit(False, "未找到 ffmpeg")
            return

        media_info = self._ensure_probe_info()
        has_audio = media_info["has_audio"] if media_info else False
        video_codec = media_info["video_codec"] if media_info else ""

        # 自動偵測原始格式：stream copy 時沿用輸入檔容器，re-encode 時用使用者設定
        if settings.use_stream_copy:
            ori_fmt = detect_input_format(
                self._video_path, media_info.get("format_name", "") if media_info else ""
            )
            fmt = ori_fmt.lower()
            log.info(f"stream copy 模式，沿用原始格式 {fmt}")
        else:
            if video_codec:
                auto_fmt = suggest_container(video_codec, settings.video_format)
                if auto_fmt != settings.video_format:
                    log.warning(
                        f"輸入編碼 {video_codec} 與 {settings.video_format} 不相容，建議改用 {auto_fmt}"
                    )
                    self.status_message.emit(
                        f"輸入編碼 {video_codec} 不支援 {settings.video_format}，輸出已自動切換為 {auto_fmt}"
                    )
                    settings.video_format = auto_fmt
            fmt = settings.video_format.lower()

        out_dir = settings.output_dir
        os.makedirs(out_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
        out_path = os.path.join(out_dir, f"{settings.prefix}_{ts}.{fmt}")

        # stream copy 失敗後自動重試
        self._export_params = dict(
            ffmpeg_path=ffmpeg_path,
            video_path=self._video_path,
            start_sec=start_sec,
            end_sec=end_sec,
            out_path=out_path,
            use_copy=settings.use_stream_copy,
            keep_audio=settings.keep_audio,
            has_audio=has_audio,
            codec=settings.video_codec,
            crf=settings.video_crf,
            preset=settings.video_preset,
            scale_width=settings.video_scale_width,
            preserve_gps=preserve_gps,
        )

        cmd = build_export_cmd(**self._export_params)
        log.info(f"FFmpeg 指令: {' '.join(cmd)}")

        self._launch_export(cmd)

    def _launch_export(self, cmd: list[str]) -> None:
        if self._export_worker and self._export_worker.isRunning():
            self._export_worker.stop()
            try:
                self._export_worker.finished.disconnect()
            except TypeError:
                pass
            self._export_worker.wait(3000)
        self._export_worker = ExportWorker(cmd)
        self._export_worker.finished.connect(self._on_export_finished)
        self._export_worker.start()

    def _on_export_finished(self, success: bool, message: str) -> None:
        # stream copy 失敗後自動改為重新編碼
        if not success and self._export_params.get("use_copy", False):
            self._export_params["use_copy"] = False
            params = dict(self._export_params)
            # 重新編碼改輸出 mp4（libx264 相容容器）
            old_path = params["out_path"]
            base, _ = os.path.splitext(old_path)
            new_path = base + ".mp4"
            params["out_path"] = new_path
            if os.path.isfile(new_path):
                log.warning(f"retry 目標檔案已存在，將覆蓋 {new_path}")
            log.info(f"stream copy 失敗，自動改為 libx264 重新編碼至 {new_path}")
            cmd = build_export_cmd(**params)
            self._launch_export(cmd)
            return

        self.export_finished.emit(success, message)
        self._export_worker = None

    def _cancel_export(self) -> None:
        self.stop_export()
        self._export_params = {}

    def stop_export(self) -> None:
        if self._export_worker and self._export_worker.isRunning():
            self._safe_stop_worker(
                self._export_worker,
                [self._export_worker.finished],
            )
            self._export_worker = None

    # ---- GPS 輔助方法 ----

    def _emit_gps_position(self, sec: float) -> None:
        gps = self.gps_manager.get_position_at_time(sec) if self.gps_manager else None
        if gps:
            self.gps_position_updated.emit(gps[0], gps[1])

    def _get_current_gps(self, frame_idx: int) -> tuple | None:
        """取得當前幀的 GPS 座標 (lat, lon)"""
        if not self.gps_manager or not self.gps_manager.has_gps_data():
            return None
        if self.fps <= 0:
            return None
        seconds = frame_idx / self.fps
        return self.gps_manager.get_position_at_time(seconds)

    def _overlay_gps_text(self, frame: np.ndarray, gps_coord: tuple) -> np.ndarray:
        lat, lon = gps_coord
        frame = _overlay_gps_text_shared(frame, gps_coord)
        osm_bytes = get_static_map(lat, lon, zoom=15, width=160, height=160)
        if osm_bytes:
            frame = overlay_osm_thumbnail(frame, osm_bytes)
        return frame

    # ---- GPS 查詢方法 ----

    def has_gps_data(self) -> bool:
        """檢查當前影片是否有 GPS 資料"""
        return self.gps_manager is not None and self.gps_manager.has_gps_data()

    def get_gps_tracks(self) -> list:
        """取得當前 GPS 軌跡點，可能觸發 OCR，首次呼叫較慢"""
        if not self.has_gps_data():
            return []
        assert self.gps_manager is not None
        self.status_message.emit("正在載入 GPS 軌跡資料...")
        tracks = self.gps_manager.extract_tracks_sync()
        if tracks:
            self.status_message.emit(f"GPS 軌跡: {len(tracks)} 個點")
        return tracks

    def get_gps_bounds(self):
        """取得 GPS 邊界範圍"""
        if not self.has_gps_data():
            return None
        return self.gps_manager.get_bounds()

    def export_gpx(self, output_path: str) -> bool:
        """匯出 GPX 軌跡檔"""
        if not self.has_gps_data():
            return False
        assert self.gps_manager is not None
        return self.gps_manager.export_gpx(output_path)

    # ---- 設定 ----

    def update_capture_settings(self, new_settings: CaptureSettings) -> None:
        """統一更新 capture_settings，避免 UI 與 Controller 雙參照明不同步。

        Controller.capture_settings 為唯一權威來源。
        """
        self._settings = new_settings

    # ---- 清除 ----

    def shutdown(self) -> None:
        self.worker.close()
        if not self.worker.wait(3000):
            log.warning("VideoWorker 未在 3 秒內停止，強制終止（可能影響 OpenCV 內部狀態）")
            self.worker.terminate()
            self.worker.wait(1000)
        # 注意：run() 結尾已自行 release cap，此處不可再釋放，
        # 否則 terminate() 強殺後 run() 尾端仍會執行 release()，
        # 主執行緒與 worker 執行緒同時存取 self.cap 造成競爭。
        self._stop_batch()
        self._cancel_export()
        self._close_gps_manager()
        from dashcam_player.utils.gps_ocr import GPSOCR

        GPSOCR.close_shared_reader()
