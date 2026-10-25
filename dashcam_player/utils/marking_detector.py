"""道路標線週期測速核心演算法

以行車紀錄器畫面中的車道標線（白色虛線，10 公尺一週期）測量車速。

原理:
- 車輛前進時標線在畫面上向下滾動。
- 在固定掃描帶（近場、標線最大最清晰處）上，每條標線通過時產生亮度脈衝。
- 相鄰脈衝的時間間隔 = 標線間距 / 車速。
- 10 公尺間距時: 車速 (km/h) = 36 / 週期 (秒)。

流程:
1. 夜間閘門 — 天空暗 + 燈具飽和主導時直接回空（路燈週期假脈衝、頭燈假階躍）。
2. 全寬平均訊號 — 掃描帶內平均亮度時間序列（平均有降噪效果）。
   （多帶合併函式保留供 Phase D，選擇準則待雙機真值驗證後再定，目前不啟用。）
3. 去趨勢 — reflect 補邊移動平均（8 秒），移除路面整體亮度漂移。
4. 脈衝計數 — 偵測亮→暗轉換（標線前緣通過）並計算週期（至少 5 個脈衝）。
5. 速度估算 — 距離/週期。

本模組為純函式，不依賴 Qt，可直接於背景執行緒使用。

驗證結果（實測 2560x1440/1920x1080 dashcam）:
- 黃昏影片: 47.0 km/h vs GPS 43.3 km/h（約 8% 誤差）
- 清晨過曝影片: 需選擇較高掃描帶以取得對比
"""

from __future__ import annotations

# mypy: disable-error-code="var-annotated,operator,arg-type,assignment"
import logging
import os
import sys

# 直接以 `python dashcam_player/utils/marking_detector.py` 執行時，
# __package__ 為 None 且 VS Code 預設 cwd 不在專案根目錄，導致
# `ModuleNotFoundError: No module named 'dashcam_player'`。
# 此段將專案根目錄（`dashcam_player` 的上一層）自動加入 sys.path，
# 同時相容含中文/空白的路徑（Python 3.11 原生 UTF-8）與 `python -m` 執行。
if __package__ in (None, ""):
    _proj_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _proj_root not in sys.path:
        sys.path.insert(0, _proj_root)

import cv2
import numpy as np

from dashcam_player.utils.cv_utils import open_video_capture
from dashcam_player.utils.log import get_logger
from dashcam_player.utils.pts_loader import get_reliable_fps_count
from dashcam_player.utils.vision_speed import MARKING_CYCLE_LENGTH_M

log = get_logger()
log.setLevel(logging.INFO)

# 速度常數
V_MIN_KMH = 5.0
V_MAX_KMH = 200.0
MIN_PULSE_COUNT = 5  # 至少需要的脈衝數（norm 112025 施工區 3 個雜訊脈衝 cons=1.00
# 曾誤輸出 45km/h；5 個門檻擋掉此類偶發對齊，正常高速 20s 窗內應有數十個）
MIN_CONSISTENCY = 0.30  # 脈衝間隔一致性下限

# --- 夜間閘門（norm 實測收斂：日/傍晚/夜 7 支） ---
# 夜間頭燈＋路燈把掃描帶照得很亮（band mean 可達 100），平均亮度無法區分日夜；
# 但「飽和像素比（燈具/反光號誌）」+「天空亮度」分離度充足：
#   日間/傍晚：sky 122-205、sat 0.001-0.013
#   夜間：sky 49-55、sat 0.028-0.073
# 雙條件 AND（晴天正午白車多也不會同時暗天空，避免誤閘）。
NIGHT_SKY_MAX = 80.0  # 天空帶（y 0.05-0.20）均值低於此視為暗
NIGHT_SAT_MIN = 0.02  # 掃描帶 V>200 像素比高於此視為燈具主導

# --- 多帶（multi-strip）標線訊號 ---
# 全寬平均在寬路（4 線道）會把小虛線漣漪稀釋到 ±1 灰階（norm 正午實測），
# 高架陰影等大擺動一來門檻即淹沒漣漪。切 4 條各自偵測再跨帶投票確認，
# 單一帶的雜訊（旁車道大車經過）因無他帶呼應而被丟棄。
N_STRIPS = 4
STRIP_MIN_AGREE = 2  # 同一事件至少需有此數的帶同時看到
STRIP_MERGE_TOL_SEC = 0.10  # 跨帶同一事件的時間容忍


def assess_lighting(sky_mean: float, sat_frac: float) -> tuple[str, str]:
    """日夜判斷（純函式，可單測）。

    Args:
        sky_mean: 天空帶（y 0.05-0.20）灰階均值
        sat_frac: 掃描帶內 V>200 像素比例

    Returns:
        (level, reason)，level 為 "DAY" / "NIGHT"。
        夜間路燈週期性通過會產生高一致性假脈衝、頭燈會在引擎蓋造假階躍，
        NIGHT 時呼叫端應直接回空（安全方向：不輸出速度）。
    """
    if sky_mean < NIGHT_SKY_MAX and sat_frac > NIGHT_SAT_MIN:
        return "NIGHT", f"sky={sky_mean:.0f}<{NIGHT_SKY_MAX:.0f} and sat={sat_frac:.3f}>{NIGHT_SAT_MIN}"
    return "DAY", f"sky={sky_mean:.0f} sat={sat_frac:.3f}"


def merge_strip_onsets(
    strip_ons_list: list[np.ndarray],
    fps: float,
    tol_sec: float = STRIP_MERGE_TOL_SEC,
    min_agree: int = STRIP_MIN_AGREE,
) -> np.ndarray:
    """合併多帶脈衝：跨帶投票確認（純函式，可單測）。

    同一事件（各帶 onsets 落在 tol_sec 內）需至少 min_agree 個帶看到才保留，
    取簇中位數為事件幀。單一帶的雜訊（旁車經過、局部陰影）因無他帶呼應被丟棄。

    Returns:
        合併後的 onsets（float 幀號，已排序），不足 3 個回空陣列。
    """
    events: list[tuple[float, int]] = []  # (frame, strip_id)
    for sid, ons in enumerate(strip_ons_list):
        for f in np.asarray(ons, dtype=float).tolist():
            events.append((float(f), sid))
    if not events:
        return np.array([], float)
    events.sort()
    tol_f = max(1.0, tol_sec * fps if fps > 0 else 3.0)
    merged: list[float] = []
    i = 0
    while i < len(events):
        j = i
        while j + 1 < len(events) and events[j + 1][0] - events[i][0] <= tol_f:
            j += 1
        strips = {events[k][1] for k in range(i, j + 1)}
        if len(strips) >= min_agree:
            merged.append(float(np.median([events[k][0] for k in range(i, j + 1)])))
        i = j + 1
    # 相鄰合併事件若過近（同一標線前後緣被不同帶拆開）去重
    dedup: list[float] = []
    for f in merged:
        if dedup and f - dedup[-1] < tol_f:
            dedup[-1] = (dedup[-1] + f) / 2.0
        else:
            dedup.append(f)
    return np.array(dedup, float)


def detrend_moving_average(signal: np.ndarray, win: int) -> np.ndarray:
    """移動平均去趨勢（reflect 對稱補邊）。

    ⚠️ 不可用 ``np.convolve(signal, kernel, mode='same')`` 直接做：
    same 在邊界隱性補零，趨勢線頭尾被拉向 0，去趨勢後出現遠超訊號本體的
    假斜坡（實測：原始 sd 2.0 → detrended sd 12.4，門檻被蓋過、脈衝全滅）。
    reflect 補邊後邊界污染消失，不需再切邊（切邊會丟 27-40% 短窗數據）。

    Args:
        signal: 一維訊號
        win: 移動平均窗長（會 clamp 至訊號長度）

    Returns:
        去趨勢後訊號（與輸入等長）。
    """
    n = len(signal)
    x = np.asarray(signal, dtype=np.float64)
    if n < 3:
        return x - float(np.mean(x)) if n else x
    win = max(1, min(win, n))
    if win <= 1:
        return x - float(np.mean(x))
    kernel = np.ones(win) / float(win)
    pad = win // 2
    padded = np.pad(x, pad, mode="reflect")
    trend_full = np.convolve(padded, kernel, mode="valid")
    # valid 長度 = n + 2*pad - win + 1（偶窗時為 n+1，取中央 n 段，半樣本偏移可忽略）
    start = (len(trend_full) - n) // 2
    trend = trend_full[start:start + n]
    return x - trend


def detect_marking_pulses(signal: np.ndarray, fps: float,
                          min_period: float = 0.15, max_period: float = 2.5,
                          trend_seconds: float = 8.0) -> tuple[np.ndarray, float, float]:
    """從掃描帶亮度時間序列偵測標線脈衝。

    去趨勢（reflect 補邊移動平均）後以 40% 標準差為門檻偵測亮→暗轉換
    （標線前緣通過掃描帶）。

    Returns:
        (onsets, median_interval_sec, consistency)
        onsets 為空陣列代表無有效脈衝。
    """
    n = len(signal)
    if n < 20:
        return np.array([], float), 0.0, 0.0
    # 去趨勢（移除路面整體亮度漂移）— 窗長不可超過訊號長度
    win = max(1, min(int(fps * trend_seconds), n))
    detrended = detrend_moving_average(signal, win)
    sd = float(detrended.std())
    if sd < 1e-6:
        return np.array([], float), 0.0, 0.0
    # 脈衝偵測（標線為亮帶，前緣為由暗轉亮）
    thr = 0.4 * sd
    on = (detrended > thr).astype(int)
    ons = [i for i in range(1, n) if on[i - 1] == 0 and on[i] == 1]
    if len(ons) < MIN_PULSE_COUNT:
        return np.array([], float), 0.0, 0.0
    ints = np.diff(ons) / fps
    med = float(np.median(ints))
    if not (min_period < med < max_period):
        return np.array([], float), 0.0, 0.0
    # 一致性 = 間隔落在中位數 ±35% 的比例
    cons = float(np.mean(np.abs(ints - med) < med * 0.35))
    return np.array(ons, float), med, cons


def analyze_video(video_path: str, distance_per_cycle: float = MARKING_CYCLE_LENGTH_M,
                  scan_band: tuple[float, float] = (0.50, 0.64),
                  max_frames: int = 0) -> dict:
    """分析整段影片並回傳標線週期測速結果。

    Args:
        video_path: 影片路徑
        distance_per_cycle: 標線間距（公尺），預設 10
        scan_band: 掃描帶垂直範圍 (y0, y1) 正規化 0-1。
            預設 (0.50,0.64) 為「車體上緣之上的近場地面」（Phase A/B 實測
            hood_top_y ≈ 0.65，舊值 0.76 含 0.65-0.76 車體反光帶），
            是標線最大最清晰處。任務 1 要求固定此帶（先檢查此程式），
            全地面標示（含遠景）會含大量小標線與雜訊，不適合 10m 週期法。
        max_frames: 處理幀數上限（0=全部）

    Returns:
        dict: 含 onsets / median_period / consistency / speed_kmh 的結果
    """
    cap = open_video_capture(video_path)
    if not cap.isOpened():
        log.error(f"MarkingDetector: 無法開啟影片 {video_path}")
        return {"onsets": [], "median_period": None, "consistency": 0.0,
                "speed_kmh": None, "distance_per_cycle": distance_per_cycle}

    h = int(cap.get(4))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    N = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    # 檔頭損壞的 AVI（宣稱 600fps/26477 幀，實際 5fps/223 幀）會讓標線脈衝
    # 週期換算速度全錯 → 以 ffprobe 的實際資料取代檔頭值
    reliable = get_reliable_fps_count(video_path)
    if reliable is not None:
        fps, N = reliable
    y1, y2 = int(h * scan_band[0]), int(h * scan_band[1])
    nf = min(max_frames, N) if max_frames > 0 else N
    # 全寬平均亮度時間序列（平均有降噪效果；多帶合併待雙機真值驗證後再啟用）
    sig_full = np.zeros(nf, np.float32)
    # 夜間閘門線上統計（不存幀，省記憶體）：天空均值＋掃描帶飽和比
    sy1, sy2 = int(h * 0.05), int(h * 0.20)
    sky_sum, sky_px = 0.0, 0
    sat_px, band_px = 0, 0

    fi = 0
    while True:
        ret, fr = cap.read()
        if not ret or fi >= nf:
            break
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY).astype(np.float32)
        band = g[y1:y2, :]
        sig_full[fi] = float(band.mean())
        sky = g[sy1:sy2, :]
        sky_sum += float(sky.sum())
        sky_px += sky.size
        sat_px += int(np.count_nonzero(band > 200))
        band_px += band.size
        fi += 1
    cap.release()

    if fi < 20:
        log.warning(f"MarkingDetector: 讀取幀數不足 ({fi})")
        return {"onsets": [], "median_period": None, "consistency": 0.0,
                "speed_kmh": None, "distance_per_cycle": distance_per_cycle,
                "lighting": "UNKNOWN", "source": "full"}
    sig_full = sig_full[:fi]

    # 夜間閘門：暗天空＋燈具飽和主導時直接回空（路燈週期假脈衝、頭燈假階躍）
    lighting, light_reason = assess_lighting(
        sky_sum / max(sky_px, 1), sat_px / max(band_px, 1)
    )
    if lighting == "NIGHT":
        log.info(f"MarkingDetector: {video_path} 夜間閘門拒判（{light_reason}）")
        return {"onsets": [], "median_period": None, "consistency": 0.0,
                "speed_kmh": None, "distance_per_cycle": distance_per_cycle,
                "lighting": lighting}

    # 單一全寬訊號偵測（平均有降噪效果）。
    # ⚠️ 多帶合併（merge_strip_onsets）目前僅保留為函式庫供 Phase D 使用，
    # 不參與選擇：實測證明「一致性無法預測正確性」（091548 merged cons 0.33 >
    # full 0.10，但 merged 25.0 全錯、full 56.8 正確；180655 反向亦然），
    # 任何覆寫門檻（0.5 等）都會在別支片上翻車。等雙機同路段真值到後再定選擇準則。
    full_ons, med, cons = detect_marking_pulses(sig_full, fps)
    ons_all = full_ons
    source = "full"
    if len(ons_all) < MIN_PULSE_COUNT or med is None or med <= 0:
        log.info(f"MarkingDetector: {video_path} 未偵測到有效標線脈衝")
        return {"onsets": [], "median_period": None, "consistency": 0.0,
                "speed_kmh": None, "distance_per_cycle": distance_per_cycle,
                "lighting": lighting, "source": source}
    ints = np.diff(ons_all) / fps
    med = float(np.median(ints))
    if not (0.15 < med < 2.5):
        return {"onsets": [], "median_period": med, "consistency": 0.0,
                "speed_kmh": None, "distance_per_cycle": distance_per_cycle,
                "lighting": lighting, "source": source}
    cons = float(np.mean(np.abs(ints - med) < med * 0.35))

    speed = distance_per_cycle / med * 3.6
    if not (V_MIN_KMH <= speed <= V_MAX_KMH):
        return {"onsets": [], "median_period": med, "consistency": cons,
                "speed_kmh": None, "distance_per_cycle": distance_per_cycle,
                "lighting": lighting, "source": source}

    log.info(f"MarkingDetector: {video_path} -> {len(ons_all)} 個脈衝, "
             f"週期={med:.3f}s, 速度={speed:.1f} km/h, 一致性={cons:.2f}, {lighting}, src={source}")
    return {"onsets": ons_all, "median_period": med, "consistency": cons,
            "speed_kmh": speed, "distance_per_cycle": distance_per_cycle,
            "lighting": lighting, "source": source
}


def overlay_marking_debug(
    frame: np.ndarray,
    scan_band: tuple[float, float] = (0.50, 0.64),
    boxes: list[list[int]] | None = None,
    speed_kmh: float | None = None,
) -> np.ndarray:
    """繪製標線偵測除錯視覺化（支援中文，不亂碼）。

    - 半透明黃帶 = 近場掃描帶（靠近車體）
    - 綠框 = 偵測到的標線候選框
    - 右上角 = 速度（中文標示）

    中文採用 PIL 繪製，解決 Windows CP950 環境下 cv2.putText 僅 ASCII 的亂碼問題。
    """
    import cv2

    try:
        from dashcam_player.utils.cv_utils import put_text_cn
    except ImportError:
        put_text_cn = None  # type: ignore[assignment]

    out = frame.copy()
    h, w = out.shape[:2]
    y1, y2 = int(h * scan_band[0]), int(h * scan_band[1])
    # 半透明掃描帶
    overlay = out.copy()
    cv2.rectangle(overlay, (0, y1), (w, y2), (0, 255, 255), -1)
    out = cv2.addWeighted(overlay, 0.15, out, 0.85, 0)
    cv2.rectangle(out, (0, y1), (w, y2), (0, 255, 255), 2)
    label = "近場掃描帶（靠近車體）"
    if put_text_cn is not None:  # type: ignore[truthy-function]
        put_text_cn(out, label, (10, max(0, y1 - 22)), font_size=16, color=(0, 255, 255), stroke_color=(0, 0, 0))
    else:
        cv2.putText(out, "scan band", (10, max(10, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)

    if boxes:
        for x, y, bw, bh in boxes:
            cv2.rectangle(out, (x, y), (x + bw, y + bh), (0, 255, 0), 2)
        info = f"標線候選: {len(boxes)} 個"
        if put_text_cn is not None:  # type: ignore[truthy-function]
            put_text_cn(out, info, (10, y2 + 8), font_size=15, color=(0, 255, 0), stroke_color=(0, 0, 0))
        else:
            cv2.putText(out, f"boxes:{len(boxes)}", (10, y2 + 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

    if speed_kmh is not None:
        txt = f"{speed_kmh:.1f} km/h"
        if put_text_cn is not None:  # type: ignore[truthy-function]
            put_text_cn(out, f"車速: {txt}", (w - 160, 30), font_size=20, color=(0, 255, 0), stroke_color=(0, 0, 0))
        else:
            cv2.putText(out, txt, (w - 120, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

    return out


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="標線週期測速 - 單檔測試（支援中文路徑）")
    parser.add_argument("video", help="影片路徑（可含中文/空白）")
    parser.add_argument("--show", action="store_true", help="顯示除錯視覺化視窗")
    parser.add_argument("--band", type=float, nargs=2, default=(0.50, 0.64), help="掃描帶 y0 y1 (預設 0.50 0.64 近場)")
    args = parser.parse_args()

    res = analyze_video(args.video, scan_band=tuple(args.band))  # type: ignore[arg-type]
    print(f"速度: {res['speed_kmh']} km/h, 週期={res['median_period']}, 一致性={res['consistency']:.2f}")

    if args.show:
        cap = open_video_capture(args.video)
        ret, frame = cap.read()
        cap.release()
        if ret and frame is not None:
            dbg = overlay_marking_debug(frame, scan_band=tuple(args.band), boxes=None, speed_kmh=res["speed_kmh"])  # type: ignore[arg-type]
            cv2.imshow("marking debug - 近場掃描帶", dbg)
            cv2.waitKey(0)
            cv2.destroyAllWindows()
