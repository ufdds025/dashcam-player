"""標線週期測速驗證工具

在 5 支測試影片上執行標線週期測速，並與內嵌 GPS 速度比較。
輸出每支影片的標線速度、GPS 速度與誤差。

用法:
    python -m dashcam_player.utils.marking_test_runner [video1 video2 ...]
"""

from __future__ import annotations

import os
import subprocess
import sys

# 直接執行支援（VS Code cwd 非專案根 + 中文路徑）
if __package__ in (None, ""):
    _proj_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _proj_root not in sys.path:
        sys.path.insert(0, _proj_root)
else:
    # 以 -m 執行時仍需 _proj_root 供影片路徑解析
    _proj_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from dashcam_player.utils.marking_detector import analyze_video

DEFAULT_VIDEOS = [
    "PIC20260825-180655-F.AVI",
    "PIC20260825-180755-F.AVI",
    "PIC20260826-091548-F.AVI",
    "PIC20260826-091548-R.AVI",
    "PIC20260826-092148-F.AVI",
]

SCAN_BANDS = {
    "PIC20260825-180655-F.AVI": (0.50, 0.64),
    "PIC20260825-180755-F.AVI": (0.50, 0.64),
    "PIC20260826-091548-F.AVI": (0.50, 0.64),
    "PIC20260826-091548-R.AVI": (0.45, 0.70),
    "PIC20260826-092148-F.AVI": (0.45, 0.64),
}


def _resolve_video_path(v: str) -> str | None:
    """解析影片路徑，支援相對/絕對 + 中文專案根。

    搜尋順序：
    1. 依輸入原樣（相對 cwd / 絕對）
    2. 專案根目錄 + basename（處理在 dashcam_player/utils 下執行時的 DEFAULT_VIDEOS）
    3. 專案根目錄 + 原輸入（保留子資料夾結構）
    """
    if os.path.exists(v):
        return v
    # 嘗試專案根
    cand = os.path.join(_proj_root, os.path.basename(v))
    if os.path.exists(cand):
        return cand
    cand2 = os.path.join(_proj_root, v)
    if os.path.exists(cand2):
        return cand2
    # 再嘗試上一層（若 _proj_root 計算偏移）
    for base in [os.getcwd(), _proj_root, os.path.dirname(_proj_root)]:
        for name in [v, os.path.basename(v)]:
            p = os.path.join(base, name)
            if os.path.exists(p):
                return p
    return None


def load_embedded_speeds(video_path: str, nframes: int = 900) -> np.ndarray:
    """從內嵌 GPS 資料軌（欄位 2/10 = km/h）取出速度陣列。"""
    out = os.path.join(os.environ.get("TEMP", "H:/TEMP/opencode"),
                       os.path.basename(video_path).replace(".AVI", ".bin"))
    subprocess.run(
        ["C:/ffmpeg/bin/ffmpeg", "-v", "error", "-y", "-i", video_path,
         "-map", "0:2", "-c", "copy", "-f", "data", out],
        check=True,
    )
    data = open(out, "rb").read().decode("utf-8", errors="replace")
    nums = [int(x) for x in data.replace("\n", "").split("/") if x.strip().isdigit()]
    body = nums[1:]
    recs = [body[i:i + 7] for i in range(0, len(body), 7)]
    gps = np.array([r[1] for r in recs]) / 10.0
    return gps[:nframes] if nframes else gps


def main() -> None:
    videos = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_VIDEOS
    print("標線週期測速驗證 (vs 內嵌 GPS)")
    print(f"專案根: {_proj_root}")
    print(f"當前目錄: {os.getcwd()}")
    print("=" * 70)
    for v in videos:
        resolved = _resolve_video_path(v)
        if resolved is None:
            print(f"=== {v} 不存在，略過 ===")
            print(f"   已搜尋: cwd/{v} , 專案根/{os.path.basename(v)}")
            continue
        if resolved != v:
            print(f"=== {v} -> {resolved} ===")
        else:
            print(f"=== {v} ===")
        v = resolved
        try:
            band = SCAN_BANDS.get(os.path.basename(v), (0.50, 0.64))
            result = analyze_video(v, max_frames=900, scan_band=band)
            if result["speed_kmh"] is None:
                print("  未偵測到有效標線週期")
            else:
                print(f"  >> 標線速度: {result['speed_kmh']:.1f} km/h "
                      f"(週期 {result['median_period']:.3f}s, "
                      f"脈衝 {len(result['onsets'])}, 一致性 {result['consistency']:.2f})")
            try:
                gps = load_embedded_speeds(v)
                print(f"  >> GPS 速度: median={np.median(gps):.1f} "
                      f"p25={np.percentile(gps, 25):.1f} p75={np.percentile(gps, 75):.1f} km/h")
                if result["speed_kmh"] is not None:
                    err = abs(result["speed_kmh"] - np.median(gps)) / np.median(gps) * 100
                    print(f"  >> 誤差: {err:.1f}%")
            except Exception as e:
                print(f"  GPS 提取失敗: {e}")
        except Exception as e:
            print(f"  分析失敗: {e}")
    print("=" * 70)


if __name__ == "__main__":
    main()
