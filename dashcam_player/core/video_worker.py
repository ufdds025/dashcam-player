from __future__ import annotations

import queue
import time
from dataclasses import dataclass
from enum import Enum

import cv2
from PyQt5.QtCore import QThread, pyqtSignal

from dashcam_player.models.video_info import VideoInfo
from dashcam_player.utils.cv_utils import configure_hw_acceleration, decode_fourcc
from dashcam_player.utils.ffmpeg import get_keyframe_positions, win32_short_path
from dashcam_player.utils.log import get_logger

log = get_logger()


class _CmdType(Enum):
    PLAY = "play"
    PAUSE = "pause"
    SEEK = "seek"
    STEP_FWD = "step_fwd"
    STEP_BWD = "step_bwd"
    STOP = "stop"
    RELEASE_CAP = "release_cap"


@dataclass
class _Command:
    """統一指令，由主執行緒 push、run() loop 單一消費。"""

    type: _CmdType
    frame: int = -1  # SEEK 用


class VideoWorker(QThread):
    """背景解碼引擎：播放、seek、單步前進/後退

    Thread safety:
    - 所有播放/seek/step 指令經由 self._cmd_queue 統一投遞，run() 單一消費，
      消除舊版 _play / _seek_frame 雙信號競爭。
    - _current_idx: 僅 run() loop 寫入（push 後自行更新），讀取端（controller）經信號取得。
    - _stop: 由主執行緒 close() 寫入，run() 讀取以結束 loop。
    - _cmd_queue 為 queue.Queue（thread-safe），主執行緒 push、run() loop 消費。
    """

    frame_ready = pyqtSignal(object, int)  # (frame_bgr, frame_index)
    position_changed = pyqtSignal(float, int)  # (當前秒數, 幀號)
    load_failed = pyqtSignal(str)
    load_finished = pyqtSignal()
    video_info_ready = pyqtSignal(object)  # VideoInfo instance

    def __init__(
        self,
        parent: object | None = None,
        hw_backends: list[tuple[str, str]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.cap: cv2.VideoCapture | None = None
        self._path: str = ""
        self._stop: bool = False
        self._fps: float = 30.0
        self._target_fps: float = 30.0
        self._total_frames: int = 0
        self._current_idx: int = 0
        self._hw_backends = hw_backends or []
        self._cmd_queue: queue.Queue[_Command] = queue.Queue()
        self._keyframe_times: list[float] | None = None  # 關鍵影格時間（秒），用於精確 seek
        self._keyframe_loaded: bool = False  # A2: 延遲載入 flag

    def open(self, path: str) -> None:
        self._path = path
        self._stop = False
        self._current_idx = 0
        self._keyframe_loaded = False
        self._keyframe_times = None
        # 清空任何舊指令，開啟後停在首幀（idle）
        self._drain_commands()
        self._cmd_queue.put(_Command(_CmdType.STOP))
        self.start()

    def close(self) -> None:
        self._stop = True
        # push STOP 喚醒可能阻塞在 msleep 的 loop，確保盡快退出
        try:
            self._cmd_queue.put_nowait(_Command(_CmdType.STOP))
        except queue.Full:
            pass

    def _drain_commands(self) -> None:
        while True:
            try:
                self._cmd_queue.get_nowait()
            except queue.Empty:
                break

    def release_cap(self) -> None:
        if self.cap:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

    @property
    def frame_count(self) -> int:
        return self._total_frames

    @property
    def current_index(self) -> int:
        return self._current_idx

    @property
    def fps(self) -> float:
        return self._fps

    def set_target_fps(self, fps: float) -> None:
        self._target_fps = fps

    def seek(self, frame_idx: int) -> None:
        if self._total_frames <= 0:
            return
        idx = max(0, min(frame_idx, self._total_frames - 1))
        self._cmd_queue.put(_Command(_CmdType.SEEK, idx))

    def set_play(self, on: bool) -> None:
        self._cmd_queue.put(_Command(_CmdType.PLAY if on else _CmdType.PAUSE))

    def step_forward(self) -> None:
        self._cmd_queue.put(_Command(_CmdType.STEP_FWD))

    def step_backward(self) -> None:
        self._cmd_queue.put(_Command(_CmdType.STEP_BWD))

    def _nearest_keyframe_idx(self, target: int) -> int:
        """回傳 <= target 的最近關鍵影格幀號（A2: keyframe-aware seek）。

        有 keyframe 時間表時：找 <= target 時間的最大 keyframe，從該處向前解碼，
        避免 cv2 在關鍵影格稀疏影片上向後回溯導致長片卡頓。
        無 keyframe 表時回退為 target 本身（沿用 cv2 內部行為）。
        """
        if not self._keyframe_times or self._fps <= 0:
            return target
        target_sec = target / self._fps
        # 找 <= target_sec 的最大 keyframe 時間
        kf_sec = 0.0
        for t in self._keyframe_times:
            if t <= target_sec:
                kf_sec = t
            else:
                break
        return max(0, min(self._total_frames - 1, int(round(kf_sec * self._fps))))

    def _do_seek(self, target: int) -> None:
        """執行跳轉並 emit 結果（run loop 內呼叫）。A2: 先跳最近 keyframe 再向前解碼。"""
        if not self.cap:
            return
        # A2: 首次 seek 時才載入關鍵影格表（避免阻塞初始播放）
        if not self._keyframe_loaded:
            self._keyframe_loaded = True
            self._keyframe_times = get_keyframe_positions(self._path)
            if self._keyframe_times:
                log.info(f"關鍵影格索引載入完成: {len(self._keyframe_times)} 個")
        start = self._nearest_keyframe_idx(target)
        if not self.cap.set(cv2.CAP_PROP_POS_FRAMES, start):
            log.warning(f"seek 到 keyframe {start} 失敗，從頭循序解碼")
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            start = 0
        # 從 keyframe 向前解碼到 target
        idx = start
        ret = True
        frame = None
        while idx < target:
            ret, frame = self.cap.read()
            if not ret or frame is None:
                break
            idx += 1
        if idx < target:
            # 仍不到 target（影片不足幀），視為失敗
            log.warning(f"seek 到 {target} 失敗（僅解到 {idx}）")
            return
        # 讀取 target 幀
        ret, frame = self.cap.read()
        if ret:
            self._current_idx = target
            self.frame_ready.emit(frame, target)
            pos = target / self._fps
            self.position_changed.emit(pos, target)
        else:
            log.warning(f"seek 到 {target} 後 read 失敗")

    def release_cap_async(self) -> None:
        """A9: 釋放解碼器（批次截圖時避免雙重 VideoCapture 佔用資源）。

        非同步推入指令，run loop 消費後關閉 cap；播放時 set_play(True) 會自動重建。
        """
        self._cmd_queue.put(_Command(_CmdType.RELEASE_CAP))

    def _ensure_cap(self) -> bool:
        """確保 cap 已開啟（A9: 批次釋放後重新播放時重建）。"""
        if self.cap is not None:
            return True
        if not self._path:
            return False
        self.cap = cv2.VideoCapture(win32_short_path(self._path), cv2.CAP_FFMPEG)
        configure_hw_acceleration(self.cap, self._hw_backends)
        if not self.cap.isOpened():
            log.error(f"重建解碼器失敗: {self._path}")
            self.cap = None
            return False
        # 跳回目前幀位置
        if self._current_idx > 0:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, self._current_idx)
        return True

    def run(self) -> None:
        self.cap = cv2.VideoCapture(win32_short_path(self._path), cv2.CAP_FFMPEG)
        configure_hw_acceleration(self.cap, self._hw_backends)

        if not self.cap.isOpened():
            log.error(f"無法開啟影片: {self._path}")
            self.load_failed.emit(f"無法開啟影片: {self._path}")
            return

        self._total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        src_fps = self.cap.get(cv2.CAP_PROP_FPS)
        self._fps = src_fps if src_fps > 0 else 30.0
        duration = self._total_frames / self._fps if self._fps > 0 else 0
        fourcc_val = int(self.cap.get(cv2.CAP_PROP_FOURCC))
        codec_str = decode_fourcc(fourcc_val)
        width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        log.info(
            f"開啟成功  codec={codec_str}  {width}x{height}  frames={self._total_frames}  fps={self._fps:.2f}"
        )

        if self._total_frames <= 0:
            log.error("幀數為 0")
            self.load_failed.emit("影片無效或無法讀取幀數")
            return

        # 解第一幀
        ret, frame = self.cap.read()
        if not ret:
            log.error("第一幀讀取失敗")
            self.load_failed.emit("無法解碼影片第一幀")
            return

        self._current_idx = 0
        self.frame_ready.emit(frame, 0)
        self.position_changed.emit(0.0, 0)

        self.video_info_ready.emit(
            VideoInfo(
                path=self._path,
                fps=self._fps,
                frame_count=self._total_frames,
                fourcc=codec_str,
                width=width,
                height=height,
                duration=duration,
            )
        )

        playing = False
        last_emit_time = time.monotonic()

        try:
            while not self._stop:
                try:
                    cmd = self._cmd_queue.get_nowait()
                except queue.Empty:
                    cmd = None

                if cmd is not None:
                    if cmd.type == _CmdType.STOP:
                        playing = False
                    elif cmd.type == _CmdType.PAUSE:
                        playing = False
                    elif cmd.type == _CmdType.PLAY:
                        # A9: 批次釋放後重建解碼器
                        if not self._ensure_cap():
                            log.warning("play 但解碼器重建失敗，放棄播放")
                            playing = False
                        else:
                            playing = True
                    elif cmd.type == _CmdType.RELEASE_CAP:
                        # A9: 釋放解碼器資源（批次截圖期間）
                        self.release_cap()
                        playing = False
                    elif cmd.type == _CmdType.SEEK:
                        playing = False
                        self._do_seek(cmd.frame)
                        last_emit_time = time.monotonic()
                        continue
                    elif cmd.type == _CmdType.STEP_FWD:
                        playing = False
                        target = self._current_idx + 1
                        if 0 <= target < self._total_frames:
                            self._do_seek(target)
                        last_emit_time = time.monotonic()
                        continue
                    elif cmd.type == _CmdType.STEP_BWD:
                        playing = False
                        target = self._current_idx - 1
                        if 0 <= target < self._total_frames:
                            self._do_seek(target)
                        last_emit_time = time.monotonic()
                        continue

                # === 播放模式 ===
                if playing:
                    now = time.monotonic()
                    elapsed = now - last_emit_time
                    frame_interval = 1.0 / self._target_fps if self._target_fps > 0 else 0.033

                    if elapsed < frame_interval:
                        remaining_ms = (frame_interval - elapsed) * 1000
                        if remaining_ms >= 5:
                            self.msleep(int(remaining_ms * 0.8))
                        continue

                    ret, frame = self.cap.read()
                    if not ret:
                        log.debug(f"播放結束 (idx={self._current_idx})")
                        playing = False
                        self.load_finished.emit()
                        continue

                    self._current_idx += 1
                    self.frame_ready.emit(frame, self._current_idx)
                    pos = self._current_idx / self._fps
                    self.position_changed.emit(pos, self._current_idx)
                    last_emit_time = time.monotonic()
                else:
                    self.msleep(20)
        finally:
            if self.cap:
                self.cap.release()
                self.cap = None
