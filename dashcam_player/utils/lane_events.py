"""車道事件偵測共用函式庫（變道過渡 / 單側實線 / 標記中斷）。

背景
----
自動標線候選（manual_annotator）在直行虛線段可用，但對「變道」無識別能力：
固定掃描框＋固定中線，標線橫越中線時半區歸屬跳變，產生過渡誤報＋漏檢；
變道後常見「單側虛線＋另一側實線」，實線側佔比恆高閂鎖，需註記而非當漏檢。

本模組職責：
  1. Task C 驗收指標（auto-bw vs 人工 cross，per-side greedy 配對）。
  2. 佔比序列 → 車道事件（純函式，訊號分佈解釋門檻，不讀 GPS）。
  3. cross 真值 → 事件（供掃描規則驗證）。
  4. Markdown 人類可讀報告（人工讀後再用標記軟體處理）。

門檻選定依據（工作單規則 3，只用訊號本身分佈）：
  - 實線恆高線 OCC_SOLID_MIN=0.05：實測 091548-F 實線側佔比 0.19~0.28，
    虛線側在兩次通過之間恆為 0.0；0.05 遠高於雜訊底（<0.02）且遠低於
    實線常駐值，兩側分離充足。
  - 持續幀數以「虛線週期」為尺：直行段 bw-bw 中位週期 9~11 幀；連續 150 幀
    （約 14 個週期）佔比不歸零，不可能全是漏檢 → 實線或靜止。
  - 單側中斷 90 幀（約 8 個週期）：短於此的缺口可能是 2~3 次連續漏檢，
    不單獨成事件；變道過渡實測缺口 40~60 幀，單側長駐可達千幀。

本模組為純函式＋薄 IO，不依賴 Qt，可直接於背景執行緒使用。
"""

from __future__ import annotations

# mypy: disable-error-code="var-annotated,operator,arg-type,assignment"
import glob
import json
import os
import statistics

import cv2

from dashcam_player.utils.cv_utils import open_video_capture
from dashcam_player.utils.log import get_logger
from dashcam_player.utils.marking_tracker import detect_dash_bands

log = get_logger()

TASK_C_TOL = 1.0
TASK_C_RECALL = 0.90
SIDES = ("left", "right")

# --- 掃描規則常數（見模組 docstring 選定依據） ---
OCC_SOLID_MIN = 0.05  # 實線恆高判定線（實測實線 0.19~0.28 / 虛線間隙 0.0 / 雜訊 <0.02）
SOLID_MIN_FRAMES = 150  # 恆高持續此幀數以上判實線段（約 14 個虛線週期）
INTERRUPT_MIN_FRAMES = 90  # 單側穿越中斷此幉數以上成事件（約 8 個週期）
GAP_MIN_FRAMES = 60  # 雙側同時中斷此幀數以上成事件
TRANS_HALO_FRAMES = 60  # 中斷起點往前回溯此範圍找對側缺口，界定過渡窗
EVENT_MERGE_GAP = 30  # 同型事件間隔小於此幀數合併
MIN_PERIOD_FRAMES = 5  # 真值 binning 用：週期下限（過濾毛刺）
# 過渡窗（coincident-gap）：兩側缺口重疊即疑變道。直行段 bw-bw 中位週期
# 9~11 幀，缺口 >25 幀（約 2.5 個週期無穿越）已不可能是連續漏檢；
# 對側確認缺口 ≥15 幀（排除單幀閃爍）。窗邊界外擴 15/30 幀供人工定位。
TRANS_GAP_MAIN = 25
TRANS_GAP_CONFIRM = 15
TRANS_PAD_BEFORE = 15
TRANS_PAD_AFTER = 30
TRANS_SOLO_MIN = 35
TRANS_SOLO_MAX = 150
# ^ 單側暫時中斷（缺口會結束，長度 35~150 幀）：變道時舊線離開半區、新線
#   尚未進入，留下數十幀空窗（實測 39~50 幀）。下限 35 排除 2~3 次連續
#   漏檢（~30 幀）；上限 150，永久單側另由 single/solid 處理。

# 無標定框影片的通用掃描帶（近場地面，與 marking_detector 預設帶同理；
# 左右以畫面中央切分，僅為候選定位，人工框選後為準，報告會註記）。
GENERIC_BAND = (0.30, 0.85, 0.55, 0.65)  # x0_frac, x1_frac, y0_frac, y1_frac


# ---------------------------------------------------------------------------
# Task C 驗收指標
# ---------------------------------------------------------------------------
def _pct(data: list[float], q: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * q / 100.0
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def load_events(path: str) -> tuple[list[dict], list[dict]]:
    """回傳 (cross真值, auto_bw候選)。cross 皆為前端穿越（=出現語意）。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    anns = data.get("annotations", [])
    cross = [a for a in anns if a.get("kind") == "cross"]
    auto_bw = [a for a in anns if a.get("auto") and a.get("type") == "bw"]
    return cross, auto_bw


def load_scan_box(path: str) -> tuple[int, int, int, int] | None:
    """讀標定檔 scan_box（無則回 None，呼叫端改用通用帶）。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        b = data.get("scan_box")
        if not b:
            return None
        r = (int(b["x0"]), int(b["y0"]), int(b["x1"]), int(b["y1"]))
        if 0 <= r[0] < r[2] and 0 <= r[1] < r[3]:
            return r
    except Exception as e:
        log.warning(f"讀 scan_box 失敗 {path}: {e}")
    return None


def match_side(
    auto: list[float], truth: list[float], tol: float
) -> tuple[int, list[tuple[float, float]]]:
    """同側 greedy 最近鄰配對。回傳 (命中數, [(auto, truth), ...])。"""
    used = [False] * len(truth)
    pairs: list[tuple[float, float]] = []
    for ab in sorted(auto):
        best_i = -1
        best_d = tol + 1e-9
        for i, t in enumerate(truth):
            if used[i]:
                continue
            d = abs(ab - t)
            if d <= tol and d < best_d:
                best_d = d
                best_i = i
        if best_i >= 0:
            used[best_i] = True
            pairs.append((ab, truth[best_i]))
    return len(pairs), pairs


def bias_stats(auto: list[float], truth: list[float]) -> dict:
    """auto 相對最近真值的 signed diff 分佈（auto - truth，負=超前）。"""
    if not auto or not truth:
        return {"n": 0, "median": 0.0, "p10": 0.0, "p90": 0.0}
    diffs = [ab - min(truth, key=lambda t: abs(ab - t)) for ab in auto]
    return {
        "n": len(diffs),
        "median": statistics.median(diffs),
        "p10": _pct(diffs, 10),
        "p90": _pct(diffs, 90),
    }


def evaluate_file(
    path: str, tol: float = TASK_C_TOL, w0: float = 0.0, w1: float = float("inf")
) -> dict:
    """評估單一標定檔，可限幀窗 [w0, w1)。負幀候選（插值邊界產物）不計入。"""
    cross, auto_bw = load_events(path)
    per_side: dict[str, dict] = {}
    for side in SIDES:
        tr = sorted(
            float(a["frame"])
            for a in cross
            if a.get("side") == side and w0 <= float(a["frame"]) < w1
        )
        au = sorted(
            float(a["frame"])
            for a in auto_bw
            if a.get("side") == side and 0 <= float(a["frame"]) and w0 <= float(a["frame"]) < w1
        )
        hits, _ = match_side(au, tr, tol)
        per_side[side] = {
            "truth": len(tr),
            "auto": len(au),
            "hits": hits,
            "recall": hits / len(tr) if tr else 1.0,
            "precision": hits / len(au) if au else 1.0,
            "bias": bias_stats(au, tr),
        }
    nt = sum(v["truth"] for v in per_side.values())
    na = sum(v["auto"] for v in per_side.values())
    nh = sum(v["hits"] for v in per_side.values())
    return {
        "file": os.path.basename(path),
        "tol": tol,
        "per_side": per_side,
        "recall": nh / nt if nt else 1.0,
        "precision": nh / na if na else 1.0,
        "pass_task_c": (nh / nt if nt else 1.0) >= TASK_C_RECALL,
    }


# ---------------------------------------------------------------------------
# 佔比序列量測（IO 邊界）＋ 事件偵測（純函式）
# ---------------------------------------------------------------------------
def resolve_box(
    video_path: str, annotations_path: str | None = None
) -> tuple[tuple[int, int, int, int], str]:
    """決定掃描框：有標定檔用其 scan_box，否則通用近場帶。回傳 (box, 來源註記)。

    框座標為絕對像素 (x0, y0, x1, y1)，需影片尺寸換算通用帶時開檔讀檔頭。
    """
    if annotations_path and os.path.exists(annotations_path):
        box = load_scan_box(annotations_path)
        if box is not None:
            return box, "scan_box"
    cap = open_video_capture(video_path)
    try:
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 2560
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1440
    finally:
        cap.release()
    fx0, fx1, fy0, fy1 = GENERIC_BAND
    return (int(w * fx0), int(h * fy0), int(w * fx1), int(h * fy1)), "generic_band"


def compute_occupancy_series(
    video_path: str,
    box: tuple[int, int, int, int],
    stride: int = 3,
    max_frames: int = 0,
) -> tuple[list[int], list[float], list[float]]:
    """每 stride 幀取樣左右半區亮線佔比。回傳 (幀號, occ_l, occ_r)。

    佔比 = 偵測到的亮線總寬 / 半區寬（0~1）。

    ⚠️ stride 上限（混疊約束）：虛線亮態 dwell 高速時僅 ~3.3 幀（140km/h），
    stride 必須小於亮態持續幀數，否則取樣恰好全部落在暗態、造出百幀級
    假歸零（實測：stride=5 在 10 幀週期虛線上造出 f890-1019 假 single，
    同檔 stride=3 無事件）。本專案影片 45~70km/h（dwell 8~11 幀），
    stride=3 安全；未知車速影片用 stride=2。
    """
    x0, y0, x1, y1 = box
    div_x = (x0 + x1) // 2
    cap = open_video_capture(video_path)
    frames: list[int] = []
    occ_l: list[float] = []
    occ_r: list[float] = []
    try:
        if not cap.isOpened():
            return frames, occ_l, occ_r
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        limit = min(max_frames, total) if max_frames > 0 else total
        fi = 0
        while True:
            ret, fr = cap.read()
            if not ret or fr is None or fi >= limit:
                break
            if fi % stride == 0:
                g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
                dl, _ = detect_dash_bands(g, x0, y0, div_x, y1)
                dr, _ = detect_dash_bands(g, div_x, y0, x1, y1)
                wl = max(div_x - x0, 1)
                wr = max(x1 - div_x, 1)
                occ_l.append(min(sum(b - a for a, b in dl) / wl, 1.0))
                occ_r.append(min(sum(b - a for a, b in dr) / wr, 1.0))
                frames.append(fi)
            fi += 1
    finally:
        cap.release()
    return frames, occ_l, occ_r


def _runs_above(occ: list[float], thr: float) -> list[tuple[int, int]]:
    """佔比持續高於 thr 的 index 區間（含端點）。"""
    runs: list[tuple[int, int]] = []
    s: int | None = None
    for i, v in enumerate(occ):
        if v > thr:
            if s is None:
                s = i
        elif s is not None:
            runs.append((s, i - 1))
            s = None
    if s is not None:
        runs.append((s, len(occ) - 1))
    return runs


def _runs_zero(occ: list[float]) -> list[tuple[int, int]]:
    """佔比等於 0 的 index 區間（含端點）。"""
    return _runs_above([-v for v in occ], -1e-9)


def _to_frames(
    runs: list[tuple[int, int]], frames: list[int], stride: int
) -> list[tuple[int, int]]:
    """index 區間轉幀號區間（尾端含 stride 內未取樣幀）。"""
    return [(frames[s], frames[e] + stride - 1) for s, e in runs]


def _merge(events: list[dict], last_sampled: int | None = None) -> list[dict]:
    """同型且間隔 < EVENT_MERGE_GAP 的事件合併（純函式）。

    last_sampled 給定時，所有 end clamp 至此（不外推超過取樣末幀）。
    """
    if not events:
        return []
    evs = sorted(events, key=lambda e: (e["type"], e["start"]))
    out: list[dict] = [dict(evs[0])]
    for e in evs[1:]:
        p = out[-1]
        if e["type"] == p["type"] and e["start"] - p["end"] <= EVENT_MERGE_GAP:
            p["end"] = max(p["end"], e["end"])
        else:
            out.append(dict(e))
    if last_sampled is not None:
        for e in out:
            e["end"] = min(e["end"], last_sampled)
    return sorted(out, key=lambda e: e["start"])


def detect_lane_events(
    frames: list[int],
    occ_l: list[float],
    occ_r: list[float],
    stride: int = 3,
) -> list[dict]:
    """佔比序列轉車道事件（純函式，可單測）。

    事件型別：
      solid_left / solid_right — 該側佔比恆高 ≥SOLID_MIN_FRAMES，
        且對側在此段有起伏（非雙側靜止）。
      single_left / single_right — 該側佔比歸零 ≥INTERRUPT_MIN_FRAMES，
        對側同時有訊號（變道後單側段；x_side 指「僅存側」）。
      gap_both — 雙側同時歸零 ≥GAP_MIN_FRAMES（無標線段，如路口）。
      transition — 中斷起點前 TRANS_HALO 內對側亦有缺口（變道過渡窗）。

    回傳 [{"type","start","end","detail"}, ...] 依 start 排序。
    事件 end 一律 clamp 到取樣末幀（尾段 stride 內未取樣幀不外推超過片尾）。
    """
    events: list[dict] = []
    if len(frames) < 10:
        return events
    n = len(frames)
    last_sampled = frames[-1]

    def _has_signal(occ: list[float], s: int, e: int) -> bool:
        seg = occ[s : e + 1]
        return any(v <= 0 for v in seg) and any(v > 0 for v in seg)

    for side, occ, other in (("left", occ_l, occ_r), ("right", occ_r, occ_l)):
        # 實線段：恆高 runs 達幀數門檻，且對側有起伏（排除雙側靜止如停等紅燈）
        for s, e in _runs_above(occ, OCC_SOLID_MIN):
            f0, f1 = frames[s], frames[e] + stride - 1
            if f1 - f0 + 1 >= SOLID_MIN_FRAMES and _has_signal(other, s, e):
                events.append({
                    "type": f"solid_{side}",
                    "start": f0, "end": f1,
                    "detail": f"{side} 佔比恆高 {f1 - f0 + 1} 幀，對側有起伏",
                })
        # 單側中斷：歸零 runs 達門檻，且對側同時有訊號
        for s, e in _runs_zero(occ):
            f0, f1 = frames[s], frames[e] + stride - 1
            if f1 - f0 + 1 < INTERRUPT_MIN_FRAMES:
                continue
            if not _has_signal(other, s, e):
                continue
            other_side = "right" if side == "left" else "left"
            events.append({
                "type": f"single_{other_side}",
                "start": f0, "end": f1,
                "detail": f"僅 {other_side} 有訊號（{side} 歸零 {f1 - f0 + 1} 幀）",
            })

    # 雙側同時歸零
    zero_l = {i for r in _runs_zero(occ_l) for i in range(r[0], r[1] + 1)}
    zero_r = {i for r in _runs_zero(occ_r) for i in range(r[0], r[1] + 1)}
    both = sorted(zero_l & zero_r)
    if both:
        s = p = both[0]
        for i in both[1:]:
            if i == p + 1:
                p = i
                continue
            f0, f1 = frames[s], frames[p] + stride - 1
            if f1 - f0 + 1 >= GAP_MIN_FRAMES:
                events.append({"type": "gap_both", "start": f0, "end": f1,
                               "detail": f"雙側無訊號 {f1 - f0 + 1} 幀"})
            s = p = i
        f0, f1 = frames[s], frames[p] + stride - 1
        if f1 - f0 + 1 >= GAP_MIN_FRAMES:
            events.append({"type": "gap_both", "start": f0, "end": f1,
                           "detail": f"雙側無訊號 {f1 - f0 + 1} 幀"})

    # 變道過渡窗（coincident-gap）：一側缺口 ≥TRANS_GAP_MAIN 且對側
    # 同期亦有缺口 ≥TRANS_GAP_CONFIRM → 標線正在橫越中線。窗邊界以外擴
    # 供人工定位；與 single/solid 獨立觸發（不依賴其實線宣告延遲）。
    zr_l = [(frames[s], frames[e] + stride - 1) for s, e in _runs_zero(occ_l)]
    zr_r = [(frames[s], frames[e] + stride - 1) for s, e in _runs_zero(occ_r)]
    for (a0, a1), (b0, b1) in (
        [(a, b) for a in zr_l for b in zr_r] + [(b, a) for a in zr_l for b in zr_r]
    ):
        if a1 - a0 + 1 < TRANS_GAP_MAIN or b1 - b0 + 1 < TRANS_GAP_CONFIRM:
            continue
        lo, hi = max(a0, b0), min(a1, b1)
        if hi < lo:
            continue
        # 窗錨定在「較短缺口」（暫時中斷側）：永久單側缺口只當背景，
        # 不得把窗起點拉回到數百幀前。
        if (a1 - a0) <= (b1 - b0):
            s0, s1 = a0, a1
        else:
            s0, s1 = b0, b1
        events.append({
            "type": "transition",
            "start": max(0, s0 - TRANS_PAD_BEFORE),
            "end": s1 + TRANS_PAD_AFTER,
            "detail": f"雙側缺口重疊 {hi - lo + 1} 幀（疑變道）",
        })

    # 單側暫時中斷：歸零段長度在 [SOLO_MIN, SOLO_MAX]、會結束（不到序列尾）、
    # 對側同期有訊號 → 變道空窗（舊線已出、新線未入）。
    last_frame = frames[-1]
    for side, occ, other in (("left", occ_l, occ_r), ("right", occ_r, occ_l)):
        for s, e in _runs_zero(occ):
            f0, f1 = frames[s], frames[e] + stride - 1
            length = f1 - f0 + 1
            if not (TRANS_SOLO_MIN <= length <= TRANS_SOLO_MAX):
                continue
            if f1 >= last_frame - stride:
                continue
            if not _has_signal(other, s, e):
                continue
            events.append({
                "type": "transition",
                "start": max(0, f0 - TRANS_PAD_BEFORE),
                "end": min(f1 + TRANS_PAD_AFTER, last_sampled),
                "detail": f"{side} 暫斷 {length} 幀後恢復（疑變道空窗）",
            })
    _ = n
    return _merge(events, last_sampled)


# ---------------------------------------------------------------------------
# 真值事件（供掃描規則驗證）＋ 驗證指標
# ---------------------------------------------------------------------------
def _cross_gaps(cross: list[dict], side: str, total_frames: int) -> list[tuple[int, int]]:
    """某側相鄰穿越間隔 >TRANS_GAP_MAIN 的缺口（含頭尾邊界）。"""
    fs = sorted(float(a["frame"]) for a in cross if a.get("side") == side)
    gaps: list[tuple[int, int]] = []
    prev = 0.0
    for f in fs:
        if f - prev > TRANS_GAP_MAIN:
            gaps.append((int(prev), int(f)))
        prev = f
    if total_frames - prev > TRANS_GAP_MAIN:
        gaps.append((int(prev), total_frames - 1))
    return gaps


def truth_events_from_cross(
    cross: list[dict], total_frames: int, bin_frames: int = 30
) -> list[dict]:
    """cross 真值轉事件（僅用於驗證掃描規則，不參與偵測）。

    single_*：每 bin_frames 一格，某側連續 ≥3 格零穿越且對側有穿越。
    transition：兩側穿越缺口（間隔 >TRANS_GAP_MAIN）相互重疊，
      且至少一側缺口會結束（排除片尾永久單側），窗取
      [較早起點-15, 較短缺口終點+30]。
    """
    events: list[dict] = []
    if not cross or total_frames <= 0:
        return events
    nbins = (total_frames + bin_frames - 1) // bin_frames
    has = {"left": [False] * nbins, "right": [False] * nbins}
    for a in cross:
        b = min(int(float(a.get("frame", 0)) // bin_frames), nbins - 1)
        has[a.get("side", "left")][b] = True
    for side in SIDES:
        other = "right" if side == "left" else "left"
        i = 0
        while i < nbins:
            if not has[side][i] and has[other][i]:
                j = i
                while j + 1 < nbins and not has[side][j + 1] and has[other][j + 1]:
                    j += 1
                if j - i + 1 >= 3:
                    f0, f1 = i * bin_frames, min((j + 1) * bin_frames - 1, total_frames - 1)
                    events.append({"type": f"single_{other}", "start": f0, "end": f1,
                                   "detail": "真值：僅一側有穿越"})
                i = j + 1
            else:
                i += 1
    gaps_l = _cross_gaps(cross, "left", total_frames)
    gaps_r = _cross_gaps(cross, "right", total_frames)
    for (a0, a1) in gaps_l:
        for (b0, b1) in gaps_r:
            lo, hi = max(a0, b0), min(a1, b1)
            if hi < lo:
                continue
            # 永久單側（缺口延續到片尾且另一側亦然）不算過渡
            if a1 >= total_frames - 1 and b1 >= total_frames - 1:
                continue
            # 窗錨定在較短缺口（同偵測側，避免永久單側把窗拉成千幀怪物）
            if (a1 - a0) <= (b1 - b0):
                s0, s1 = a0, a1
            else:
                s0, s1 = b0, b1
            events.append({"type": "transition",
                           "start": max(0, s0 - TRANS_PAD_BEFORE),
                           "end": s1 + TRANS_PAD_AFTER,
                           "detail": "真值：雙側穿越缺口重疊"})
    # 單側暫時中斷真值：缺口會結束、長度在窗內、對側同期有穿越
    by_side = {
        s: sorted(float(a["frame"]) for a in cross if a.get("side") == s)
        for s in SIDES
    }
    for side in SIDES:
        other = "right" if side == "left" else "left"
        for a0, a1 in _cross_gaps(cross, side, total_frames):
            length = a1 - a0
            if not (TRANS_SOLO_MIN <= length <= TRANS_SOLO_MAX):
                continue
            if a1 >= total_frames - 1:
                continue
            if not any(a0 <= f <= a1 for f in by_side[other]):
                continue
            events.append({"type": "transition",
                           "start": max(0, a0 - TRANS_PAD_BEFORE),
                           "end": a1 + TRANS_PAD_AFTER,
                           "detail": "真值：單側暫斷後恢復"})
    return _merge(sorted(events, key=lambda e: e["start"]))


def _overlap(a0: int, a1: int, b0: int, b1: int) -> float:
    """a 相對於 min(len(a), len(b)) 的重疊比（短事件被長事件覆蓋即算命中）。"""
    lo, hi = max(a0, b0), min(a1, b1)
    if hi < lo:
        return 0.0
    denom = min(a1 - a0 + 1, b1 - b0 + 1)
    return (hi - lo + 1) / denom if denom > 0 else 0.0


def validate_events(
    detected: list[dict], truth: list[dict], min_overlap: float = 0.5
) -> dict:
    """事件級驗證：同型且重疊比 ≥min_overlap 即命中。回傳 precision/recall。

    型別對應：single_* 互相比（僅存側須一致）、transition 對 transition、
    solid_* 對 single_*（真值無實線註記前，以單側段為實線代理真值）。
    """

    def _norm(t: str) -> str:
        # solid_X（X 側為實線）⟺ 僅另一側有穿越，即 single_{other}
        if t == "solid_left":
            return "single_right"
        if t == "solid_right":
            return "single_left"
        return t

    det = [e for e in detected if _norm(e["type"]) in
           {"single_left", "single_right", "transition", "gap_both"}]
    tru = [e for e in truth if e["type"] in
           {"single_left", "single_right", "transition"}]
    used = [False] * len(tru)
    hits = 0
    for d in det:
        for i, t in enumerate(tru):
            if used[i] or _norm(d["type"]) != t["type"]:
                continue
            if _overlap(d["start"], d["end"], t["start"], t["end"]) >= min_overlap:
                used[i] = True
                hits += 1
                break
    return {
        "detected": len(det),
        "truth": len(tru),
        "hits": hits,
        "precision": hits / len(det) if det else 1.0,
        "recall": hits / len(tru) if tru else 1.0,
    }


# ---------------------------------------------------------------------------
# 人類可讀報告
# ---------------------------------------------------------------------------
def _fmt_time(frame: int, fps: float) -> str:
    return f"{frame / fps:.1f}s" if fps > 0 else f"f{frame}"


def render_markdown_report(
    video_path: str,
    fps: float,
    total_frames: int,
    box_source: str,
    box: tuple[int, int, int, int],
    events: list[dict],
    task_c: dict | None = None,
) -> str:
    """單支影片的報告段落（人工閱讀後再用標記軟體處理）。"""
    name = os.path.basename(video_path)
    lines = [
        f"## {name}",
        "",
        f"- 影片：`{video_path}`",
        f"- fps={fps:.2f}，總幀={total_frames}（約 {total_frames / fps:.0f}s）" if fps > 0 else f"- 總幀={total_frames}",
        f"- 掃描框：x={box[0]}..{box[2]}, y={box[1]}..{box[3]}（來源：{box_source}）",
    ]
    if box_source == "generic_band":
        lines.append("  - [注意] 通用帶（未經人工框選），左右僅為畫面中央切分，事件位置為候選，需人工框選後為準")
    if task_c is not None:
        lines.append(
            f"- Task C（auto-bw vs cross）：R={task_c['recall']:.2f} P={task_c['precision']:.2f}"
        )
    lines.append("")
    if not events:
        lines.extend(["無車道事件（全片雙側正常）。", ""])
        return "\n".join(lines)
    lines.extend(["| 起止幀 | 時間 | 型別 | 說明 | 建議動作 |", "|---|---|---|---|---|"])
    for e in events:
        t = f"{_fmt_time(e['start'], fps)}–{_fmt_time(e['end'], fps)}"
        if e["type"] == "transition":
            act = "人工確認過渡誤報/漏檢（r 刪 / b-w 補）"
        elif e["type"].startswith("single_"):
            act = "抽查＋實線段起止註記（events[]）"
        elif e["type"].startswith("solid_"):
            act = "實線段起止註記，不逐幀"
        else:
            act = "人工看畫面裁定"
        lines.append(f"| f{e['start']}–f{e['end']} | {t} | {e['type']} | {e['detail']} | {act} |")
    lines.append("")
    return "\n".join(lines)


def find_project_annotations(root: str) -> dict[str, str]:
    """專案根目錄下 stem → annotations.json 路徑（供 resolve 掃描框）。

    key 為去後綴小寫 stem（如 pic20260825-180655-f），與影片檔名 stem 對齊。
    """
    out: dict[str, str] = {}
    for p in glob.glob(os.path.join(root, "*_annotations.json")):
        stem = os.path.splitext(os.path.basename(p))[0].lower()
        if stem.endswith("_annotations"):
            stem = stem[: -len("_annotations")]
        out[stem] = p
    return out
