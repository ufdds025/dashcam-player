"""直接複製 video_worker.py 的 _compute_frame_interval + deadline 排程邏輯，
用真實 cv2 解碼 + 真實 ffprobe PTS，量測 wall-clock 是否等於影片真實長度。
不改動任何原始碼，純粹拿相同演算法對不同幀率的合成影片做端對端驗證。
"""
import json
import subprocess
import time

import cv2


def _compute_frame_interval(pts_list, current_idx, fps, target_fps):
    """逐字複製自 video_worker.py，未做任何修改。"""
    speed_factor = target_fps / fps if fps > 0 else 1.0
    if pts_list and current_idx + 1 < len(pts_list):
        current_pts = pts_list[current_idx] if current_idx < len(pts_list) else 0
        interval = (pts_list[current_idx + 1] - current_pts) / speed_factor
        if 0 < interval <= 1.0:
            return interval
    return 1.0 / target_fps if target_fps > 0 else 0.033


def load_real_pts(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "packet=dts", "-of", "csv", path],
        capture_output=True, text=True,
    ).stdout
    tb_out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=time_base", "-of",
         "default=nk=1:nw=1", path],
        capture_output=True, text=True,
    ).stdout.strip()
    num, den = tb_out.split("/")
    tb = int(num) / int(den)
    dts = [int(line.split(",")[1]) for line in out.splitlines() if line.strip()]
    return [d * tb for d in dts]


def real_duration(path):
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", path], capture_output=True, text=True).stdout)
    return float(info["format"]["duration"])


def simulate_playback(path, fps, target_fps=None):
    target_fps = target_fps or fps
    pts_list = load_real_pts(path)
    cap = cv2.VideoCapture(path)
    idx = 0
    next_deadline = None
    t_start = time.monotonic()
    while True:
        frame_interval = _compute_frame_interval(pts_list, idx, fps, target_fps)
        now = time.monotonic()
        if next_deadline is None:
            next_deadline = now
        if now - next_deadline > frame_interval:
            next_deadline = now
        if now < next_deadline:
            remaining = next_deadline - now
            if remaining > 0.002:
                time.sleep(remaining - 0.002)
            continue
        ret, frame = cap.read()
        if not ret:
            break
        idx += 1
        next_deadline += frame_interval
    cap.release()
    wall_clock = time.monotonic() - t_start
    return idx, wall_clock


for fname, fps in [("test_10fps.mp4", 10), ("test_15fps.mp4", 15),
                    ("test_20fps.mp4", 20), ("test_60fps.mp4", 60)]:
    path = f"synth_test/{fname}"
    expected = real_duration(path)
    frames, wall = simulate_playback(path, fps)
    diff_ms = (wall - expected) * 1000
    print(f"{fname:16s} 真實長度={expected:6.3f}s  播放張數={frames:4d}  "
          f"wall-clock={wall:6.3f}s  誤差={diff_ms:+6.1f}ms")
