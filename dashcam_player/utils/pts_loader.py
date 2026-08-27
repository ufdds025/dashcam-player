"""PTS + Keyframe 探測器

合併 keyframe 與全幀 PTS 探測，供 VideoWorker 與 SpeedWorker 共用
"""

from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import dataclass
from typing import Any

from dashcam_player.utils.ffmpeg import find_ffmpeg
from dashcam_player.utils.log import get_logger

log = get_logger()


@dataclass
class PTSResult:
    """PTS 探測結果"""
    pts_list: list[float]          # 每幀真實時間 (秒)
    keyframe_times: list[float]    # 關鍵幀時間 (秒)
    source: str                    # "pts" / "best_effort" / "linear"
    quality: str                   # "EXACT" / "DEGRADED" / "UNSAFE"


def load_pts_and_keyframes(
    video_path: str,
    timeout: float = 30.0,
    duration_budget_factor: float = 0.1,
) -> PTSResult:
    """
    一次探測同時取得全幀 PTS + 關鍵幀時間
    
    Args:
        video_path: 影片路徑
        timeout: 基礎超時秒數
        duration_budget_factor: 影片長度 × 此係數 = 額外預算 (秒)
    
    Returns:
        PTSResult (pts_list, keyframe_times, source, quality)
    """
    ffmpeg_path, ffprobe_path = find_ffmpeg()
    if ffprobe_path is None:
        return PTSResult([], [], "none", "UNSAFE")
    
    # 先用 ffprobe 取得影片長度以計算動態 timeout
    try:
        dur_cmd = [ffprobe_path, "-v", "error", "-show_entries", "format=duration",
                   "-of", "csv=p=0", video_path]
        dur_result = subprocess.run(dur_cmd, capture_output=True, timeout=10, text=True)
        duration = float(dur_result.stdout.strip()) if dur_result.stdout.strip() else 0.0
    except Exception:
        duration = 0.0
    
    # 動態 timeout = max(固定最小, duration * factor)
    dynamic_timeout = max(timeout, duration * 0.1)
    
    # 1. 先嘗試取得 pkt_pts_time (最準確)
    pts_result = _probe_pts(video_path, ffprobe_path, dynamic_timeout, "pkt_pts_time")
    if pts_result.pts_list:
        return pts_result
    
    # 2. 嘗試 best_effort_timestamp_time
    pts_result = _probe_pts(video_path, ffprobe_path, dynamic_timeout, "best_effort_timestamp_time")
    if pts_result.pts_list:
        pts_result.quality = "DEGRADED"
        pts_result.source = "best_effort"
        return pts_result
    
    # 3. 回退線性累加 (需先取得 keyframe times 做平均 fps)
    keyframe_times = get_keyframe_positions(video_path)
    if keyframe_times and len(keyframe_times) > 1:
        # 估算平均 fps
        total_frames = len(keyframe_times) * 100  # 粗估
        # 實際上需要 ffprobe 取得總幀數
        pts_result = _linear_fallback(video_path, ffprobe_path, dynamic_timeout)
        if pts_result.pts_list:
            pts_result.quality = "UNSAFE"
            pts_result.source = "linear"
            return pts_result
    
    return PTSResult([], [], "none", "UNSAFE")


def _probe_pts(
    video_path: str,
    ffprobe_path: str,
    timeout: float,
    entry: str,
) -> PTSResult:
    """探測指定 PTS 類型"""
    cmd = [
        ffprobe_path,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", f"frame={entry},pict_type",
        "-of", "json",
        video_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if result.returncode != 0:
            return PTSResult([], [], "none", "UNSAFE")
        
        data = json.loads(result.stdout.decode("utf-8", errors="replace"))
        frames = data.get("frames", [])
        
        pts_list = []
        keyframe_times = []
        for f in frames:
            if entry in f:
                try:
                    pts = float(f[entry])
                    pts_list.append(pts)
                    if f.get("pict_type") == "I":
                        keyframe_times.append(pts)
                except (ValueError, TypeError):
                    pass
        
        if pts_list:
            quality = "EXACT" if entry == "pkt_pts_time" else "DEGRADED"
            return PTSResult(pts_list, keyframe_times, entry, quality)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        pass
    
    return PTSResult([], [], "none", "UNSAFE")


def _linear_fallback(
    video_path: str,
    ffprobe_path: str,
    timeout: float,
) -> PTSResult:
    """線性 fallback：用平均 fps 估算"""
    # 取得總幀數和平均 fps
    cmd = [
        ffprobe_path,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate,nb_frames",
        "-of", "json",
        video_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if result.returncode != 0:
            return PTSResult([], [], "none", "UNSAFE")
        
        data = json.loads(result.stdout.decode("utf-8", errors="replace"))
        streams = data.get("streams", [])
        if not streams:
            return PTSResult([], [], "none", "UNSAFE")
        
        stream = streams[0]
        # 解析 r_frame_rate (如 "30000/1001")
        rfr = stream.get("r_frame_rate", "0/1")
        if "/" in rfr:
            num, den = map(int, rfr.split("/"))
            fps = num / den if den else 0.0
        else:
            fps = float(rfr)
        
        nb_frames = int(stream.get("nb_frames", 0)) or 0
        
        if fps > 0 and nb_frames > 0:
            pts_list = [i / fps for i in range(nb_frames)]
            return PTSResult(pts_list, [], "linear_fallback", "UNSAFE")
    except Exception:
        pass
    
    return PTSResult([], [], "none", "UNSAFE")


# 保留原有介面給 VideoWorker 使用
def get_keyframe_positions(video_path: str) -> list[float]:
    """取得關鍵幀時間位置（秒） - 供 VideoWorker seek 優化使用"""
    result = load_pts_and_keyframes(video_path, timeout=30.0)
    return result.keyframe_times


# 便利函數
def load_pts_and_keyframes_sync(
    video_path: str,
    timeout: float = 30.0,
) -> tuple[list[float], list[float], str]:
    """同步載入 PTS 和 keyframe，回傳 (pts_list, keyframe_times, quality)"""
    result = load_pts_and_keyframes(video_path, timeout)
    return result.pts_list, result.keyframe_times, result.quality