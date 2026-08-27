"""SpeedWorker - 背景視覺測速掃描執行緒

負責離線/背景掃描影片產出 SpeedTrack
"""

from __future__ import annotations

import queue
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

import cv2
import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal

if TYPE_CHECKING:
    from dashcam_player.models.speed import SpeedTrack
    from dashcam_player.models.video_info import VideoInfo

from dashcam_player.utils.cv_utils import open_video_capture
from dashcam_player.utils.log import get_logger
from dashcam_player.utils.pts_loader import load_pts_and_keyframes
from dashcam_player.utils.vision_speed import (
    VisionPoint,
    detect_markings_multi_strip,
    estimate_vision_speed,
    video_suitability_check,
)

log = get_logger()


class _CmdType(Enum):
    STOP = "stop"
    PAUSE = "pause"
    RESUME = "resume"


@dataclass
class _Command:
    type: _CmdType


class SpeedWorker(QThread):
    """背景視覺測速 Worker
    
    兩段式自適應取樣：
    Stage 1: 前 8 秒全幀率掃描估算 v_p95
    Stage 2: 自適應 FPS 主掃 + 護網重掃
    """

    result_ready = pyqtSignal(object)  # (SpeedTrack)
    progress = pyqtSignal(int, int)    # (current, total)
    error_occurred = pyqtSignal(str)   # error message
    stage_changed = pyqtSignal(str)    # "stage1" / "stage2" / "rescan"

    def __init__(
        self,
        parent: object | None = None,
        hw_backends: list[tuple[str, str]] | None = None,
    ) -> None:
        super().__init__(parent)
        self._path: str = ""
        self._hw_backends = hw_backends or []
        self._stop = False
        self._pause = False
        self._cmd_queue: queue.Queue[_Command] = queue.Queue()
        
        # 設定參數
        self._distance_per_cycle = 10.0
        self._suitability_threshold = "LIMITED"
        
        # 兩段式參數
        self._stage1_duration = 8.0  # 秒
        self._target_samples_per_cycle = 6
        self._safety_factor = 1.2
        self._min_fps = 6
        self._max_fps = 30
        
        # 護網重掃
        self._rescan_threshold = 2.5  # 週期 < 2.5 * sample_interval
        self._rescan_done = False
        
        # PTS/幀時間
        self._pts_list: list[float] = []
        self._keyframe_times: list[float] = []
        self._fps: float = 30.0
        self._total_frames: int = 0
        self._duration: float = 0.0
        
        # 狀態
        self._current_stage = "idle"
        self._current_frame_idx = 0
        
        # 結果
        self._vision_points: list = []  # VisionPoint 列表

    def open(self, path: str, distance_per_cycle: float = 10.0) -> None:
        self._path = path
        self._distance_per_cycle = distance_per_cycle
        self._stop = False
        self._pause = False
        self._rescan_done = False
        self._vision_points = []
        self._cmd_queue = queue.Queue()  # 重置佇列
        self.start()

    def close(self) -> None:
        self._stop = True
        self._cmd_queue.put_nowait(_Command(_CmdType.STOP))

    def pause(self) -> None:
        self._pause = True
        self._cmd_queue.put_nowait(_Command(_CmdType.PAUSE))

    def resume(self) -> None:
        self._pause = False
        self._cmd_queue.put_nowait(_Command(_CmdType.RESUME))

    def _drain_commands(self) -> None:
        while True:
            try:
                self._cmd_queue.get_nowait()
            except queue.Empty:
                break

    def run(self) -> None:
        # 載入 PTS + keyframe
        log.info(f"SpeedWorker: 開始載入 PTS - {self._path}")
        pts_result = self._load_pts()
        if not pts_result.pts_list:
            self.error_occurred.emit("無法載入影片時間資訊")
            return
        
        self._pts_list = pts_result.pts_list
        self._keyframe_times = pts_result.keyframe_times
        self._fps = 1.0 / (pts_result.pts_list[1] - pts_result.pts_list[0]) if len(pts_result.pts_list) > 1 else 30.0
        self._total_frames = len(pts_result.pts_list)
        self._duration = pts_result.pts_list[-1] if pts_result.pts_list else 0.0
        
        log.info(f"SpeedWorker: PTS 載入完成 - {len(self._pts_list)} 幀, {self._duration:.1f}s, {self._fps:.1f}fps")
        
        # 開啟影片
        cap = open_video_capture(self._path, self._hw_backends)
        if not cap.isOpened():
            self.error_occurred.emit(f"無法開啟影片: {self._path}")
            return
        
        try:
            # Stage 1: 粗探
            self._current_stage = "stage1"
            self.stage_changed.emit("stage1")
            self._run_stage1(cap)
            
            if self._stop:
                return
            
            # Stage 2: 主掃
            self._current_stage = "stage2"
            self.stage_changed.emit("stage2")
            self._run_stage2(cap)
            
            if self._stop:
                return
            
            # 完成
            self._finish()
            
        finally:
            if cap:
                cap.release()

    def _load_pts(self):
        """載入 PTS + keyframe (使用 pts_loader)"""
        from dashcam_player.utils.pts_loader import load_pts_and_keyframes
        return load_pts_and_keyframes(self._path, timeout=30.0)

    def _run_stage1(self, cap: cv2.VideoCapture) -> None:
        """Stage 1: 前 8 秒全幀率掃描估算 v_p95"""
        log.info("SpeedWorker: Stage 1 開始 - 粗探")
        self.progress.emit(0, 100)
        
        stage1_frames = int(self._stage1_duration * self._fps)
        stage1_frames = min(stage1_frames, self._total_frames)
        
        periods = []
        pts_sampled = []
        
        for idx in range(stage1_frames):
            if self._stop:
                return
            while self._pause:
                self.msleep(100)
                if self._stop:
                    return
            
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, frame = cap.read()
            if not ret or frame is None:
                continue
            
            # 適合度檢查
            suitability = video_suitability_check(frame)
            if suitability == "UNSUITABLE":
                continue
            
            # 灰階 ROI
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            h, w = gray.shape
            roi = gray[int(h*0.65):, int(w*0.2):int(w*0.8)]
            
            # 偵測標線
            periods_frame = detect_markings_multi_strip(roi)
            if periods_frame:
                periods.extend(periods_frame)
                pts_sampled.append(idx / self._fps)
            
            # 進度
            if idx % max(1, stage1_frames // 20) == 0:
                self.progress.emit(int(idx / stage1_frames * 100), 100)
        
        self.progress.emit(100, 100)
        
        # 估算 v_p95
        if periods:
            speeds = [self._distance_per_cycle / p for p in periods if p > 0]
            speeds = [s for s in speeds if 1.39 <= s <= 55.56]  # 5-200 km/h
            if speeds:
                speeds.sort()
                v_p95 = speeds[int(len(speeds) * 0.95)]
                # 計算建議 FPS
                target_fps = min(max(int(v_p95 / self._distance_per_cycle * self._target_samples_per_cycle * 1.2), 6), 30)
                log.info(f"Stage 1 完成: v_p95={v_p95*3.6:.1f} km/h, 建議 FPS={target_fps}")
            else:
                target_fps = 15
        else:
            target_fps = 15
        
        self._stage2_fps = target_fps
        log.info(f"SpeedWorker: Stage 1 完成，建議 FPS = {target_fps}")

    def _run_stage2(self, cap: cv2.VideoCapture) -> None:
        """Stage 2: 自適應 FPS 主掃 + 護網重掃"""
        log.info(f"SpeedWorker: Stage 2 開始 - 主掃 @ {self._stage2_fps}fps")
        
        # 計算跳幀間隔
        frame_interval = max(1, int(self._fps / self._stage2_fps))
        
        bad_windows = []  # (start_idx, end_idx)
        self._vision_points = []
        
        idx = 0
        while idx < self._total_frames:
            if self._stop:
                return
            while self._pause:
                self.msleep(100)
                if self._stop:
                    return
            
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, frame = cap.read()
            if not ret or frame is None:
                idx += frame_interval
                continue
            
            # 適合度檢查
            suitability = video_suitability_check(frame)
            if suitability == "UNSUITABLE":
                idx += frame_interval
                continue
            
            # 灰階 ROI
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            h, w = gray.shape
            roi = gray[int(h*0.65):, int(w*0.2):int(w*0.8)]
            
            # 偵測標線
            periods = detect_markings_multi_strip(roi)
            
            # 時間戳
            t = idx / self._fps
            
            if periods:
                from dashcam_player.utils.vision_speed import estimate_vision_speed
                v_points = estimate_vision_speed(
                    periods, [t], self._distance_per_cycle
                )
                for vp in v_points:
                    self._vision_points.append(vp)
            else:
                # 無標線偵測
                self._vision_points.append(None)
            
            # 護網檢查：連續窗口週期異常
            if len(self._vision_points) >= 2:
                # 檢查最近幾個有效點
                valid = [vp for vp in self._vision_points[-5:] if vp and vp.raw_speed]
                if len(valid) >= 2:
                    periods_recent = [self._distance_per_cycle / vp.raw_speed for vp in valid if vp.raw_speed]
                    if periods_recent:
                        avg_period = sum(periods_recent) / len(periods_recent)
                        sample_interval = frame_interval / self._fps
                        if avg_period < 2.5 * sample_interval and not self._rescan_done:
                            # 標記需要重掃
                            bad_windows.append((idx - frame_interval*4, idx))
            
            idx += frame_interval
            
            # 進度
            if idx % max(1, self._total_frames // 100) == 0:
                self.progress.emit(int(idx / self._total_frames * 100), 100)
        
        # 護網重掃
        if bad_windows and not self._rescan_done:
            self._rescan_done = True
            self.stage_changed.emit("rescan")
            self._rescan_windows(cap, bad_windows)
        
        self.progress.emit(100, 100)

    def _rescan_windows(self, cap: cv2.VideoCapture, windows: list[tuple[int, int]]) -> None:
        """局部 30fps 重掃異常窗口"""
        log.info(f"SpeedWorker: 重掃 {len(windows)} 個異常窗口")
        
        for start_idx, end_idx in windows:
            if self._stop:
                return
            start_idx = max(0, start_idx)
            end_idx = min(self._total_frames - 1, end_idx)
            
            for idx in range(start_idx, end_idx + 1):
                if self._stop:
                    return
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ret, frame = cap.read()
                if not ret or frame is None:
                    continue
                
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                h, w = gray.shape
                roi = gray[int(h*0.65):, int(w*0.2):int(w*0.8)]
                periods = detect_markings_multi_strip(roi)
                
                if periods:
                    from dashcam_player.utils.vision_speed import estimate_vision_speed
                    t = idx / self._fps
                    v_points = estimate_vision_speed(periods, [t], self._distance_per_cycle)
                    # 更新對應位置
                    # 這裡簡化：實際應更新 _vision_points 對應位置
                    pass

    def _finish(self) -> None:
        """完成掃描，產出 SpeedTrack"""
        from dashcam_player.models.speed import SpeedTrack, SpeedPoint
        
        # 建立 SpeedTrack
        points = []
        for vp in self._vision_points:
            if vp:
                points.append(SpeedPoint(
                    time=vp.time,
                    raw_vision=vp.raw_speed,
                    corrected=None,
                    fused=vp.raw_speed,
                    confidence=vp.confidence,
                    source="vision",
                    period_hypothesis=vp.period_hypothesis,
                    strip_votes=vp.strip_votes,
                    reject_reason=vp.reject_reason,
                ))
        
        if not points:
            track = SpeedTrack(points=[], status="NO_VISION_SPEED")
        else:
            track = SpeedTrack(
                points=points,
                status="OK" if len(points) > 10 else "PARTIAL",
            )
        
        self.result_ready.emit(track)
        log.info(f"SpeedWorker 完成: {len(points)} points, status={track.status}")