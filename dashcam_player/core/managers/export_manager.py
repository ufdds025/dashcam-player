"""ExportManager — 影片匯出職責。

管理 FFmpeg 匯出（ExportWorker）+ stream-copy 失敗重試邏輯。
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from PyQt5.QtCore import QObject, pyqtSignal

if TYPE_CHECKING:
    from dashcam_player.core.export_worker import ExportWorker
    from dashcam_player.models.capture_settings import CaptureSettings
    from dashcam_player.models.video_info import VideoInfo

from dashcam_player.utils.ffmpeg import (
    build_export_cmd,
    detect_input_format,
    find_ffmpeg,
    probe_media_info,
    suggest_container,
)
from dashcam_player.utils.log import get_logger

log = get_logger()


class ExportManager(QObject):
    """匯出管理器：FFmpeg 匯出 + stream-copy 重試。"""

    export_finished = pyqtSignal(bool, str)
    status_message = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._export_worker: ExportWorker | None = None
        self._export_params: dict = {}
        self._video_path: str = ""
        self._video_info: VideoInfo | None = None

    def set_video_context(self, video_path: str, video_info: VideoInfo | None) -> None:
        self._video_path = video_path
        self._video_info = video_info

    def _ensure_probe_info(self) -> dict | None:
        if self._video_info and self._video_info.probe_info:
            return self._video_info.probe_info
        info = probe_media_info(self._video_path)
        if self._video_info:
            self._video_info.probe_info = info
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

        out_dir = settings.output_dir or os.path.abspath("captures")
        os.makedirs(out_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
        out_path = os.path.join(out_dir, f"{settings.prefix}_{ts}.{fmt}")

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

        from dashcam_player.core.export_worker import ExportWorker

        self._export_worker = ExportWorker(cmd)
        self._export_worker.finished.connect(self._on_export_finished)
        self._export_worker.start()

    def _on_export_finished(self, success: bool, message: str) -> None:
        if not success and self._export_params.get("use_copy", False):
            self._export_params["use_copy"] = False
            params = dict(self._export_params)
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

        if success:
            out_path = self._export_params.get("out_path", "")
            message = f"擷取完成\n{out_path}" if out_path else "擷取完成"
        self._export_params = {}
        self.export_finished.emit(success, message)
        self._export_worker = None

    def cancel_export(self) -> None:
        self.stop_export()
        self._export_params = {}

    def stop_export(self) -> None:
        if self._export_worker and self._export_worker.isRunning():
            self._export_worker.stop()
            try:
                self._export_worker.finished.disconnect()
            except TypeError:
                pass
            self._export_worker.wait(3000)
            self._export_worker = None
