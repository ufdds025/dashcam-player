"""車體上緣偵測模組（視覺測速 ROI 車體排除用）。

背景
----
視覺測速的自動 ROI 若涵蓋到車輛車體（引擎蓋），車體鏡面反光會被
誤判為車道標線。Phase A 實測（5 支影片 row-wise 曲線）結論：

- 全寬 row 平均看不到邊界（車體只佔中央，兩側仍是路面，會被稀釋）。
- motion（幀間差分）不可用——引擎蓋反光會動，hood 帶 motion 反而是高峰。
- 中央欄 ``x[0.35, 0.65]`` 時間中位數 ``V(y)`` 有穩定階躍：
  路面 ``V≈75-95`` → 車體 ``V≈110-143``，``ΔV≥30``，4 支前鏡頭一致；
  人工框底邊 0.643-0.668 即為此階躍位置。
- 由上往下取「第一個」階躍是關鍵：0.78 處還有更強邊
 （hood 下緣/儀表板交界），取全域最大會錯取 0.78。
- 後鏡頭無車體簽名 → 回 ``None`` 走 fail-safe。

本模組為純函式（``detect_hood_top_from_profile`` 不碰 IO），不依賴 Qt，
可直接於背景執行緒使用。
"""

from __future__ import annotations

# mypy: disable-error-code="var-annotated,operator,arg-type,assignment"
import logging
import os
import sys

# 直接以 `python dashcam_player/utils/hood_detector.py` 執行時，
# __package__ 為 None 且 cwd 不在專案根目錄，導致 ModuleNotFoundError。
# 此段將專案根目錄自動加入 sys.path（與 marking_detector.py 同模式）。
if __package__ in (None, ""):
    _proj_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _proj_root not in sys.path:
        sys.path.insert(0, _proj_root)

from dataclasses import dataclass

import cv2
import numpy as np

from dashcam_player.utils.cv_utils import open_video_capture
from dashcam_player.utils.log import get_logger
from dashcam_player.utils.pts_loader import get_reliable_fps_count

log = get_logger()
log.setLevel(logging.INFO)

# --- 偵測參數（Phase A 數據收斂，見模組 docstring） ---
CENTER_X0 = 0.35  # 中央欄左邊界（車體所在區，全寬平均會稀釋邊界）
CENTER_X1 = 0.65  # 中央欄右邊界
SEARCH_Y0 = 0.50  # 由上往下搜尋起點
SEARCH_Y1 = 0.85  # 由上往下搜尋終點
STEP_WINDOW_RATIO = 0.03  # 階躍量測窗（H 比例）；緩坡車體另以 STEP_WINDOW_WIDE 補捉
STEP_WINDOW_WIDE = 0.06  # 寬窗：正午強光下 hood 階躍攤成緩坡（091049-F 實測，窗 0.03 內僅 +10）
MIN_STEP_DELTA_V = 25.0  # 最小亮度躍變（實測 ΔV 33-54）
WIDE_MIN_STEP_DELTA_V = 20.0  # 寬窗門檻：同樣 +40 攤在 0.10H 緩坡上時，
# 窄窗（0.03H）內僅 +12、寬窗（0.06H）內約 +24，須略放寬才有餘裕；
# 後鏡頭在寬窗+20 下仍無簽名（tests 回歸鎖定）
MIN_POST_STEP_V = 110.0  # 躍後亮度下限（實測車體 V 110-143）
N_SAMPLES = 12  # 取樣幀數（10%-90% 均分）
MIN_SAMPLES = 3  # 最少可用幀數

# 無偵測時的保守預設底邊：4 支前鏡頭人工框底邊最小值 0.643 再扣一點。
# 呼叫端（SpeedROIDetector / SpeedWorker）在回 None 時使用此值，
# 取代舊的 0.75/0.76/0.85 寫死底邊（含車體反光帶）。
CONSERVATIVE_FALLBACK_Y = 0.64


@dataclass
class HoodDetectionResult:
    """車體上緣偵測結果。"""

    video_path: str
    hood_top_y: float  # 車體上緣（正規化 0-1，ROI 底邊應設於此之上）
    confidence: float  # 0-1（階躍強度正規化）
    n_samples: int  # 實際取樣幀數


def _smooth_1d(x: np.ndarray, win: int = 15) -> np.ndarray:
    """1D 移動平均（奇窗，same 卷積）"""
    win = max(1, win | 1)
    kernel = np.ones(win) / float(win)
    return np.convolve(x, kernel, mode="same")


def median_v_profile(frames: list[np.ndarray]) -> np.ndarray | None:
    """中央欄時間中位數 V(y) 曲線。

    時間中位數可濾除移動車輛等暫態亮體，留下路面/車體靜態亮度結構。
    回傳長度 H 的 float32 陣列，幀數不足回 None。
    """
    if len(frames) < MIN_SAMPLES:
        return None
    h, w = frames[0].shape[:2]
    # 防禦：中途解析度變化（實務極罕見）時丟棄異形幀，避免 np.stack 拋 ValueError
    frames = [f for f in frames if f.shape[:2] == (h, w)]
    if len(frames) < MIN_SAMPLES:
        return None
    cx0, cx1 = int(w * CENTER_X0), int(w * CENTER_X1)
    if cx1 <= cx0:
        return None
    try:
        stack = np.stack(
            [cv2.cvtColor(f, cv2.COLOR_BGR2HSV)[:, cx0:cx1, 2].astype(np.float32) for f in frames],
            axis=0,
        )
    except cv2.error:
        return None
    return np.asarray(np.median(stack, axis=0).mean(axis=1), dtype=np.float32)


def detect_hood_top_from_profile(
    v_profile: np.ndarray,
    search_y0: float = SEARCH_Y0,
    search_y1: float = SEARCH_Y1,
    step_window_ratio: float = STEP_WINDOW_RATIO,
    min_step_delta_v: float = MIN_STEP_DELTA_V,
    min_post_step_v: float = MIN_POST_STEP_V,
) -> float | None:
    """從中央欄 V(y) 曲線找車體上緣（純函式，可單測）。

    由上往下找第一個「持續正階躍」：窗內 ``ΔV >= min_step_delta_v``
    且躍後 ``V >= min_post_step_v``，後續 0.02H 不跌回（容忍 10）。
    由上往下取第一個是刻意的——0.78 處另有更強邊（hood 下緣），
    取全域最大會錯取（Phase A 實測）。

    窄窗（0.03H/門檻 25）優先，找不到時以寬窗（0.06H/門檻 20）重掃一次——
    正午強光下 hood 階躍會攤成緩坡（D 槽 091049-F 實測：窄窗內僅 +10，
    寬窗內約 +20-24）。若呼叫端傳入的窗寬已 >= 寬窗則只掃一次（避免重複）。

    Returns:
        hood_top_y（正規化 0-1）或 None（無車體簽名）。
    """
    scans = [(step_window_ratio, min_step_delta_v)]
    if step_window_ratio < STEP_WINDOW_WIDE:
        scans.append((STEP_WINDOW_WIDE, WIDE_MIN_STEP_DELTA_V))
    for ratio, thresh in scans:
        got = _scan_first_step(
            v_profile, search_y0, search_y1, ratio, thresh, min_post_step_v
        )
        if got is not None:
            return got[0]
    return None


def _scan_first_step(
    v_profile: np.ndarray,
    search_y0: float,
    search_y1: float,
    step_window_ratio: float,
    min_step_delta_v: float,
    min_post_step_v: float,
) -> tuple[float, float] | None:
    """單一窗寬的由上往下首階躍掃描。回傳 (hood_top_y, step) 或 None。"""
    h = int(len(v_profile))
    if h < 50:
        return None
    vs = _smooth_1d(np.asarray(v_profile, dtype=np.float64), 15)
    win = max(3, int(h * step_window_ratio))
    y_start = max(0, int(h * search_y0))
    y_end = min(h - win - 1, int(h * search_y1) - win)
    if y_end <= y_start:
        return None
    for y in range(y_start, y_end):
        dv = float(vs[y + win] - vs[y])
        if dv >= min_step_delta_v and float(vs[y + win]) >= min_post_step_v:
            y2 = min(h - 1, y + win + int(h * 0.02))
            if float(vs[y2]) >= float(vs[y + win]) - 10.0:
                return (y + win / 2) / h, dv
    return None


def _detect_with_step(v_profile: np.ndarray) -> tuple[float | None, float | None]:
    """雙窗首階躍掃描，回傳 (hood_top_y, step)。step 為實際檢出窗的躍變量，
    供 confidence 計算（不可用固定窄窗重算，見 detect_hood_top 註解）。"""
    for ratio, thresh in (
        (STEP_WINDOW_RATIO, MIN_STEP_DELTA_V),
        (STEP_WINDOW_WIDE, WIDE_MIN_STEP_DELTA_V),
    ):
        got = _scan_first_step(
            v_profile, SEARCH_Y0, SEARCH_Y1, ratio, thresh, MIN_POST_STEP_V
        )
        if got is not None:
            return got[0], got[1]
    return None, None


def _sample_frames(video_path: str, n: int = N_SAMPLES) -> tuple[list[np.ndarray], int]:
    """均分取樣 10%-90% 區間 n 幀。回傳 (frames, height)。"""
    cap = open_video_capture(video_path)
    if not cap.isOpened():
        return [], 0
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        # 檔頭損壞的 AVI（宣稱 600fps/26477 幀）會讓取樣位置落在不存在的幀上，
        # 以 ffprobe 的實際幀數取代檔頭值（與 speed_roi_detector.py 同模式）。
        try:
            reliable = get_reliable_fps_count(video_path)
            if reliable is not None:
                total = reliable[1]
        except Exception:
            pass
        if total <= 0:
            return [], 0
        lo = int(total * 0.10)
        hi = int(total * 0.90)
        if hi <= lo:
            lo, hi = 0, max(total - 1, 0)
        frames: list[np.ndarray] = []
        for i in range(n):
            idx = int(lo + (hi - lo) * i / max(n - 1, 1)) if n > 1 else lo
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, fr = cap.read()
            if ret and fr is not None:
                frames.append(fr)
        h = int(frames[0].shape[0]) if frames else 0
        return frames, h
    finally:
        cap.release()


def detect_hood_top(video_path: str, n_samples: int = N_SAMPLES) -> HoodDetectionResult | None:
    """偵測影片車體上緣。無車體簽名（後鏡頭等）回 None，呼叫端用保守預設。

    Args:
        video_path: 影片路徑（可含中文/空白，經 open_video_capture 開啟）
        n_samples: 取樣幀數

    Returns:
        HoodDetectionResult 或 None（無法開啟 / 幀數不足 / 無階躍簽名）。
    """
    frames, _h = _sample_frames(video_path, n_samples)
    if len(frames) < MIN_SAMPLES:
        log.info(f"HoodDetector: 取樣幀數不足 ({len(frames)})，{video_path}")
        return None
    profile = median_v_profile(frames)
    if profile is None:
        return None
    hood_top, step = _detect_with_step(profile)
    if hood_top is None or step is None:
        log.info(f"HoodDetector: {video_path} 無車體階躍簽名（可能無車體入鏡）")
        return None
    # 信心度：實際檢出窗的階躍強度相對門檻的超出比（上限 1.0）。
    # ⚠️ 不可用固定窄窗重算——寬窗檢出（如緩坡）在窄窗內 step 僅 +10，會被誤算成 0。
    confidence = max(0.0, min(1.0, (step - MIN_STEP_DELTA_V) / 30.0 + 0.5))
    log.info(
        f"HoodDetector: {video_path} -> hood_top_y={hood_top:.3f}, "
        f"step={step:.1f}, confidence={confidence:.2f}"
    )
    return HoodDetectionResult(
        video_path=video_path,
        hood_top_y=float(hood_top),
        confidence=float(confidence),
        n_samples=len(frames),
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="車體上緣偵測 - 單檔測試（支援中文路徑）")
    parser.add_argument("video", help="影片路徑（可含中文/空白）")
    args = parser.parse_args()

    res = detect_hood_top(args.video)
    if res is None:
        print(f"無車體簽名，建議保守底邊 {CONSERVATIVE_FALLBACK_Y}")
    else:
        print(f"hood_top_y={res.hood_top_y:.3f}, confidence={res.confidence:.2f}, n={res.n_samples}")
