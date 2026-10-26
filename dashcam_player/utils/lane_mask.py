"""AI 標線 Mask 訊號源抽象（UFLDv2 Spike 階段）。

背景
----
UFLDv2 輸出的是車道「幾何折線」（連續內插，不隨虛線間隙脈動），不能直接取代
``detect_dash_bands`` 的時序佔比訊號。本模組只提供「折線→Mask→佔比」的純函式
轉接層（無 torch 依賴、可單測），讓 AI 與傳統路徑共用同一套下游
（``_side_crossings`` → 交替 → 閃爍濾波 → JSON），保證 Task C 可公平比對。

Spike 三路訊號（定義見 ``scripts/eval_mask_spike.py``）：
  classical — ``detect_dash_bands`` 佔比（基線，不動）
  ai-conf   — UFLDv2 row-softmax 信心在參考列附近的時序訊號
  ai-geo    — AI 車道橫向位置定窗＋窗內傳統亮度脈衝

本模組僅含 torch-free 的部分；模型載入／推論留在 scripts 端 lazy import，
shipped package import 時不碰 torch。

註：本模組需 ``numpy``（Mask 為 uint8 陣列，cv2.polylines 目標別無他法）。
AGENTS.md「其餘檔案不可新增 import numpy」原意是避免非視覺模組膨脹；
本模組為純視覺轉接層（同 cv_utils/gps_ocr 性質），此處破例並記錄。
"""

from __future__ import annotations

import cv2
import numpy as np

# 光柵化線寬預設（px, 以 2560x1440 全幀計；約近場虛線半寬量級）。
# Spike 會掃 4/8/12 並報告分佈差異，此預設僅為起點，不參與驗收門檻。
DEFAULT_LANE_THICKNESS = 6


def rasterize_lanes(
    lanes: list[list[tuple[float, float]]],
    h: int,
    w: int,
    thickness: int = DEFAULT_LANE_THICKNESS,
) -> np.ndarray:
    """車道折線光柵化為二值 Mask（純函式）。

    Args:
        lanes: 每條車道為 [(x, y), ...] 全幀像素座標；少於 2 點的車道忽略。
        h, w: 全幀尺寸。
        thickness: 線寬（px）。

    Returns:
        uint8 Mask（0/255）。
    """
    mask = np.zeros((h, w), dtype=np.uint8)
    for lane in lanes:
        if len(lane) < 2:
            continue
        pts = np.array(
            [(int(round(x)), int(round(y))) for x, y in lane], dtype=np.int32
        )
        cv2.polylines(mask, [pts], False, 255, max(1, thickness), cv2.LINE_8)
    return mask


def mask_to_occupancy(
    box_mask: np.ndarray,
    divider_ratio: float = 0.5,
) -> tuple[float, float]:
    """Box-crop 後 Mask 轉左右半區佔比（純函式）。

    定義與傳統路徑對齊：佔比 = 含 Mask 像素的欄數 / 半區寬。
    （``detect_dash_bands`` 回傳 x 區間，frac = 區間總寬 / 半區寬；
    欄位計數為其 Mask 等價形式，見 ``intervals_to_mask`` parity test。）

    Args:
        box_mask: 已 crop 至掃描框的二值 Mask（H_box x W_box）。
        divider_ratio: 分割線框內正規化位置（0..1），0.5 為正中。

    Returns:
        (occ_l, occ_r)，0..1。
    """
    if box_mask.size == 0:
        return 0.0, 0.0
    bw = box_mask.shape[1]
    if bw <= 0:
        return 0.0, 0.0
    div = int(round(bw * min(max(divider_ratio, 0.0), 1.0)))
    div = min(max(div, 0), bw)
    col_any = np.asarray((box_mask > 0).any(axis=0), dtype=bool)
    wl = max(div, 1)
    wr = max(bw - div, 1)
    occ_l = float(np.count_nonzero(col_any[:div])) / wl if div > 0 else 0.0
    occ_r = float(np.count_nonzero(col_any[div:])) / wr if div < bw else 0.0
    return min(occ_l, 1.0), min(occ_r, 1.0)


def intervals_to_mask(
    intervals: list[tuple[int, int]],
    box_w: int,
    box_h: int,
    x_offset: int = 0,
) -> np.ndarray:
    """傳統 x 區間轉 Mask（測試 parity 用，非生產路徑）。

    Args:
        intervals: 絕對 x 區間 [(a, b), ...]。
        box_w, box_h: Mask 尺寸（框尺寸）。
        x_offset: 絕對→相對座標偏移（通常為框 x0）。
    """
    mask = np.zeros((box_h, max(box_w, 1)), dtype=np.uint8)
    for a, b in intervals:
        s = max(0, int(a) - x_offset)
        e = min(box_w, int(b) - x_offset)
        if e > s:
            mask[:, s:e] = 255
    return mask


def lane_x_at_y(
    polyline: list[tuple[float, float]],
    y: float,
) -> float | None:
    """折線在指定 y 的線性內插 x（純函式；ai-geo 定窗用）。

    折線 x 應為 y 的函數（車道大致垂直走向）；若 y 超出範圍回 None。
    """
    if len(polyline) < 2:
        return None
    pts = sorted(polyline, key=lambda p: p[1])
    if not (pts[0][1] <= y <= pts[-1][1]):
        return None
    for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
        if y0 <= y <= y1:
            if y1 == y0:
                return float((x0 + x1) / 2.0)
            t = (y - y0) / (y1 - y0)
            return float(x0 + t * (x1 - x0))
    return None


def lane_windows_at_ref(
    lanes: list[list[tuple[float, float]]],
    ref_y: float,
    half_width_px: float,
    frame_w: int,
) -> list[tuple[int, int]]:
    """各車道在參考列上的 x 窗（純函式；ai-geo 定窗用）。

    Args:
        lanes: 全幀座標折線。
        ref_y: 參考列 y（全幀座標）。
        half_width_px: 窗半寬（px）。
        frame_w: 幀寬（clamp 用）。

    Returns:
        [(xa, xb), ...] 依 x 排序、clamp 至幀內；無交點車道不列入。
    """
    wins: list[tuple[int, int]] = []
    for lane in lanes:
        xc = lane_x_at_y(lane, ref_y)
        if xc is None:
            continue
        xa = max(0, int(round(xc - half_width_px)))
        xb = min(frame_w, int(round(xc + half_width_px)))
        if xb > xa:
            wins.append((xa, xb))
    return sorted(wins)
