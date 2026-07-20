"""PlaybackManager — 播放控制職責。

管理 VideoWorker 的 play/pause/seek/step/speed，
持有 current frame state 與 playback properties。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt5.QtCore import QObject, pyqtSignal

if TYPE_CHECKING:
    import numpy as np

    from dashcam_player.core.video_worker import VideoWorker
    from dashcam_player.models.video_info import VideoInfo


class PlaybackManager(QObject):
    """薄播放管理器：封裝所有播放/seek/step/speed 邏輯。"""

    frame_ready = pyqtSignal(object, int)
    position_updated = pyqtSignal(float, int)
    video_info_ready = pyqtSignal(object)  # VideoInfo
    play_finished = pyqtSignal()

    def __init__(self, worker: VideoWorker, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._worker = worker
        self._current_idx: int = 0
        self._current_frame: np.ndarray | None = None
        self._playing: bool = False
        self._video_info: VideoInfo | None = None
        self._capture_start_frame: int = -1
        self._capture_end_frame: int = -1
        self._gps_throttle_counter: int = 0
        self._gps_throttle_interval: int = 5
        self._gps_manual_seek: bool = False

    def connect_worker_signals(self) -> None:
        self._worker.frame_ready.connect(self._on_frame_ready)
        self._worker.position_changed.connect(self._on_position_changed)
        self._worker.load_finished.connect(self._on_play_finished)
        self._worker.video_info_ready.connect(self._on_video_info_ready)

    # ---- properties ----

    @property
    def video_info(self) -> VideoInfo | None:
        return self._video_info

    @video_info.setter
    def video_info(self, info: VideoInfo | None) -> None:
        self._video_info = info

    @property
    def total_frames(self) -> int:
        return self._video_info.frame_count if self._video_info else 0

    @property
    def fps(self) -> float:
        return self._video_info.fps if self._video_info else 30.0

    @property
    def duration(self) -> float:
        return self._video_info.duration if self._video_info else 0.0

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def current_frame(self) -> np.ndarray | None:
        return self._current_frame

    @property
    def current_idx(self) -> int:
        return self._current_idx

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
    def gps_throttle_interval(self) -> int:
        return self._gps_throttle_interval

    @gps_throttle_interval.setter
    def gps_throttle_interval(self, val: int) -> None:
        self._gps_throttle_interval = val

    @property
    def gps_manual_seek(self) -> bool:
        return self._gps_manual_seek

    @gps_manual_seek.setter
    def gps_manual_seek(self, val: bool) -> None:
        self._gps_manual_seek = val

    @property
    def gps_throttle_counter(self) -> int:
        return self._gps_throttle_counter

    def reset_gps_throttle(self) -> None:
        self._gps_throttle_counter = 0

    def increment_gps_throttle(self) -> bool:
        """Increment counter, return True if GPS update should fire."""
        self._gps_throttle_counter += 1
        should_fire = (
            self._gps_throttle_counter >= self._gps_throttle_interval
            or self._gps_manual_seek
        )
        if should_fire:
            self._gps_throttle_counter = 0
            self._gps_manual_seek = False
        return should_fire

    # ---- playback controls ----

    def toggle_play(self) -> bool:
        if not self._video_info:
            return False
        if self._current_idx >= self.total_frames - 1:
            self.seek(0)
        self._playing = not self._playing
        self._worker.set_play(self._playing)
        return self._playing

    def seek(self, frame_idx: int) -> None:
        self._gps_manual_seek = True
        self._gps_throttle_counter = 0
        self._worker.seek(frame_idx)

    def seek_to(self, frame_idx: int) -> None:
        self.seek(frame_idx)

    def skip(self, seconds: float) -> None:
        if self.fps <= 0:
            return
        delta = int(seconds * self.fps)
        target = max(0, min(self.total_frames - 1, self._current_idx + delta))
        self.seek(target)

    def set_playback_speed(self, speed: float) -> None:
        self._worker.set_target_fps(self.fps * speed)

    def step_forward(self) -> None:
        self._gps_manual_seek = True
        self._gps_throttle_counter = 0
        self._worker.step_forward()

    def step_backward(self) -> None:
        self._gps_manual_seek = True
        self._gps_throttle_counter = 0
        self._worker.step_backward()

    def get_current_frame_index(self) -> int:
        return self._current_idx

    def get_current_time(self) -> float:
        return self._current_idx / self.fps if self.fps > 0 else 0.0

    # ---- signal handlers (connected to worker) ----

    def _on_frame_ready(self, frame: np.ndarray, idx: int) -> None:
        self._current_frame = frame
        self._current_idx = idx
        self.frame_ready.emit(frame, idx)

    def _on_video_info_ready(self, info: VideoInfo) -> None:
        self._video_info = info
        self.video_info_ready.emit(info)

    def _on_position_changed(self, sec: float, frame_idx: int) -> None:
        self._current_idx = frame_idx
        self.position_updated.emit(sec, frame_idx)

    def _on_play_finished(self) -> None:
        self._playing = False
        self.play_finished.emit()
