"""人工標示工具 — 幀級標線穿越標定（任務 B，混合自動+人工流程）

用途：依《視覺測速_幀級標定工作單》B 產生 Ground Truth。

流程（三步式）：
  1. 框選範圍：程式開啟影片、顯示首幀，使用者以滑鼠拖曳框出參考帶
     （包住單條車道虛線標線的矩形區域）。範圍內所有標線穿越都要標示。
  2. 程式產生候選：程式在框選範圍內抽取亮度訊號、去趨勢、偵測黑↔白邊緣
     穿越，自動標出候選幀號（如 1, 11, 21, 31）。這些是「程式判讀」候選，
     於畫面上以不同顏色標示（auto=true）。
  3. 人工確認/補標：逐幀檢視，確認/修正/移除程式的候選；程式無法判讀的
     穿越以 b（黑→白）/ w（白→黑）手動補標。存檔即為 Ground Truth。

顯示：
  - 黃色半透明框 = 使用者框選的掃描範圍（含水平 x 與垂直 y）
  - 綠色半框 = Tab 所在側（b/w 單點補標的作用側，一眼可辨）
  - 紫色/青色標籤 = 程式自動產生的候選穿越（auto）
  - 綠色（黑→白）/ 橙色（白→黑）標籤 = 人工標示/已確認

用法:
    python -m dashcam_player.utils.manual_annotator "影片.avi"
    python -m dashcam_player.utils.manual_annotator "影片.avi" --output custom.json
    直接執行亦可（VS Code cwd 非專案根 + 中文路徑已相容）

按鍵（檢視/標示階段）:
    n / → / d      : 下一幀
    p / ← / a      : 上一幀
    ] / . / Shift+n: 跳到下一個自動候選穿越
    [ / , / Shift+p: 跳到上一個自動候選穿越
    Tab            : 切換 b/w 單點補標的 左側/右側（所在半框描綠；穿越點點擊自動判側，不需 Tab）
    滑鼠左鍵點擊   : 在標線前端掃過橙色參考列(框中央)的瞬間，點一下該點
                     （點在中線左側=左線，右側=右線，自動判定）
                     設點後翻幀（n/p/[ ]/g）即自動儲存，不需 Enter
    Enter          : 立即儲存該穿越標記（記錄目前幀 = 穿越幀）
    Esc            : 取消未完成的點擊設點
    b              : 人工標記 黑→白 (B→W) 於當前幀（單點模式）
    w              : 人工標記 白→黑 (W→B) 於當前幀（單點模式）
    r / Delete     : 移除當前幀的標記（含穿越標記）
    s              : 存檔
    g              : 跳至指定幀號
    q / ESC        : 離開（未存檔時提示）

人工穿越標記說明（Task C ground-truth, Option A）:
    對同一條白虛線，凡「標線前端（領先緣）」掃過橙色參考列（= 掃描框中央，與 auto 判穿越的同一
    參考平面）的瞬間，滑鼠點該點 → Enter 儲存，記錄的「幀」即為該條線的穿越幀。
    側別由點擊 x 相對框中央中線自動判定（左=左線，右=右線）；b/w 單點補標才用 Tab 切側。
    與 auto 用同一參考平面 → 穿越幀可直接公平比對（無系統性偏移）。
    程式框（掃描範圍）在人工階段固定顯示、不可移動。

按鍵（框選階段）:
    滑鼠左鍵拖曳 = 選取矩形範圍；放開後可再次拖曳重選
    Enter        = 確認框選範圍，進入程式自動偵測
    r / c        : 清除並重新框選
    q / ESC      : 取消離開

輸出 JSON（相容既有 *_annotations.json 骨架）:
{
  "video_path": "...PIC...AVI",
  "resolution": [2560, 1440],
  "fps": 30.0,
  "scan_box": {"x0": 800, "y0": 792, "x1": 1600, "y1": 1094},
  "total_annotations": 8,
  "annotations": [
    {"frame": 11, "type": "bw", "time": 0.37, "label": "黑→白", "auto": true},
    {"frame": 35, "type": "wb", "time": 1.17, "label": "白→黑", "auto": false}
  ],
  "total_removed": 2,
  "removed_annotations": [
    {"frame": 12, "type": "wb", "auto": true, "removed_via": "r",
     "removed_box": [800, 792, 1600, 1094]}
  ]
}

刪除軌跡說明：
  R 刪掉的標記不會消失無蹤——原始紀錄（含 auto/type/side/frame）加上
  removed_via / removed_box 快照，存入 removed_annotations，供錯誤分析
  （`--audit` 彙整）。讀檔會讀回但不會復活成候選；重選框/重跑自動偵測
  也不清空，累積誤報歷史（以 removed_box 區分新舊框紀錄）。
"""

from __future__ import annotations

# mypy: disable-error-code="var-annotated,operator,arg-type,assignment,union-attr,index"
import argparse
import json
import os
import sys
import tempfile

if __package__ in (None, ""):
    _proj_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _proj_root not in sys.path:
        sys.path.insert(0, _proj_root)
else:
    _proj_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from dashcam_player.utils.cv_utils import (  # noqa: E402
    _get_cn_font,
    open_video_capture,
    put_text_cn,
)
from dashcam_player.utils.log import get_logger  # noqa: E402
from dashcam_player.utils.marking_tracker import detect_dash_bands  # noqa: E402
from dashcam_player.utils.pts_loader import get_reliable_fps_count  # noqa: E402

log = get_logger()

# 字體基準：依顯示畫面高度動態計算（基於 720p 基準值，全螢幕/大螢幕自動放大）
_BASE_FONT_H = 720  # 基準畫面高度

# 點擊座標映射除錯檔（人工標示階段每次左鍵點擊即寫入）
_CLICK_DEBUG_PATH = os.path.join(tempfile.gettempdir(), "dashcam_annotate_click_debug.log")

# 程式預設框（2560x1440 同相機）；僅在無既有 scan_box 且未指定 --box 時使用，
# 右側標線預設框（車道右側虛線，可見度高於左側）
DEFAULT_BOX_2560_1440: tuple[int, int, int, int] = (1250, 830, 1560, 962)
# 後鏡頭 1920x1080 的預設框（實測標線位於 x900-1400, y400-800）
DEFAULT_BOX_1920_1080: tuple[int, int, int, int] = (900, 400, 1400, 800)


def _default_box_for_resolution(w: int, h: int) -> tuple[int, int, int, int]:
    """依解析度回傳程式預設框（headless 自動標示用，同相機/解析度單一固定框）。"""
    if (w, h) == (1920, 1080):
        return DEFAULT_BOX_1920_1080
    return DEFAULT_BOX_2560_1440


def _get_video_meta(video_path: str) -> tuple[int, int, float, int, list[float] | None]:
    """回傳 (w, h, fps, total, pts_list_or_None)"""
    cap = open_video_capture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"無法開啟影片: {video_path}")
    try:
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        # 以 ffprobe 真值取代檔頭（600fps/26477 幀 → 5fps/223 幀）
        reliable = get_reliable_fps_count(video_path)
        if reliable is not None:
            fps, total = reliable
        pts = None
        try:
            from dashcam_player.utils.pts_loader import load_pts_and_keyframes

            res = load_pts_and_keyframes(video_path, timeout=10.0)
            if res.pts_list:
                pts = res.pts_list
        except Exception:
            pass
        return w, h, fps, total, pts
    finally:
        cap.release()


def _load_annotations(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        ann = data.get("annotations", [])
        return ann if isinstance(ann, list) else []
    except Exception as e:
        log.warning(f"載入舊標示失敗 {path}: {e}")
        return []


def _load_removed(path: str) -> list[dict]:
    """讀回人工刪除軌跡（供錯誤分析，續標/重跑皆保留累積）。"""
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        rem = data.get("removed_annotations", [])
        return rem if isinstance(rem, list) else []
    except Exception as e:
        log.warning(f"載入刪除軌跡失敗 {path}: {e}")
        return []


def _load_existing_box(path: str) -> tuple[int, int, int, int] | None:
    """從既有標定檔讀回 scan_box（供續標/批次自動標示沿用程式框）。"""
    if not os.path.exists(path):
        return None
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
        log.warning(f"讀回 scan_box 失敗 {path}: {e}")
    return None


def _split_manual_auto(annotations: list[dict]) -> tuple[list[dict], list[dict]]:
    """拆分人工標記與程式候選（純函式，可單測）。

    人工 = auto 欄位為 False（cross 穿越標記＋b/w 單點補標）。
    新框／重跑自動偵測時只丟棄程式候選，人工標記一律保留。
    """
    manual = [a for a in annotations if not bool(a.get("auto", False))]
    auto = [a for a in annotations if bool(a.get("auto", False))]
    return manual, auto


def _save_annotations(
    out_path: str,
    video_path: str,
    w: int,
    h: int,
    fps: float,
    box: tuple[int, int, int, int] | None,
    annotations: list[dict],
    removed: list[dict] | None = None,
) -> None:
    annotations = sorted(annotations, key=lambda x: int(x.get("frame", 0)))
    data: dict = {
        "video_path": os.path.abspath(video_path),
        "resolution": [w, h],
        "fps": float(fps),
        "total_annotations": len(annotations),
        "annotations": annotations,
    }
    if box is not None:
        x0, y0, x1, y1 = box
        data["scan_box"] = {"x0": x0, "y0": y0, "x1": x1, "y1": y1}
    # 人工刪除軌跡：R 刪掉的自動標記保留於此，供錯誤分析（讀檔會讀回，不會復活）。
    # 重新框選/重跑自動偵測時不清空，累積誤報歷史；box 快照供分析時過濾舊框紀錄。
    if removed:
        data["removed_annotations"] = removed
        data["total_removed"] = len(removed)
    # 中文路徑：utf-8 寫入，確保不經 cp950 損壞
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info(f"已存檔 {len(annotations)} 筆（+{len(removed) if removed else 0} 筆已移除） -> {out_path}")


def _get_time_for_frame(frame_idx: int, fps: float, pts: list[float] | None) -> float:
    if pts is not None and 0 <= frame_idx < len(pts):
        return float(pts[frame_idx])
    return float(frame_idx / fps) if fps > 0 else 0.0


def _font_size(base: int, disp_h: int) -> int:
    """依顯示畫面高度動態計算字體大小（720p 基準）。"""
    return max(12, int(base * disp_h / _BASE_FONT_H))


def _resize_for_display(frame: np.ndarray, max_w: int = 1920, max_h: int = 1080) -> tuple[np.ndarray, float]:
    """縮至安全顯示尺寸，保持長寬比。"""
    h, w = frame.shape[:2]
    scale = min(max_w / w, max_h / h, 1.0)
    if scale < 1.0:
        nw, nh = int(w * scale), int(h * scale)
        resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
        return resized, scale
    return frame, 1.0


# ---------------------------------------------------------------------------
# 階段 1：框選範圍（滑鼠選取矩形）
# ---------------------------------------------------------------------------
class _RectSelector:
    """以滑鼠在畫面上拖曳選取矩形範圍。"""

    def __init__(self, img: np.ndarray) -> None:
        self.img = img
        self.h, self.w = img.shape[:2]
        self.start: tuple[int, int] | None = None
        self.cur: tuple[int, int] | None = None
        self.rect: tuple[int, int, int, int] | None = None  # x0,y0,x1,y1

    def on_mouse(self, event: int, x: int, y: int, flags: int, param: object) -> None:
        if event == cv2.EVENT_LBUTTONDOWN:
            self.start = (x, y)
            self.cur = (x, y)
        elif event == cv2.EVENT_MOUSEMOVE and self.start is not None:
            self.cur = (x, y)
        elif event == cv2.EVENT_LBUTTONUP and self.start is not None:
            self.cur = (x, y)
            self._commit()

    def _commit(self) -> None:
        if self.start is None or self.cur is None:
            return
        x0 = min(self.start[0], self.cur[0])
        y0 = min(self.start[1], self.cur[1])
        x1 = max(self.start[0], self.cur[0])
        y1 = max(self.start[1], self.cur[1])
        if x1 - x0 < 8 or y1 - y0 < 4:
            self.rect = None
            return
        # 超出圖片邊界 clamp
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self.w, x1), min(self.h, y1)
        self.rect = (x0, y0, x1, y1)


def _select_region(frame_full: np.ndarray, win: str, default: tuple[int, int, int, int] | None) -> tuple[int, int, int, int] | None:
    """顯示首幀，讓使用者以滑鼠框選掃描範圍，回傳原解析度座標 (x0,y0,x1,y1)。

    按 Enter 確認、r/c 重選、q/Esc 取消。無預設時以全畫面為起點。
    """
    disp, scale = _resize_for_display(frame_full)
    sel = _RectSelector(disp)
    if default is not None:
        x0, y0, x1, y1 = default
        sel.rect = (int(x0 * scale), int(y0 * scale), int(x1 * scale), int(y1 * scale))
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    fh = disp.shape[0]
    cv2.resizeWindow(win, min(1920, disp.shape[1]), min(1080, fh))
    cv2.setMouseCallback(win, sel.on_mouse)

    print("\n[階段 1] 請以滑鼠拖曳框出寬掃描範圍（涵蓋左右車道，約 1.5 車道寬）")
    print("          程式將以框中央垂直線分割左右，獨立偵測左右車道標線")
    print("          Enter=確認 / r=重選 / q=離開")

    while True:
        ov = sel.img.copy()
        if sel.rect is not None:
            x0, y0, x1, y1 = sel.rect
            overlay = ov.copy()
            cv2.rectangle(overlay, (x0, y0), (x1, y1), (0, 255, 255), -1)
            cv2.addWeighted(overlay, 0.18, ov, 0.82, 0)
            cv2.rectangle(ov, (x0, y0), (x1, y1), (0, 255, 255), 2)
            # 顯示中間分割線（框中央垂直線）
            div_x = (x0 + x1) // 2
            cv2.line(ov, (div_x, y0), (div_x, y1), (255, 0, 255), 2)
            put_text_cn(ov, "掃描範圍（框中央紫線分割左右車道）", (max(0, x0), max(8, y0 - 22)), font_size=_font_size(22, fh), color=(0, 255, 255), stroke_color=(0, 0, 0))
        elif sel.cur is not None and sel.start is not None:
            # 拖拉中
            x0 = min(sel.start[0], sel.cur[0])
            y0 = min(sel.start[1], sel.cur[1])
            x1 = max(sel.start[0], sel.cur[0])
            y1 = max(sel.start[1], sel.cur[1])
            cv2.rectangle(ov, (x0, y0), (x1, y1), (0, 255, 255), 2)
        put_text_cn(ov, "Enter=確認  r=重選  q=離開", (10, 10), font_size=_font_size(22, fh), color=(255, 255, 255), stroke_color=(0, 0, 0))
        put_text_cn(ov, "※ 框選涵蓋左右車道，中央紫線自動分割", (10, 32), font_size=_font_size(20, fh), color=(0, 255, 255), stroke_color=(0, 0, 0))
        cv2.imshow(win, ov)
        key = cv2.waitKeyEx(20)
        if key in (27, ord("q"), ord("Q")):
            cv2.destroyWindow(win)
            return None
        if key in (ord("r"), ord("R"), ord("c"), ord("C")):
            sel.rect = None
            sel.start = None
            sel.cur = None
            continue
        if key in (13, 10, 32):  # Enter / 空白
            if sel.rect is not None:
                cv2.destroyWindow(win)
                rx0, ry0, rx1, ry1 = sel.rect
                # 轉回原解析度
                return (
                    int(round(rx0 / scale)),
                    int(round(ry0 / scale)),
                    int(round(rx1 / scale)),
                    int(round(ry1 / scale)),
                )
    cv2.destroyWindow(win)
    return None


# ---------------------------------------------------------------------------
# 階段 2：程式自動產生候選穿越（雙車道獨立追蹤）
# ---------------------------------------------------------------------------
def _side_crossings(
    occ: list[float],
    start: int,
    eps_up: float = 0.02,
    eps_down: float = 0.01,
    min_persist: int = 3,
) -> list[tuple[float, str]]:
    """單側佔比序列轉穿越事件（純函式，可單測）。

    出現/消失語意：bw = 佔比從 ~0 升起（標線進入），wb = 回落到 ~0（標線離開），
    以**絕對**佔比門檻判定，不用各側歷史中位數。

    背景（092148 左彎道實測）：舊碼以 `max(0.03, median*0.7)` 為上升門檻，
    左側細標線進入時佔比僅 0.076，低於左側歷史中位數撐起的 0.10 門檻而漏檢；
    同一幀右側粗標線（0.064 > 右側低門檻）觸發，看起來像「右進入帶動左一起標」。
    出現事件只關心「從無到有」，細線 0.07 與粗線 0.20 都該在進入當下觸發，
    故改絕對小門檻；抖動由下游交替濾波＋閃爍對濾波處理。

    v2 修訂（2026-08-29，091548-R 鏡頭實測發現）：僅用瞬間門檻穿越，在雜訊底噪
    與門檻同量級的畫面（本例為寬框＋魚眼鏡頭）會產生大量假交界——實測 60 幀內
    5 個事件有 4 個是連續 1~2 幀內反覆橫跳的雜訊（例：`0.016→0.020→0.000→
    0.020→0.008→0.064`，每幀都在跨門檻，不可能是真實標線進出，真實標線進出
    需要連續多幀才會完整經過偵測框）。改為「跨門檻後需連續 `min_persist` 幀
    維持同一狀態才確認」，可過濾此類單/雙幀雜訊震盪，同一組實測數據下事件數從
    5 個降到 1 個（僅保留真正持續多幀的訊號）。

    `min_persist` 越大越保守（可能延遲真實交界的判定幀號 `min_persist-1` 幀），
    需與 `eps_up`/`eps_down` 搭配依實際幀率/雜訊水準調整，預設 3 為初步驗證值。

    Args:
        occ: 單側每幀亮線佔比（0-1）
        start: 起始幀號（輸出幀號 = start + 內插偏移）
        eps_up: 出現門檻（半框寬 516px 下 ≈10 欄亮線，低於此視為雜訊）
        eps_down: 消失門檻（比出現低，形成小滯回，抗抖動）
        min_persist: 跨門檻後需連續維持同一狀態的幀數，才確認為真實交界

    Returns:
        [(frame_float, "bw"/"wb"), ...] 依幀排序（含線性內插次幀精度）。
    """
    o = np.array(occ, dtype=np.float64)
    nz = o[o > 0.001]
    if len(nz) < 5:
        return []
    out: list[tuple[float, str]] = []
    present = False
    pending_state: bool | None = None
    pending_start: int | None = None
    pending_count = 0
    for i in range(len(o)):
        v = float(o[i])
        target_state = present
        if not present and v > eps_up:
            target_state = True
        elif present and v < eps_down:
            target_state = False

        if target_state != present:
            if pending_state == target_state:
                pending_count += 1
            else:
                pending_state = target_state
                pending_count = 1
                pending_start = i
            if pending_count >= min_persist:
                # 內插次幀精度：以狀態實際開始改變的那一幀（pending_start）為準
                out.append((float(pending_start) + start, "bw" if target_state else "wb"))
                present = target_state
                pending_state = None
                pending_count = 0
        else:
            pending_state = None
            pending_count = 0
    return out


def _auto_detect_crossings(
    video_path: str,
    box: tuple[int, int, int, int],
    start: int,
    total: int,
    fps: float,
    divider_ratio: float = 0.5,
) -> list[dict]:
    """在寬框內以左右兩側獨立追蹤「暗路面上的亮虛線」，輸出黑↔白穿越候選。

    設計：
    1. 使用者框選寬框（涵蓋左右車道，約 1.5 車道寬）。
    2. 以 divider_ratio（預設 0.5，框內正規化 x）垂直分割為左/右半區。
    3. 左/右半區各自執行 detect_dash_bands + assign_to_tracks 追蹤。
    4. 各側佔比序列經 _side_crossings 轉穿越事件（出現/消失絕對門檻），產出 side 欄位。

    Args:
        divider_ratio: 分割線位置（框內正規化 0..1），0.5 為正中間。
    """
    x0, y0, x1, y1 = box
    cap = open_video_capture(video_path)
    if not cap.isOpened():
        return []

    band_w = x1 - x0
    div_x = int(x0 + band_w * divider_ratio)  # 分割線絕對座標

    # 左/右半區各自的佔比序列
    occ_left: list[float] = []
    occ_right: list[float] = []

    fi = start
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    while True:
        ret, fr = cap.read()
        if not ret or fr is None or fi >= total:
            break
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)

        # 左半區
        dets_l, _ = detect_dash_bands(g, x0, y0, div_x, y1)
        frac_l = 0.0
        for a, b in dets_l:
            frac_l += (b - a) / float(div_x - x0) if div_x > x0 else 0.0
        occ_left.append(float(min(frac_l, 1.0)))

        # 右半區
        dets_r, _ = detect_dash_bands(g, div_x, y0, x1, y1)
        frac_r = 0.0
        for a, b in dets_r:
            frac_r += (b - a) / float(x1 - div_x) if x1 > div_x else 0.0
        occ_right.append(float(min(frac_r, 1.0)))

        fi += 1
    cap.release()

    if len(occ_left) < 20 and len(occ_right) < 20:
        return []

    # 為各側獨立產生穿越（出現/消失語意，純函式 _side_crossings，可單測）
    anns: list[dict] = []
    for side, occ in (("left", occ_left), ("right", occ_right)):
        if len(occ) < 20:
            continue
        for fr0, typ in _side_crossings(occ, start):
            anns.append({
                "frame": fr0, "type": typ, "auto": True, "side": side,
                "time": _get_time_for_frame(fr0, fps, None),
            })

    # 去重複（同幀同型同側），按幀排序
    seen: set[tuple[int, str, str]] = set()
    out: list[dict] = []
    for a in sorted(anns, key=lambda x: float(x["frame"])):
        k = (round(float(a["frame"])), a["type"], a["side"])
        if k in seen:
            continue
        seen.add(k)
        out.append(a)

    # 強制交替模式：去除同型過近偵測
    out = _enforce_alternating_pattern(out, min_frame_gap=4, expected_cycle=5)
    # 閃爍對：異型相鄰幀（佔比訊號在門檻上下抖動，非真實標線進出）
    out = _remove_flicker_pairs(out)
    return out


def _remove_flicker_pairs(
    anns: list[dict],
    min_dwell_frames: int = 3,
) -> list[dict]:
    """移除異型相鄰閃爍對（純函式，可單測）。

    真實標線通過時，亮態（bw→wb 間隔）約佔週期的 40%（台灣規範白線 4m/週期 10m）；
    若 wb 緊跟 bw 在 < min_dwell_frames 內出現（同側），代表佔比訊號在門檻
    上下抖動的閃爍，不是標線進出——捨棄後者（較晚的穿越點）。
    內線右虛線＋左實線場景中，實線邊緣抖動常產生此類 bw/wb 相鄰對。
    高速下偵測常只抓住標線一幀（blink：bw1166.4→wb1167.8 差 1.4），此類 blink-off 的 wb
    同樣捨棄、保留 bw（出現為真）。

    Args:
        anns: 候選穿越（需含 frame/type/side）
        min_dwell_frames: 異型最小間隔（幀，**浮點原值比較**，四捨五入會把
            1.4-1.8 的 blink-off 灌水成 2 而漏擋）。=3 的依據：180655 刪除軌跡
            audit 顯示被刪 wb 的 dwell 中位僅 3.3（該片 45km/h，真實 dwell 應 ~10 幀，
            duty 20% 遠低於規範 40%）；3 幀門檻覆蓋至 ~140km/h（dwell 3.3），
            高於此的速度此專案尚無樣本。

    Returns:
        過濾後穿越（排序不變）。
    """
    if not anns:
        return []
    by_side: dict[str, list[dict]] = {}
    for a in anns:
        by_side.setdefault(a.get("side", "left"), []).append(a)
    out: list[dict] = []
    for _side, lst in by_side.items():
        lst = sorted(lst, key=lambda x: float(x["frame"]))
        kept: list[dict] = []
        for a in lst:
            if (kept and a.get("type") != kept[-1].get("type")
                    and float(a["frame"]) - float(kept[-1]["frame"]) < min_dwell_frames):
                continue  # 閃爍對，捨棄較晚者
            kept.append(a)
        out.extend(kept)
    return sorted(out, key=lambda x: float(x["frame"]))


# ---------------------------------------------------------------------------
# 輔助：強制交替模式後處理（去除不可能的同型連續偵測）
# ---------------------------------------------------------------------------
def _enforce_alternating_pattern(
    anns: list[dict],
    min_frame_gap: int = 4,
    expected_cycle: int = 5,
) -> list[dict]:
    """強制 bw/wb 交替模式，去除同型連續且距離過近的偵測。

    邏輯：
    1. 依 side 分組，各側獨立處理
    2. 依幀排序，追蹤預期下一個型態（bw→wb→bw...）
    3. 同型且幀距 < min_frame_gap：視為雜訊，捨棄較晚者
    4. 同型且幀距 >= expected_cycle * 1.5：可能漏掉中間轉換，保留但重設交替期望
    """
    if not anns:
        return []

    by_side: dict[str, list[dict]] = {"left": [], "right": []}
    for a in anns:
        by_side.setdefault(a.get("side", "left"), []).append(a)

    out: list[dict] = []
    for _side, lst in by_side.items():
        lst = sorted(lst, key=lambda x: float(x["frame"]))
        if len(lst) <= 1:
            out.extend(lst)
            continue

        filtered: list[dict] = []
        expected_type = lst[0]["type"]
        for a in lst:
            if a["type"] == expected_type:
                filtered.append(a)
                expected_type = "wb" if expected_type == "bw" else "bw"
            else:
                if filtered and (float(a["frame"]) - float(filtered[-1]["frame"])) < min_frame_gap:
                    continue
                filtered.append(a)
                expected_type = "wb" if a["type"] == "bw" else "bw"

        # 二次過濾：移除仍然存在的同型過近（防禦性）
        final: list[dict] = []
        for a in filtered:
            if final and a["type"] == final[-1]["type"] and \
               (float(a["frame"]) - float(final[-1]["frame"])) < min_frame_gap:
                continue
            final.append(a)

        # 三次過濾：移除子幀級密集簇（同型連續且間距 < 1.5 幀）
        tight: list[dict] = []
        for a in final:
            if tight and a["type"] == tight[-1]["type"] and \
               (float(a["frame"]) - float(tight[-1]["frame"])) < 1.5:
                continue
            tight.append(a)

        out.extend(tight)

    return sorted(out, key=lambda x: float(x["frame"]))


# ---------------------------------------------------------------------------
# 階段 3：人工確認/補標（互動檢視）
# ---------------------------------------------------------------------------
def _draw_overlay(
    frame: np.ndarray,
    box: tuple[int, int, int, int] | None,
    annotations: list[dict],
    cur_idx: int,
    fps: float,
    pts: list[float] | None,
    w: int,
    h: int,
    pending: dict | None = None,
    active_side: str | None = None,
) -> np.ndarray:
    out = frame.copy()
    fh = out.shape[0]  # 畫面實際高度（供動態字體計算）

    # ---- 第一階段：所有 cv2 繪圖（線條、矩形、半透明疊加） ----
    if box is not None:
        x0, y0, x1, y1 = box
        overlay = out.copy()
        cv2.rectangle(overlay, (x0, y0), (x1, y1), (0, 255, 255), -1)
        cv2.addWeighted(overlay, 0.12, out, 0.88, 0)
        cv2.rectangle(out, (x0, y0), (x1, y1), (0, 255, 255), 2)
        # 中央分割線（紫色虛線，區分左右車道）
        div_x = (x0 + x1) // 2
        for yy in range(y0, y1, 16):
            cv2.line(out, (div_x, yy), (div_x, min(yy + 8, y1)), (255, 0, 255), 2)
        # Tab 所在側半框描綠（b/w 單點補標的作用側，一眼可辨）
        if active_side in ("left", "right"):
            ax0, ax1 = (x0, div_x) if active_side == "left" else (div_x, x1)
            cv2.rectangle(out, (ax0, y0), (ax1, y1), (0, 255, 0), 3)
        # 參考列 = 掃描框中線（橙色，判斷穿越時刻）
        ref_y = (y0 + y1) // 2
        cv2.line(out, (0, ref_y), (w, ref_y), (0, 165, 255), 1)

    mid_x = (box[0] + box[2]) // 2 if box else w // 2
    left_x = (box[0] + mid_x) // 2 if box else w // 4      # 左半區中心
    right_x = (mid_x + box[2]) // 2 if box else 3 * w // 4  # 右半區中心
    y_top, y_bot = (box[1], box[3]) if box else (0, h)
    mid_y = (y_top + y_bot) // 2
    for ann in sorted(annotations, key=lambda x: int(x.get("frame", 0))):
        f = int(ann.get("frame", -1))
        t = ann.get("type", "")
        is_auto = bool(ann.get("auto", False))
        if is_auto:
            color = (255, 0, 255) if t == "bw" else (255, 128, 0) if t == "wb" else (200, 200, 200)
        else:
            color = (0, 255, 0) if t == "bw" else (255, 128, 0) if t == "wb" else (200, 200, 200)
        if f == cur_idx:
            # 依 side 決定顯示位置：左側標記畫左半區，右側標記畫右半區
            side = ann.get("side", "left")
            draw_x = left_x if side == "left" else right_x
            cv2.line(out, (draw_x, y_top), (draw_x, y_bot), color, 3)

    for a in annotations:
        if a.get("kind") == "cross" and a.get("frame", -1) == cur_idx:
            side = a.get("side", "left")
            s_color = (0, 200, 255) if side == "right" else (255, 0, 200)
            try:
                px, py = int(a["point"][0]), int(a["point"][1])
                cv2.circle(out, (px, py), 8, s_color, -1)
                cv2.circle(out, (px, py), 12, (255, 255, 255), 2)
            except Exception:
                pass

    if pending is not None and pending.get("frame") == cur_idx:
        side = pending.get("side", "left")
        s_color = (0, 200, 255) if side == "right" else (255, 0, 200)
        pt = pending.get("point")
        if pt is not None:
            cv2.circle(out, tuple(int(v) for v in pt), 8, s_color, -1)
            cv2.circle(out, tuple(int(v) for v in pt), 12, (255, 255, 255), 2)
        cursor = pending.get("cursor")
        if cursor is not None:
            cx, cy = int(cursor[0]), int(cursor[1])
            cv2.line(out, (cx - 12, cy), (cx + 12, cy), s_color, 1)
            cv2.line(out, (cx, cy - 12), (cx, cy + 12), s_color, 1)

    # 底部說明列背景
    bar_top = h - 74
    bar = out.copy()
    cv2.rectangle(bar, (0, bar_top), (w, h), (0, 0, 0), -1)
    cv2.addWeighted(bar, 0.55, out, 0.45, 0, dst=out)
    cv2.rectangle(out, (0, bar_top), (w, h), (255, 255, 255), 1)

    # 掃描範圍放大條
    if box is not None and box[3] - box[1] > 5 and box[2] - box[0] > 5:
        try:
            bx0, by0, bx1, by1 = box
            band = out[by0:by1, bx0:bx1]
            if band.size > 0:
                zoom_h = 46
                band_zoom = cv2.resize(band, (w, zoom_h), interpolation=cv2.INTER_NEAREST)
                zo = out[bar_top - zoom_h:bar_top].copy()
                cv2.addWeighted(band_zoom, 0.6, zo, 0.4, 0, dst=out[bar_top - zoom_h:bar_top])
        except Exception:
            pass

    # ---- 第二階段：一次性轉 PIL，繪製所有中文文字 ----
    try:
        from PIL import Image, ImageDraw
        rgb = cv2.cvtColor(out, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(rgb)
        draw = ImageDraw.Draw(pil_img)

        def _pil_text(txt: str, x: int, y: int, base_size: int, bgr_color: tuple[int, int, int]) -> int:
            """繪製文字並回傳實際佔用高度（含行距），供後續定位。"""
            pil_font = _get_cn_font(_font_size(base_size, fh))
            if pil_font is None:
                return 0
            fill = (bgr_color[2], bgr_color[1], bgr_color[0])
            # 取得文字實際邊界
            bbox = draw.textbbox((x, y), txt, font=pil_font, stroke_width=2)
            draw.text((x, y), txt, font=pil_font, fill=fill, stroke_width=2, stroke_fill=(0, 0, 0))
            return max(2, bbox[3] - bbox[1] + 4)  # 高度 + 行距

        if box is not None:
            mid_x = (box[0] + box[2]) // 2
            left_x = (box[0] + mid_x) // 2
            right_x = (mid_x + box[2]) // 2
            _pil_text("掃描範圍", max(0, box[0]), max(8, box[1] - 28), 20, (0, 255, 255))
            if active_side in ("left", "right"):
                _pil_text(f"Tab:{'右側' if active_side == 'right' else '左側'}",
                          max(0, box[0]), max(8, box[1] - 52), 18, (0, 255, 0))
            _pil_text("參考列", 4, max(8, ref_y - 6), 18, (0, 165, 255))

        for ann in sorted(annotations, key=lambda x: int(x.get("frame", 0))):
            f = int(ann.get("frame", -1))
            t = ann.get("type", "")
            is_auto = bool(ann.get("auto", False))
            if f == cur_idx:
                if is_auto:
                    label = "A黑→白" if t == "bw" else "A白→黑" if t == "wb" else t
                    color = (255, 0, 255) if t == "bw" else (255, 128, 0)
                else:
                    label = "黑→白" if t == "bw" else "白→黑" if t == "wb" else t
                    color = (0, 255, 0) if t == "bw" else (255, 128, 0)
                # 依 side 決定顯示位置
                if box is not None:
                    mid_x = (box[0] + box[2]) // 2
                    side = ann.get("side", "left")
                    draw_x = (box[0] + mid_x) // 2 if side == "left" else (mid_x + box[2]) // 2
                else:
                    draw_x = mid_x
                _pil_text(f"{label} #{f}", draw_x + 8, mid_y - 10, 21, color)

        for a in annotations:
            if a.get("kind") == "cross" and a.get("frame", -1) == cur_idx:
                side = a.get("side", "left")
                s_color = (0, 200, 255) if side == "right" else (255, 0, 200)
                try:
                    _pil_text(f"{'右' if side == 'right' else '左'}側 前端穿越",
                              int(a["point"][0]) + 10, int(a["point"][1]) - 14, 18, s_color)
                except Exception:
                    pass

        if pending is not None and pending.get("frame") == cur_idx:
            side = pending.get("side", "left")
            s_color = (0, 200, 255) if side == "right" else (255, 0, 200)
            pt = pending.get("point")
            if pt is not None:
                _pil_text(f"{'右' if side == 'right' else '左'}側穿越點",
                          int(pt[0]) + 10, int(pt[1]) - 14, 20, s_color)

        # 頂部資訊 - 動態計算位置避免重疊
        cur_time = _get_time_for_frame(cur_idx, fps, pts)
        n_auto = sum(1 for a in annotations if bool(a.get("auto", False)))
        n_man = len(annotations) - n_auto
        info = f"幀: {cur_idx} / 時間: {cur_time:.3f}s / 標示 {len(annotations)} 筆 (程式{n_auto}/人工{n_man})"
        info2 = "b=黑→白 w=白→黑 r=移除 s=存檔 q=離開  [ ]=跳候選  n/p=上下幀"
        y = 4
        y += _pil_text(info, 10, y, 22, (255, 255, 255))
        y += _pil_text(info2, 10, y, 20, (200, 200, 200))

        # 底部狀態列 - 在 bar 區域內由下往上排版，確保不超界
        cur_auto = [a for a in annotations if int(a.get("frame", -1)) == cur_idx and bool(a.get("auto", False))]
        cur_man = [a for a in annotations if int(a.get("frame", -1)) == cur_idx and not bool(a.get("auto", False))]
        cur_cross = [a for a in annotations if a.get("kind") == "cross" and int(a.get("frame", -1)) == cur_idx]
        if pending is not None and pending.get("frame") == cur_idx:
            side = pending.get("side", "left")
            if pending.get("point") is None:
                status = f"✎ 標示中【{'右側' if side == 'right' else '左側'}】: 在標線前端掃過橙色參考列的瞬間，點一下該點"
                scolor = (255, 0, 200)
            else:
                status = f"✎ 標示中【{'右側' if side == 'right' else '左側'}】翻幀自動儲存 / Enter 立存 / Esc 取消"
                scolor = (0, 200, 255)
        elif cur_cross:
            tag = " + ".join(f"{'右' if x.get('side') == 'right' else '左'}側@{x['frame']}" for x in cur_cross)
            status = f"★ 人工穿越已標: {tag}"
            scolor = (0, 255, 0)
        elif cur_man:
            tag = ",".join(f"{'左' if x.get('side') == 'left' else '右'}{'黑→白' if x.get('type') == 'bw' else '白→黑'}" for x in cur_man)
            status = f"★ 人工已標: {tag}"
            scolor = (0, 255, 0)
        elif cur_auto:
            status = "◆ 程式候選（可保留或按 r 移除）"
            scolor = (255, 0, 255)
        else:
            status = "未標示（點中線左/右側自動判側 → Enter 存 / b/w 單點用 Tab 選側）"
            scolor = (160, 160, 160)

        help1 = "點擊設點→翻幀自動存 Enter=立存 Esc=取消 r=移除 s=存檔 [ ]=跳候選"
        help2 = "n/p=上下幀  ,=跳10幀  g=跳幀號  q=離開  b/w=黑→白/白→黑"

        # 從 bar 底部往上排版三行文字
        y = h - 4
        y -= _pil_text(help2, 10, y, 18, (200, 200, 200))
        y -= _pil_text(help1, 10, y, 18, (200, 200, 200))
        y -= _pil_text(status, 10, y, 21, scolor)

        if box is not None and box[3] - box[1] > 5 and box[2] - box[0] > 5:
            _pil_text("掃描範圍放大", w - 150, bar_top - 46 + 18, 14, (0, 255, 255))

        # PIL → BGR
        out = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    except ImportError:
        pass  # PIL 不可用時維持無文字的純圖形疊加

    return out


def _draw_cross_event(out: np.ndarray, a: dict, cur_idx: int) -> None:
    """在畫面上繪製一筆人工穿越標記（標線前端掃過橙色參考列的瞬間，單點）。"""
    side = a.get("side", "left")
    s_color = (0, 200, 255) if side == "right" else (255, 0, 200)
    try:
        px, py = (int(a["point"][0]), int(a["point"][1]))
    except Exception:
        return
    cv2.circle(out, (px, py), 8, s_color, -1)
    cv2.circle(out, (px, py), 12, (255, 255, 255), 2)
    fh = out.shape[0]
    put_text_cn(out, f"{'右' if side == 'right' else '左'}側 前端穿越", (px + 10, py - 10),
                font_size=_font_size(18, fh), color=s_color, stroke_color=(0, 0, 0))


def _process_video(
    video_path: str,
    out_path: str,
    start: int,
    box: tuple[int, int, int, int] | None,
    auto_only: bool = False,
) -> None:
    """三步式處理：框選範圍 → 程式自動偵測 → 人工確認/補標。"""
    w, h, fps, total, pts = _get_video_meta(video_path)
    print(f"\n{'='*64}")
    print(f"影片: {video_path}")
    print(f"解析度: {w}x{h}, fps: {fps:.2f}, 總幀: {total}")

    cap = open_video_capture(video_path)
    if not cap.isOpened():
        print(f"無法開啟影片解碼器: {video_path}")
        return

    # 讀取起始幀供框選
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    retf, framef = cap.read()
    if not retf or framef is None:
        print("起始幀讀取失敗")
        cap.release()
        return

    # 階段 1：框選範圍 + 續標判斷
    existing_annotations: list[dict] = []
    existing_box: tuple[int, int, int, int] | None = None
    # 新框 → 重跑自動偵測（只丟棄舊程式候選，人工標記一律保留後合併）
    box_changed = False
    # 人工刪除軌跡：讀回累積（續標/重跑皆保留，供錯誤分析；重選框後舊紀錄帶 box 快照）
    removed: list[dict] = _load_removed(out_path) if os.path.exists(out_path) else []
    if os.path.exists(out_path):
        existing_annotations = _load_annotations(out_path)
        existing_box = _load_existing_box(out_path)
        if not auto_only and existing_annotations:
            n_auto_ex = sum(1 for a in existing_annotations if bool(a.get("auto", False)))
            n_man_ex = len(existing_annotations) - n_auto_ex
            print(f"發現既有標定檔：{len(existing_annotations)} 筆（程式{n_auto_ex}/人工{n_man_ex}）"
                  + (f"，另有 {len(removed)} 筆已移除（保留供分析）" if removed else ""))
            try:
                ans = input("是否續標？(y=續標/n=重新開始): ").strip().lower()
            except Exception:
                ans = "y"
            if ans in ("n", "no"):
                existing_annotations = []
                existing_box = None
                print("已清除舊標記，重新開始")

    selected = box
    if selected is not None and existing_box is not None and tuple(selected) != tuple(existing_box):
        # --box 指定了與既有不同的框：視同重設框架（人工保留、稍後重跑自動偵測）
        print("--box 與既有框不同，視同重設框架（人工標記保留、重跑自動偵測）")
        box_changed = True
    if selected is None:
        if auto_only:
            # --auto-only：用既有框或程式預設（headless batch 不需互動框選）
            if existing_box is not None:
                selected = existing_box
                print(f"沿用既有 scan_box: x={selected[0]}..{selected[2]}, y={selected[1]}..{selected[3]}")
            else:
                selected = _default_box_for_resolution(w, h)
                print(f"[auto-only] 使用程式預設框: x={selected[0]}..{selected[2]}, y={selected[1]}..{selected[3]}")
        elif existing_box is not None:
            # 續標模式：顯示既有框，讓使用者確認或重選
            print(f"既有掃描框: x={existing_box[0]}..{existing_box[2]}, y={existing_box[1]}..{existing_box[3]}")
            try:
                ans = input("沿用既有框？(y=沿用/n=重新框選): ").strip().lower()
            except Exception:
                ans = "y"
            if ans in ("n", "no"):
                win_sel = f"框選範圍 - {os.path.basename(video_path)}"
                selected = _select_region(framef, win_sel, None)
                if selected is None:
                    print("已取消框選，結束")
                    cap.release()
                    return
                # 重選框後需重新跑自動偵測；人工標記保留（只丟棄舊程式候選）
                box_changed = True
            else:
                selected = existing_box
        else:
            # 互動模式：一律由使用者手動拖曳框選，不沿用舊框或預設框
            win_sel = f"框選範圍 - {os.path.basename(video_path)}"
            selected = _select_region(framef, win_sel, None)
            if selected is None:
                print("已取消框選，結束")
                cap.release()
                return
    print(f"框選範圍: x={selected[0]}..{selected[2]}, y={selected[1]}..{selected[3]}")

    # 階段 2：程式自動產生候選
    #   - 沿用既有框＋有既有標記（互動續標）→ 直接沿用（含人工＋舊程式候選）
    #   - 新框／無既有標記／auto-only →（保留人工＋）重跑自動偵測後合併
    #     （auto-only 永遠重跑，否則演算法更新後重跑無效）
    if existing_annotations and not box_changed and not auto_only:
        annotations = existing_annotations
        n_auto = sum(1 for a in annotations if bool(a.get("auto", False)))
        print(f"沿用既有標記 {len(annotations)} 筆（程式{n_auto}/人工{len(annotations)-n_auto}）")
    else:
        manual_kept, _ = _split_manual_auto(existing_annotations)
        if manual_kept:
            print(f"人工標記 {len(manual_kept)} 筆保留，重跑自動偵測後合併…")
        else:
            print("程式自動偵測標線穿越中…")
        annotations = manual_kept + _auto_detect_crossings(video_path, selected, start, total, fps)
        n_auto = len(annotations) - len(manual_kept)
        print(f"程式自動產生候選 {n_auto} 筆（＋人工 {len(manual_kept)} 筆）: "
              + ", ".join(f"{a['frame']}{'BW' if a['type']=='bw' else 'WB'}" for a in annotations if bool(a.get('auto', False))) if n_auto else "（無候選）")

    if auto_only:
        _save_annotations(out_path, video_path, w, h, fps, selected, annotations, removed)
        cap.release()
        n_man = sum(1 for a in annotations if not bool(a.get("auto", False)))
        print(f"[auto-only] 已儲存 {len(annotations)} 筆（程式{n_auto}/人工{n_man}） -> {out_path}")
        return

    # 階段 3：人工確認/補標（從最後一個已標記幀開始，或從頭）
    if annotations:
        last_annotated = max(int(a.get("frame", 0)) for a in annotations)
        cur_idx = max(0, min(last_annotated, total - 1))
        print(f"從最後標記位置繼續: 幀 {cur_idx}")
    else:
        cur_idx = max(0, min(start, total - 1))
    cap.set(cv2.CAP_PROP_POS_FRAMES, cur_idx)
    win = f"人工標示確認 - {os.path.basename(video_path)}"
    has_gui = True
    try:
        # 使用 KEEPRATIO 保持長寬比，避免 trackbar 佔據空間導致畫面被切
        cv2.namedWindow(win, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)
        # 不建立 trackbar，改用鍵盤導航；視窗大小在第一幀顯示時依實際 disp 尺寸設定
    except Exception as e:
        print(f"視窗建立失敗（{e}），無法互動確認")
        has_gui = False

    dirty = False

    # ---- 人工穿越標記狀態（Option A：標線前端掃過橙色參考列的瞬間，單點） ----
    pending: dict | None = None  # {"side","point":(x,y),"frame"}
    current_side = "left"  # Tab 切換 b/w 單點補標的側別（穿越點點擊自動判側）
    disp_scale: dict[str, float] = {"s": 1.0}  # 顯示→原圖縮放比例（mouse 回呼在閉包內共享）

    def _mouse_2pt(event: int, x: int, y: int, flags: int, _param: object | None) -> None:
        nonlocal pending, current_side
        if event not in (cv2.EVENT_LBUTTONDOWN, cv2.EVENT_MOUSEMOVE):
            return
        s = disp_scale.get("s", 1.0)
        iw = int(disp_scale.get("w", 0))     # 顯示影像寬（縮小後）
        ih = int(disp_scale.get("h", 0))     # 顯示影像高
        # OpenCV HighGUI 滑鼠座標為「影像內」座標（0,0=顯示影像左上），直接等比映射回原解析度
        if not (iw > 0 and ih > 0):
            return
        if not (0 <= x < iw and 0 <= y < ih):
            return  # 點在影像外（trackbar/邊框），忽略
        px, py = int(round(x / s)), int(round(y / s))  # 映射回原解析度
        # 滑鼠移動：僅更新游標預覽（不建 pending）
        if event == cv2.EVENT_MOUSEMOVE:
            if pending is not None:
                pending["cursor"] = (px, py)
            return
        # 點擊除錯：寫入暫存檔，供座標映射診斷（滑鼠→原圖）
        try:
            with open(_CLICK_DEBUG_PATH, "a", encoding="utf-8") as _df:
                _df.write(
                    f"click raw=({x},{y}) disp=({iw}x{ih}) s={s} -> orig=({px},{py}) box={selected}\n"
                )
        except Exception:
            pass
        # 左鍵點擊：在前端掃過橙色參考列的瞬間，把那一點記為穿越點。
        # 側別由點擊 x 相對中線（掃描框中央）自動判定：右側即右線，左側即左線，
        # 不需 Tab 切換（Tab 僅影響 b/w 單點補標模式）。
        div_x = (selected[0] + selected[2]) // 2 if selected else w // 2
        click_side = "right" if px >= div_x else "left"
        if pending is None or pending.get("frame") != cur_idx:
            pending = {"side": click_side, "point": (px, py), "frame": cur_idx}
        else:
            pending["point"] = (px, py)
            pending["side"] = click_side
        print(f"\r[穿越標記] {'右' if click_side == 'right' else '左'}側 @ 幀 {cur_idx} 點({px},{py}) 翻幀自動儲存        ", end="", flush=True)

    if has_gui:
        try:
            cv2.setMouseCallback(win, _mouse_2pt)
        except Exception:
            pass

    def _read_frame_at(idx: int) -> np.ndarray | None:
        if not cap.set(cv2.CAP_PROP_POS_FRAMES, idx):
            return None
        ret2, fr = cap.read()
        if not ret2 or fr is None:
            return None
        return fr

    def _jump_candidate(delta: int) -> None:
        nonlocal cur_idx
        cands = sorted(int(a["frame"]) for a in annotations)
        if not cands:
            return
        if delta > 0:
            nxt = [c for c in cands if c > cur_idx]
            if nxt:
                cur_idx = nxt[0]
            else:
                cur_idx = cands[0]
        else:
            prv = [c for c in cands if c < cur_idx]
            if prv:
                cur_idx = prv[-1]
            else:
                cur_idx = cands[-1]

    def _commit_pending() -> bool:
        """提交待儲存穿越點（有點才存）。回傳是否存了。

        點擊設點後翻幀即自動呼叫，不需 Enter；Enter 為立即儲存。
        overlay 快取鍵含 pending 狀態，pending 清空後自然重繪，不需手動失效。
        """
        nonlocal pending, dirty
        if pending is not None and pending.get("point") is not None:
            ev = {
                "kind": "cross",
                "side": pending["side"],
                "point": [int(v) for v in pending["point"]],
                "frame": pending["frame"],
                "time": _get_time_for_frame(pending["frame"], fps, pts),
                "auto": False,
            }
            annotations.append(ev)
            dirty = True
            print(f"\r[人工穿越] {'右' if ev['side'] == 'right' else '左'}側 @ 幀 {ev['frame']} 前端點{ev['point']}", end="", flush=True)
            pending = None
            return True
        return False

    print("按鍵: n/p 上下幀, [ ] 跳候選, b 黑→白, w=白→黑 r=移除, s=存檔, g 跳幀, q 離開")
    print("穿越標記: 在中線左/右側點一下（自動判側）→ 翻幀自動儲存（Enter 立存）；Tab 只切換 b/w 補標側")

    last_shown_idx = -1
    cached_fr: np.ndarray | None = None
    cached_overlay: np.ndarray | None = None
    cache_key: tuple | None = None
    window_size_set = False
    while True:
        if has_gui:
            # 幀快取：僅在切幀時重新解碼
            if cached_fr is None or last_shown_idx != cur_idx:
                fr = _read_frame_at(cur_idx)
                if fr is None:
                    print(f"幀 {cur_idx} 讀取失敗，跳過")
                    cur_idx = min(cur_idx + 1, total - 1)
                    continue
                cached_fr = fr
                last_shown_idx = cur_idx
                cached_overlay = None  # 幀變了需重繪 overlay
                cache_key = None
            else:
                fr = cached_fr

            # Overlay 快取：依 (幀索引, pending 狀態, Tab 側) 快取，避免重複 PIL 轉換
            # pending 狀態以 (frame, point, cursor, side) 為鍵
            if pending is not None:
                p_key = (pending.get("frame"), pending.get("point"), pending.get("cursor"), pending.get("side"))
            else:
                p_key = None
            new_cache_key = (cur_idx, p_key, current_side)

            if cached_overlay is not None and cache_key == new_cache_key:
                overlay = cached_overlay
            else:
                overlay = _draw_overlay(fr, selected, annotations, cur_idx, fps, pts, w, h,
                                        pending, current_side)
                cached_overlay = overlay
                cache_key = new_cache_key

            disp, sc = _resize_for_display(overlay)
            disp_scale["s"] = sc
            disp_scale["w"] = float(disp.shape[1])
            disp_scale["h"] = float(disp.shape[0])
            key = None
            try:
                cv2.imshow(win, disp)
                if not window_size_set:
                    # 首次顯示時設定視窗大小為實際顯示尺寸，確保完整可見
                    cv2.resizeWindow(win, disp.shape[1], disp.shape[0])
                    window_size_set = True
                # 短 timeout：滑鼠事件（點擊/移動）後能即時重繪；無按鍵時回 -1 重新整理
                key = cv2.waitKeyEx(30)
            except Exception as e:
                print(f"視窗操作失敗（{e}），改用單步前進")
                has_gui = False

            if key is None or key == -1:
                continue
        else:
            # 無 GUI：無法互動，結束（候選已存於 annotations，可用 --output 存檔）
            print("無圖形介面，跳過人工確認階段")
            break

        # ---- 人工穿越標記按鍵（Option A：前端掃過橙色參考列的瞬間，單點） ----
        if key == 9:  # Tab：只切換 b/w 單點補標的側別；穿越點側別由點擊位置自動判定，不受影響
            current_side = "right" if current_side == "left" else "left"
            print(f"\r[b/w補標] 切換為 {'右側' if current_side == 'right' else '左側'}          ", end="", flush=True)
            continue
        if key == 13 and pending is not None:  # Enter：立即儲存穿越標記
            _commit_pending()
            continue

        if key in (27, ord("q"), ord("Q")):
            if pending is not None:
                pending = None
                print("\r[穿越] 已取消未完成標示          ", end="", flush=True)
                continue
            if dirty:
                print("有未存檔變更，是否存檔後離開？ (y/n/c): ", end="", flush=True)
                try:
                    ans = input().strip().lower()
                except Exception:
                    ans = "y"
                if ans == "c":
                    continue
                if ans in ("y", "yes", ""):
                    _save_annotations(out_path, video_path, w, h, fps, selected, annotations, removed)
                    dirty = False
            break
        if key in (ord("n"), ord("N"), 2555904, 83, ord("d"), ord("D")):
            _commit_pending()  # 點擊設點後翻幀即儲存，不需 Enter
            cur_idx = min(cur_idx + 1, total - 1)
            continue
        if key in (ord("]"), 63235):  # 右方括號 / End 鍵附近
            _commit_pending()
            _jump_candidate(1)
            continue
        if key == ord("."):
            _commit_pending()
            _jump_candidate(1)
            continue
        if key in (ord("p"), ord("P"), 2424832, 81, ord("a"), ord("A")):
            _commit_pending()
            cur_idx = max(cur_idx - 1, 0)
            continue
        if key == ord(","):
            _commit_pending()
            _jump_candidate(-1)
            continue
        if key in (ord("["),):
            _commit_pending()
            _jump_candidate(-1)
            continue
        if key in (ord("b"), ord("B")):
            if any(int(a.get("frame", -1)) == cur_idx and a.get("type") == "bw" and a.get("side") == current_side for a in annotations):
                print(f"幀 {cur_idx} [{'左' if current_side == 'left' else '右'}] 已標 黑→白，略過")
            else:
                annotations.append({"frame": cur_idx, "type": "bw", "side": current_side, "time": _get_time_for_frame(cur_idx, fps, pts), "label": "黑→白", "auto": False})
                dirty = True
                cached_overlay = None  # 立即失效 overlay 快取，即時顯示新標記
                print(f"\r[人工] 黑→白 @ 幀 {cur_idx} [{'左' if current_side == 'left' else '右'}]          ", end="", flush=True)
            continue
        if key in (ord("w"), ord("W")):
            if any(int(a.get("frame", -1)) == cur_idx and a.get("type") == "wb" and a.get("side") == current_side for a in annotations):
                print(f"幀 {cur_idx} [{'左' if current_side == 'left' else '右'}] 已標 白→黑，略過")
            else:
                annotations.append({"frame": cur_idx, "type": "wb", "side": current_side, "time": _get_time_for_frame(cur_idx, fps, pts), "label": "白→黑", "auto": False})
                dirty = True
                cached_overlay = None  # 立即失效 overlay 快取，即時顯示新標記
                print(f"\r[人工] 白→黑 @ 幀 {cur_idx} [{'左' if current_side == 'left' else '右'}]          ", end="", flush=True)
            continue
        if key in (ord("r"), ord("R"), 255, 8):
            cur_marks = [a for a in annotations if int(a.get("frame", -1)) == cur_idx]
            if cur_marks:
                annotations = [a for a in annotations if int(a.get("frame", -1)) != cur_idx]
                # 刪除軌跡：記下被刪的原始紀錄（供錯誤分析，不會復活）
                for m in cur_marks:
                    removed.append({**m, "removed_via": "r",
                                    "removed_box": list(selected) if selected else None})
                dirty = True
                cached_overlay = None  # 立即失效 overlay 快取，即時顯示移除結果
                print(f"\r[移除] 幀 {cur_idx} 的 {len(cur_marks)} 筆標記（已記入刪除軌跡，共 {len(removed)} 筆）          ", end="", flush=True)
            elif annotations:
                popped = annotations.pop()
                removed.append({**popped, "removed_via": "r",
                                "removed_box": list(selected) if selected else None})
                dirty = True
                cached_overlay = None  # 立即失效 overlay 快取
                print(f"\r[移除] 幀 {popped.get('frame')} {popped.get('type')}（已記入刪除軌跡）          ", end="", flush=True)
            else:
                print("無標記可移除")
            continue
        if key in (ord("s"), ord("S")):
            _commit_pending()  # 先收尚未提交的點擊，避免存檔遺漏
            _save_annotations(out_path, video_path, w, h, fps, selected, annotations, removed)
            dirty = False
            print(f"已存檔 {len(annotations)} 筆 -> {out_path}")
            continue
        if key in (ord("g"), ord("G")):
            _commit_pending()  # 跳幀前先收（同翻幀自動儲存語意）
            try:
                if has_gui:
                    cv2.destroyWindow(win)
                    cv2.waitKey(1)
                txt = input(f"跳至幀號 (0-{total-1}): ").strip()
                if has_gui:
                    cv2.namedWindow(win, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)
                if txt.isdigit():
                    cur_idx = max(0, min(int(txt), total - 1))
                else:
                    print("無效幀號")
            except Exception as e:
                print(f"跳幀失敗: {e}")
                if has_gui:
                    try:
                        cv2.namedWindow(win, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)
                    except Exception:
                        pass
            continue
        # 移除 trackbar 相關代碼，改用鍵盤導航

    cap.release()
    if has_gui:
        try:
            cv2.destroyAllWindows()
        except Exception:
            pass
    print(f"結束，共 {len(annotations)} 筆標示 -> {out_path}")
    if dirty:
        print("有未存檔變更；請重開並按 s 存檔")


def _resolve_video_path(raw: str) -> str:
    if os.path.isabs(raw) and os.path.exists(raw):
        return os.path.abspath(raw)
    for candidate in (
        os.path.join(_proj_root, os.path.basename(raw)),
        os.path.join(_proj_root, raw),
        raw,
    ):
        if os.path.exists(candidate):
            return os.path.abspath(candidate)
    print(f"找不到影片: {raw}")
    print(f"已搜尋: {raw}, {_proj_root}/{os.path.basename(raw)}")
    sys.exit(1)


def _audit_annotations(out_dir: str) -> None:
    """彙整目錄下 *_annotations.json 的刪除軌跡，供錯誤分析。

    輸出：每支影片的有效/移除筆數、移除中 auto 佔比、型態×側別分佈、
    閃爍對比例（同側 bw/wb 相鄰 ≤1 幀——此類應已被 _remove_flicker_pairs 擋下，
    若仍大量出現代表濾波失效或閾值需調）。
    """
    import glob

    paths = sorted(glob.glob(os.path.join(out_dir, "*_annotations.json")))
    if not paths:
        print(f"{out_dir} 下無 *_annotations.json")
        return
    print(f"{'檔名':34s} {'有效':>5s} {'移除':>5s} {'移除auto%':>9s} {'閃爍對%':>8s} 型態x側別(top)")
    tot_ann, tot_rem = 0, 0
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"{os.path.basename(p)}: 讀取失敗 {e}")
            continue
        anns = data.get("annotations", []) or []
        rems = data.get("removed_annotations", []) or []
        tot_ann += len(anns)
        tot_rem += len(rems)
        n_auto = sum(1 for r in rems if bool(r.get("auto", False)))
        # 型態×側別分佈（cross 類無 type，顯示 kind）
        dist: dict[str, int] = {}
        for r in rems:
            k = f"{r.get('type') or r.get('kind', '?')}/{r.get('side', '?')}"
            dist[k] = dist.get(k, 0) + 1
        top = ",".join(f"{k}x{v}" for k, v in sorted(dist.items(), key=lambda kv: -kv[1])[:3])
        # 閃爍對：同側 bw/wb 相鄰 ≤1 幀
        flick = 0
        by_side: dict[str, list] = {}
        for r in rems:
            by_side.setdefault(r.get("side", "?"), []).append(r)
        for lst in by_side.values():
            ts = sorted((float(x.get("frame", -1)), str(x.get("type") or x.get("kind") or ""))
                        for x in lst
                        if isinstance(x.get("frame"), (int, float)))
            for (f0, t0), (f1, t1) in zip(ts, ts[1:], strict=False):
                if t0 != t1 and abs(f1 - f0) <= 1.5:
                    flick += 1
        print(f"{os.path.basename(p):34s} {len(anns):5d} {len(rems):5d} "
              f"{(n_auto / len(rems) * 100 if rems else 0):8.0f}% "
              f"{(flick / max(len(rems), 1) * 100):7.0f}% {top}")
    print(f"合計：有效 {tot_ann} 筆，移除 {tot_rem} 筆")


def main() -> None:
    parser = argparse.ArgumentParser(description="人工標示 — 幀級標線穿越標定（框選→自動偵測→人工確認）")
    parser.add_argument("videos", nargs="*", help="一支或多支影片路徑（可含中文/空白）")
    parser.add_argument("--output", "-o", default=None, help="單支影片時的輸出路徑（多支時忽略）")
    parser.add_argument("--output-dir", "-d", default=None, help="批量輸出目錄（預設專案根目錄）")
    parser.add_argument("--start", type=int, default=0, help="起始幀")
    parser.add_argument("--box", type=int, nargs=4, default=None,
                        help="跳過框選，直接指定掃描範圍 x0 y0 x1 y1（原解析度像素）")
    parser.add_argument("--auto-only", action="store_true",
                        help="僅自動偵測並存檔（不開啟人工確認視窗），供 batch 產出候選用")
    parser.add_argument("--audit", action="store_true",
                        help="彙整輸出目錄下 *_annotations.json 的刪除軌跡（不需 videos 參數）")
    args = parser.parse_args()

    if args.audit:
        out_dir = os.path.abspath(args.output_dir) if args.output_dir else _proj_root
        _audit_annotations(out_dir)
        return

    if not args.videos:
        print("需指定影片路徑（或用 --audit 只做刪除軌跡彙整）")
        sys.exit(1)
    video_paths = [_resolve_video_path(v) for v in args.videos]

    box: tuple[int, int, int, int] | None = None
    if args.box is not None:
        x0, y0, x1, y1 = args.box
        if not (0 <= x0 < x1 and 0 <= y0 < y1):
            print(f"--box 非法: {args.box}")
            sys.exit(1)
        box = (x0, y0, x1, y1)

    if len(video_paths) == 1 and args.output:
        out_path = os.path.abspath(args.output)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        _process_video(video_paths[0], out_path, args.start, box, args.auto_only)
    else:
        out_dir = os.path.abspath(args.output_dir) if args.output_dir else _proj_root
        os.makedirs(out_dir, exist_ok=True)
        print(f"輸出目錄: {out_dir}")
        print("        (檔名 = {影片名}_annotations.json，重新執行會自動載入續標)")
        for vp in video_paths:
            base = os.path.splitext(os.path.basename(vp))[0]
            out_path = os.path.join(out_dir, f"{base}_annotations.json")
            _process_video(vp, out_path, args.start, box, args.auto_only)


if __name__ == "__main__":
    main()
