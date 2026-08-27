from __future__ import annotations

import bisect
import queue
import threading
import time
from dataclasses import dataclass
from enum import Enum

import cv2
from PyQt5.QtCore import QThread, pyqtSignal

from dashcam_player.models.video_info import VideoInfo
from dashcam_player.utils.cv_utils import decode_fourcc, open_video_capture
from dashcam_player.utils.ffmpeg import get_keyframe_positions
from dashcam_player.utils.log import get_logger
from dashcam_player.utils.pts_loader import load_pts_and_keyframes_sync

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
    error_occurred = pyqtSignal(str, str)  # (title, message) — seek/播放等非載入失敗的錯誤
    play_finished = pyqtSignal()  # 播放到尾端（EOF）
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
        self._keyframe_loaded: bool = False  # A2: 背景載入完成 flag

    def open(self, path: str) -> None:
        self._path = path
        self._stop = False
        self._current_idx = 0
        self._keyframe_loaded = False
        self._keyframe_times = None
        self._pts_list = []
        self._pts_quality = "UNSAFE"
        # 背景預載關鍵影格時間表 + 全幀 PTS，避免雙重 ffprobe
        self._start_keyframe_loader()
        # 清空任何舊指令，開啟後停在首幀（idle）
        self._drain_commands()
        self._cmd_queue.put(_Command(_CmdType.STOP))
        self.start()

    def _start_keyframe_loader(self) -> None:
        """背景執行緒預載關鍵影格時間表 + 全幀 PTS (P1c)。
        
        合併原有 keyframe loader 與新的 pts_loader，
        避免雙重 ffprobe 呼叫。
        seek 不阻塞：表格就緒前 `_do_seek` 直接交給 cv2 內部 seek。
        影片已切換時丟棄結果（以載入當下的 path 比對）。
        """
        path = self._path
        if not path:
            return

        def _load() -> None:
            pts_list, keyframe_times, quality = load_pts_and_keyframes_sync(path, timeout=30.0)
            if self._path != path:
                return  # 影片已切換，丟棄過期結果
            self._pts_list = pts_list
            self._keyframe_times = keyframe_times
            self._pts_quality = quality
            if keyframe_times:
                log.info(f"關鍵影格索引載入完成: {len(keyframe_times)} 個")
            if pts_list:
                log.info(f"PTS 陣列載入完成: {len(pts_list)} 幀, quality={quality}")
            self._keyframe_loaded = True

        threading.Thread(target=_load, name="pts-keyframe-loader", daemon=True).start()

    def close(self) -> None:
        self._stop = True
        # push STOP 喚醒可能阻塞在 msleep 的 loop，確保盡快退出
        # （queue 為 unbounded，put_nowait 不會 raise Full）
        self._cmd_queue.put_nowait(_Command(_CmdType.STOP))

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
        """回傳 <= target 的最近關鍵影格幂號（A2: keyframe-aware seek）。

        有 keyframe 時間表時：找 <= target 時間的最大 keyframe，從該處向前解碼，
        避免 cv2 在關鍵影格稀疏影片上向後回溯導致長片卡頓。
        無 keyframe 表時回退為 target 本身（沿用 cv2 內部行為）。
        
        優先使用 PTS 陣列計算目標秒數（VFR 準確），回退用 fps 估算。
        """
        if not self._keyframe_times or self._fps <= 0:
            return target
        
        # 計算目標秒數：優先用 PTS，否則用 fps 估算
        if self._pts_list and target < len(self._pts_list):
            target_sec = self._pts_list[target]
        else:
            target_sec = target / self._fps
        
        # bisect_right 找 <= target_sec 的最大 keyframe 時間（O(log n)）
        pos = bisect.bisect_right(self._keyframe_times, target_sec) - 1
        if pos < 0:
            return 0
        kf_sec = self._keyframe_times[pos]
        
        # 回傳幂號：優先用 PTS 反推，否則用 fps 估算
        if self._pts_list:
            # 在 PTS 陣列中找最接近 kf_sec 的索引
            idx = bisect.bisect_left(self._pts_list, kf_sec)
            return max(0, min(self._total_frames - 1, idx))
        else:
            return max(0, min(self._total_frames - 1, int(round(kf_sec * self._fps))))

    def _do_seek(self, target: int) -> None:
        """執行跳轉並 emit 結果（run loop 內呼叫）。A2: 先跳最近 keyframe 再向前解碼。"""
        if not self.cap:
            return
        # A2: keyframe 表由背景執行緒預載；尚未就緒時直接交給 cv2 內部 seek（不阻塞）
        start = self._nearest_keyframe_idx(target) if self._keyframe_loaded else target
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
            msg = f"無法跳轉至幀 {target}（僅解到 {idx}）"
            log.warning(msg)
            self.error_occurred.emit("跳轉失敗", msg)
            return
        # 讀取 target 幀
        ret, frame = self.cap.read()
        if ret:
            self._current_idx = target
            self.frame_ready.emit(frame, target)
            pos = target / self._fps
            self.position_changed.emit(pos, target)
        else:
            msg = f"跳轉至幀 {target} 後讀取失敗"
            log.warning(msg)
            self.error_occurred.emit("跳轉失敗", msg)

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
        self.cap = open_video_capture(self._path, self._hw_backends)
        if not self.cap.isOpened():
            log.error(f"重建解碼器失敗: {self._path}")
            self.cap = None
            return False
        # 跳回目前幀位置
        if self._current_idx > 0:
            if not self.cap.set(cv2.CAP_PROP_POS_FRAMES, self._current_idx):
                log.warning(f"重建解碼器後 seek 至 {self._current_idx} 失敗，重置至首幀")
                self._current_idx = 0
        return True

    def run(self) -> None:
        self.cap = open_video_capture(self._path, self._hw_backends)
        try:
            # cap 建立後任何路徑（early return / 例外）都必須釋放解碼器；
            # 舊版三個 early return 發生在 try 之前，會洩漏 VideoCapture
            self._run_loop()
        finally:
            if self.cap:
                self.cap.release()
                self.cap = None

def _run_loop(self) -> None:
        if self.cap is None:  # run() 必已賦值；此守衛同時供型別窄化
            return
        if not self.cap.isOpened():
            log.error(f"無法開啟影片: {self._path}")
            self.load_failed.emit(f"無法開啟影片: {self._path}")
            return

        self._total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        src_fps = self.cap.get(cv2.CAP_PROP_FPS)
        self._fps = src_fps if src_fps > 0 else 30.0
        # 換檔重置為 1.0x；實際倍率由 PlaybackManager 於 video_info_ready 時套用
        self._target_fps = self._fps
        
        # 使用 PTS 計算 duration（若可用），否則回退 fps 估算
        if self._pts_list and len(self._pts_list) == self._total_frames:
            duration = self._pts_list[-1] if self._pts_list else 0
        else:
            duration = self._total_frames / self._fps if self._fps > 0 else 0
            
        fourcc_val = int(self.cap.get(cv2.CAP_PROP_FOURCC))
        codec_str = decode_fourcc(fourcc_val)
        width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        log.info(
            f"開啟成功  codec={codec_str}  {width}x{height}  frames={self._total_frames}  fps={self._fps:.2f}"
        )

        if self._total_frames <= 0:
            log.error("幂數為 0")
            self.load_failed.emit("影片無效或無法讀取幂數")
            return

        # 解第一幂
        ret, frame = self.cap.read()
        if not ret:
            log.error("第一幂讀取失敗")
            self.load_failed.emit("無法解碼影片第一幂")
            return

        self._current_idx = 0
        self.frame_ready.emit(frame, 0)
        pos = self._pts_list[0] if self._pts_list else 0.0
        self.position_changed.emit(pos, 0)

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
                    # 維持播放狀態：拖動進度條後繼續播放（單步仍暫停）
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
                
                # 使用 PTS 計算幀間隔（若可用），否則用 target_fps
                if self._pts_list and self._current_idx + 1 < len(self._pts_list):
                    next_pts = self._pts_list[self._current_idx + 1]
                    current_pts = self._pts_list[self._current_idx] if self._current_idx < len(self._pts_list) else 0
                    frame_interval = next_pts - current_pts
                else:
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
                    self.play_finished.emit()
                    continue

                self._current_idx += 1
                self.frame_ready.emit(frame, self._current_idx)
                pos = self._pts_list[self._current_idx] if self._pts_list and self._current_idx < len(self._pts_list) else self._current_idx / self._fps
                self.position_changed.emit(pos, self._current_idx)
                last_emit_time = time.monotonic()
            else:
                self.msleep(20)
