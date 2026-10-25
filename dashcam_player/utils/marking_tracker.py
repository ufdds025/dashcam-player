"""地面標線多物件追蹤模組（支援變換車道的「1 或 2 條線」追蹤）。

背景
----
行車紀錄器測速需要鎖定「地面標線通過參考帶」的週期。傳統單一固定掃描帶
在**變換車道**時會失效——變道後原本在框內的標線被另一條取代，程式無從得知
正在追的是哪一條。

本模組把標線當作**可追蹤的物件**：
  1. 在涵蓋左右車道的寬框內，偵測「暗路面上的亮物件」（虛線），依 x 位置分簇。
  2. 合併同一條線誤拆的多個相鄰峰。
  3. 跨幀指派到持續的 Track（依 x 鄰近度），虛線真實間隙期間以「遺失但延續」
     處理，不自斷。
  4. 每條 Track 維持左/右側別；線出現/消失/換邊 = 變道事件的徵兆。

本模組為純函式，不依賴 Qt，可直接於背景執行緒使用。
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from dashcam_player.utils.cv_utils import open_video_capture

# 側別判定（x 為框內正規化 0..1）
SIDE_LEFT = "left"
SIDE_CENTER = "center"
SIDE_RIGHT = "right"


def detect_dash_bands(
    gray: np.ndarray,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    min_line_ratio: float = 0.25,
    min_sep_ratio: float = 0.03,
    offset_from_median: int = 35,
) -> tuple[list[tuple[int, int]], float]:
    """在框內偵測「暗路面上的亮虛線」x 區間。

    Args:
        gray: 全幀灰階圖。
        x0..y1: 偵測框（絕對座標）。
        min_line_ratio: 一欄內亮像素比例需高於此才視為線的一部分（0..1）。
        min_sep_ratio: 相鄰兩條線最小間距（框寬比例），低於視為同一條合併。
        offset_from_median: 亮物件門檻 = 路面中位數 + 此偏移（避免絕對門檻受
            過曝路段/陰影影響）。

    Returns:
        (intervals, base_brightness)
        intervals: [(x_start_abs, x_end_abs), ...] 已合併、依 x 排序。
        base_brightness: 偵測所用之路面基準亮度。
    """
    sub = gray[y0:y1, x0:x1].astype(np.float32)
    base = float(np.median(sub))
    thr = max(120.0, base + float(offset_from_median))

    bi: np.ndarray = (sub > thr).astype(np.uint8) * 255
    # 開運算去雜訊，再沿 y 連通同一條線的垂直斷塊
    bi = cv2.morphologyEx(
        bi, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    )
    bi = cv2.dilate(bi, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 21)), iterations=1)

    col = bi.mean(axis=0) / 255.0  # 每欄亮像素比例, shape (W,)
    W = col.shape[0]
    intervals: list[tuple[int, int]] = []
    i = 0
    while i < W:
        if col[i] > min_line_ratio:
            j = i
            while j < W and col[j] > min_line_ratio:
                j += 1
            intervals.append((i, j))
            i = j
        else:
            i += 1

    # 合併過近的區間（同一條線誤拆）
    merged: list[tuple[int, int]] = []
    min_sep = int(min_sep_ratio * W)
    for iv in intervals:
        if merged and iv[0] - merged[-1][1] <= min_sep:
            prev = merged.pop()
            merged.append((prev[0], max(prev[1], iv[1])))
        else:
            merged.append(iv)

    # 丟棄觸及框邊緣的區間（被框裁剪的局部物件，非整條線）。
    # 注意：此處 s/e 為「框內相對座標」(0..W)，必須與 0/W 比較，
    # 不可用 x0/x1（整幀絕對座標），否則非左邊界的框會把全部結果濾掉。
    out: list[tuple[int, int]] = []
    for s, e in merged:
        if e - s < 2:
            continue
        if s <= 0 or e >= W:
            continue
        out.append((x0 + s, x0 + e))
    return out, base


@dataclass
class MarkingTrack:
    """單一標線追蹤物件。"""

    track_id: int
    xc: float  # 框內正規化 x 中心 (0..1)，間隙期間以預測值延續
    side: str
    active: bool
    last_seen: int
    birth: int
    persistence: int = 0  # 已連續存活幀數（活躍累增 / 間隙不減）

    def __post_init__(self) -> None:
        if self.side not in (SIDE_LEFT, SIDE_CENTER, SIDE_RIGHT):
            self.side = SIDE_CENTER


def side_of(xc: float, left_ratio: float = 0.32, right_ratio: float = 0.68) -> str:
    """依框內正規化 x 判別側別。"""
    if xc < left_ratio:
        return SIDE_LEFT
    if xc > right_ratio:
        return SIDE_RIGHT
    return SIDE_CENTER


def assign_to_tracks(
    detections: list[tuple[int, int]],
    prev_tracks: list[MarkingTrack],
    band_w: int,
    current_frame: int,
    max_gap_ratio: float = 0.06,
    max_lost_frames: int = 10,
    min_persist_to_keep: int = 3,
    next_track_id: int = 0,
) -> tuple[list[MarkingTrack], list[dict]]:
    """把當前幀偵測到的線指派/維持到持續 Track。

    Args:
        detections: [(x0, x1), ...] 框內絕對 x 區間。
        prev_tracks: 上一幀的 Track（含遺失中的）。
        band_w: 框寬（像素）。
        current_frame: 當前幀號（用於 last_seen / birth）。
        max_gap_ratio: 偵測中心與 track 中心容忍距離（框寬比例）。
        max_lost_frames: track 失聯超過此幀數才終止。
        min_persist_to_keep: 存活達此幀數才視為可靠（過濾單幀雜訊）。
        next_track_id: 分配給新 track 的起始 id。

    Returns:
        (tracks, events)
        tracks: 本幀更新後的 Track 清單（活躍 + 遺失待續）。
        events: spawn / lost 事件描述。
    """
    events: list[dict] = []
    dcx = [
        ((d[0] + d[1]) / 2.0 - band_w // 2) / band_w for d in detections
    ]
    max_gap = max_gap_ratio * band_w

    used_det: set[int] = set()
    matched_idx: set[int] = set()
    result: list[MarkingTrack] = []

    # 由持久優先做最鄰近貪婪指派（距離小者優先）
    ordered = sorted(
        enumerate(prev_tracks),
        key=lambda it: (-it[1].persistence, it[1].track_id),
    )
    for idx, tr in ordered:
        best = None
        best_d = max_gap
        for di, dx in enumerate(dcx):
            if di in used_det:
                continue
            d = abs(dx * band_w - tr.xc * band_w)
            if d < best_d:
                best_d = d
                best = di
        if best is not None:
            used_det.add(best)
            matched_idx.add(idx)
            tr.xc = float(np.clip(dcx[best], 0.0, 1.0))
            tr.side = side_of(tr.xc)
            tr.active = True
            tr.last_seen = current_frame
            tr.persistence += 1
            result.append(tr)
        else:
            # 未配到偵測：遺失但不到期則延續，到期則終止
            if current_frame - tr.last_seen > max_lost_frames:
                if tr.persistence >= min_persist_to_keep:
                    events.append(
                        {"type": "lost", "track_id": tr.track_id, "side": tr.side}
                    )
                continue  # 終止，不續留
            tr.active = False
            result.append(tr)

    # 未被配到的偵測 → 開新 track
    for di, dx in enumerate(dcx):
        if di in used_det:
            continue
        tr = MarkingTrack(
            track_id=next_track_id,
            xc=float(np.clip(dx, 0.0, 1.0)),
            side=side_of(float(np.clip(dx, 0.0, 1.0))),
            active=True,
            last_seen=current_frame,
            birth=current_frame,
            persistence=1,
        )
        next_track_id += 1
        result.append(tr)
        events.append(
            {"type": "spawn", "track_id": tr.track_id, "side": tr.side}
        )

    return result, events


def build_track_timeline(
    video_path: str,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    start: int,
    end: int,
    fps: float = 30.0,
) -> list[dict]:
    """掃完整段影片，輸出每幀的追蹤狀態時間線（供驗證與偵測變道）。

    Returns:
        per-frame dict: {frame, active: [{track_id, xc, side}], lost: [...]}
    """
    cap = open_video_capture(video_path)
    if not cap.isOpened():
        return []
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    fi = start
    band_w = x1 - x0
    tracks: list[MarkingTrack] = []
    nid = 0
    timeline: list[dict] = []
    while fi < end:
        ret, fr = cap.read()
        if not ret:
            break
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        dets, _base = detect_dash_bands(g, x0, y0, x1, y1)
        tracks, events = assign_to_tracks(
            dets, tracks, band_w, fi, next_track_id=nid
        )
        for t in tracks:
            if t.track_id >= nid:
                nid = t.track_id + 1
        timeline.append(
            {
                "frame": fi,
                "active": [
                    {
                        "track_id": t.track_id,
                        "xc": round(float(t.xc), 3),
                        "side": t.side,
                    }
                    for t in tracks
                    if t.active
                ],
                "alive": [
                    {
                        "track_id": t.track_id,
                        "xc": round(float(t.xc), 3),
                        "side": t.side,
                        "persistence": t.persistence,
                    }
                    for t in tracks
                ],
                "spawns": [e for e in events if e["type"] == "spawn"],
                "losts": [e for e in events if e["type"] == "lost"],
            }
        )
        fi += 1
    cap.release()
    return timeline
