"""Speed ROI 自動偵測引擎

使用 OpenCV 影像處理定位車道標線區域。
純 OpenCV 實作，無 PyTorch/CUDA 依賴。

偵測邏輯：
1. 車體上緣偵測（hood_detector，中央欄 V 階躍，Phase A/B）
2. 取樣多幀（掃描上限至 hood_top - margin，無車體時 0.85）
3. 自適應二值化分離亮色標線（白/黃）在暗色路面上
4. 找輪廓 → 篩選 elongated vertical shapes（標線特徵）
5. 分析左右分佈 → 判定 side (left/right/both)
6. 計算 union ROI（底邊 clamp 至車體上緣之上，切掉 >30% 降信心）
"""

from __future__ import annotations

import os
import sys

if __package__ in (None, ""):
    _proj_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _proj_root not in sys.path:
        sys.path.insert(0, _proj_root)

import cv2
import numpy as np

from dashcam_player.models.speed_roi import SpeedROIDetectionResult
from dashcam_player.utils.cv_utils import open_video_capture, sample_frame_indices
from dashcam_player.utils.hood_detector import detect_hood_top
from dashcam_player.utils.log import get_logger
from dashcam_player.utils.pts_loader import get_reliable_fps_count

log = get_logger()

# ROI 底邊與車體上緣的安全距離（正規化 H）。人工框底邊即 hood_top（誤差 ±0.015），
# 自動框留 0.02 margin 確保反光帶不入框。
HOOD_MARGIN = 0.02


class SpeedROIDetector:
    """車道標線 ROI 自動偵測器"""

    def __init__(self, video_path: str) -> None:
        self._video_path = video_path

    def detect(self) -> SpeedROIDetectionResult | None:
        """偵測車道標線 ROI，回傳 DetectionResult 或 None"""
        cap = open_video_capture(self._video_path)
        if not cap.isOpened():
            log.warning(f"SpeedROI 偵測：無法開啟影片 {self._video_path}")
            return None

        try:
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0

            if w <= 0 or h <= 0:
                return None

            # 檔頭損壞的 AVI（宣稱 600fps/26477 幀，實際 5fps/223 幀）會讓取樣
            # 位置落在不存在的幀上 → 以 ffprobe 的實際資料取代檔頭值
            reliable = get_reliable_fps_count(self._video_path)
            if reliable is not None:
                fps, total = reliable

            fmt = os.path.splitext(self._video_path)[1].lstrip(".").lower() or "mp4"
            indices = sample_frame_indices(total, fps)

            # 車體上緣：候選掃描與 union ROI 的底邊上限（Phase C）。
            # hood_top 有值 → 上限 hood_top - margin；None = 無車體簽名（後鏡頭等），
            # 維持 0.85 不限縮（不可用保守 0.64，後鏡頭有效路面到 0.74）。
            hood_top: float | None = None
            try:
                hood_res = detect_hood_top(self._video_path)
                hood_top = hood_res.hood_top_y if hood_res else None
            except Exception as e:
                log.warning(f"SpeedROI 偵測：hood 偵測失敗（{e}），不套用車體限制")
            scan_y_max = min(0.85, hood_top - HOOD_MARGIN) if hood_top is not None else 0.85

            # 收集所有候選框
            all_boxes: list[list[int]] = []
            best_idx = indices[0]

            for idx in indices:
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ret, frame = cap.read()
                if not ret or frame is None:
                    continue

                boxes = self._detect_marking_boxes(frame, w, h, y_max_ratio=scan_y_max)
                if boxes:
                    all_boxes.extend(boxes)
                    best_idx = idx

            if not all_boxes:
                log.info("SpeedROI 偵測：未偵測到標線候選區域")
                return None

            # 分析左右分佈
            side = self._determine_side(all_boxes, w)

            # 計算 union ROI（底邊 clamp 至車體上緣之上，順便回傳被切掉的高度比）
            (rx, ry, rw, rh), cut_ratio = self._compute_union_roi(
                all_boxes, w, h, hood_top_y=hood_top
            )

            # clamp 切掉 >30% 高度 → 候選框主力在車體區，可信度降級
            confidence = 0.4 if cut_ratio > 0.30 else 0.7

            log.info(
                f"SpeedROI 偵測成功: side={side}, "
                f"roi=({rx:.3f}, {ry:.3f}, {rw:.3f}, {rh:.3f}), "
                f"boxes={len(all_boxes)}, hood_top={hood_top}"
            )

            return SpeedROIDetectionResult(
                video_path=self._video_path,
                video_width=w,
                video_height=h,
                file_format=fmt,
                detected_roi=(rx, ry, rw, rh),
                detected_side=side,
                confidence=confidence,
                sample_frame_idx=best_idx,
                hood_top_y=hood_top,
            )
        finally:
            cap.release()

    def _detect_marking_boxes(
        self, frame: np.ndarray, w: int, h: int, y_max_ratio: float = 0.85
    ) -> list[list[int]]:
        """從單一幀偵測標線候選框。

        Args:
            y_max_ratio: 候選掃描下限（正規化）。有車體時傳入 hood_top - margin，
                無車體時 0.85（維持舊行為，後鏡頭有效路面到 0.74）。
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # 只看畫面中段（30% 高度起，至車體上緣或 0.85），避開天空和引擎蓋
        y1 = int(h * 0.30)
        y2 = max(y1 + 1, int(h * y_max_ratio))
        roi = gray[y1:y2, :]

        boxes: list[list[int]] = []

        # 策略 1: 自適應閾值（穩健）
        adaptive = cv2.adaptiveThreshold(
            roi, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 31, -15
        )
        boxes.extend(self._find_marking_contours(adaptive, 0, y1, w, h))

        # 策略 2: 固定高閾值（白/黃標線在暗路面上）
        mean_val = float(np.mean(roi))
        thresh = max(int(mean_val + 50), 150)
        _, fixed = cv2.threshold(roi, thresh, 255, cv2.THRESH_BINARY)
        boxes.extend(self._find_marking_contours(fixed, 0, y1, w, h))

        # 策略 3: HSV — 孤立高亮度低飽和度像素（白/黃標線）
        hsv = cv2.cvtColor(frame[y1:y2, :], cv2.COLOR_BGR2HSV)
        # 白色: V>180, S<60  |  黃色: H in [20,35], V>150
        white_mask = (hsv[:, :, 2] > 180) & (hsv[:, :, 1] < 60)
        yellow_mask = (hsv[:, :, 0] >= 20) & (hsv[:, :, 0] <= 35) & (hsv[:, :, 2] > 150)
        combined = np.zeros_like(roi)
        combined[white_mask | yellow_mask] = 255
        # 膨脹合併相鄰像素
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 3))
        combined = cv2.dilate(combined, kernel, iterations=1)
        boxes.extend(self._find_marking_contours(combined, 0, y1, w, h))

        return self._dedupe_boxes(boxes)

    def _find_marking_contours(
        self, binary: np.ndarray, offset_x: int, offset_y: int,
        frame_w: int, frame_h: int,
    ) -> list[list[int]]:
        """從二值化影像找標線候選框"""
        # 膨脹合併相鄰亮區
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 5))
        dilated = cv2.dilate(binary, kernel, iterations=2)

        contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        boxes: list[list[int]] = []
        for cnt in contours:
            x, y, bw, bh = cv2.boundingRect(cnt)
            area = bw * bh

            # 篩選條件
            rel_cy = (y + bh / 2) / frame_h
            rel_bw = bw / frame_w
            rel_bh = bh / frame_h

            # 標線在畫面中段
            if rel_cy < 0.20 or rel_cy > 0.90:
                continue
            # 寬度合理（不超過畫面 30%，不小於 0.5%）
            if rel_bw < 0.005 or rel_bw > 0.30:
                continue
            # 高度合理（不小於 1%，不大於 40%）
            if rel_bh < 0.01 or rel_bh > 0.40:
                continue
            # 面積合理
            if area < 100 or area > frame_w * frame_h * 0.15:
                continue
            # 標線偏垂直（高/寬比 > 0.3）
            if bh < bw * 0.3:
                continue

            # 轉換為全幀座標
            boxes.append([x + offset_x, y + offset_y, bw, bh])

        return boxes

    def _dedupe_boxes(self, boxes: list[list[int]]) -> list[list[int]]:
        """去重：量化座標後去除重複框"""
        if not boxes:
            return []
        seen: set[tuple[int, ...]] = set()
        unique: list[list[int]] = []
        for b in boxes:
            key = (b[0] // 10, b[1] // 10, b[2] // 10, b[3] // 10)
            if key not in seen:
                seen.add(key)
                unique.append(b)
        return unique

    def _determine_side(self, boxes: list[list[int]], frame_w: int) -> str:
        """分析標線分佈判定 side"""
        mid = frame_w // 2
        left_count = sum(1 for b in boxes if b[0] + b[2] // 2 < mid)
        right_count = len(boxes) - left_count

        total = len(boxes)
        if total == 0:
            return "both"

        # 任一側佔比超過 70% → 側向
        if left_count / total > 0.7:
            return "left"
        if right_count / total > 0.7:
            return "right"
        return "both"

    def _compute_union_roi(
        self, boxes: list[list[int]], frame_w: int, frame_h: int,
        hood_top_y: float | None = None,
    ) -> tuple[tuple[float, float, float, float], float]:
        """計算所有框的 union ROI (relative 0.0-1.0)。

        有車體時底邊 clamp 至 ``hood_top_y - HOOD_MARGIN`` 之上。
        回傳 ((rx, ry, rw, rh), cut_ratio)，cut_ratio = 被 clamp 切掉的高度
        佔 clamp 前 union 高度的比例（供呼叫端降信心用）。
        """
        if not boxes:
            return (0.05, 0.40, 0.90, 0.45), 0.0  # default

        min_x = min(b[0] for b in boxes)
        min_y = min(b[1] for b in boxes)
        max_x = max(b[0] + b[2] for b in boxes)
        max_y = max(b[1] + b[3] for b in boxes)

        # 加 padding
        pad_x = int(frame_w * 0.03)
        pad_y = int(frame_h * 0.03)
        min_x = max(0, min_x - pad_x)
        min_y = max(0, min_y - pad_y)
        max_x = min(frame_w, max_x + pad_x)
        max_y = min(frame_h, max_y + pad_y)

        cut_ratio = 0.0
        if hood_top_y is not None:
            limit_y = int((hood_top_y - HOOD_MARGIN) * frame_h)
            if max_y > limit_y and limit_y > min_y:
                raw_h = max_y - min_y
                cut_ratio = (max_y - limit_y) / raw_h if raw_h > 0 else 0.0
                max_y = limit_y

        rx = min_x / frame_w
        ry = min_y / frame_h
        rw = (max_x - min_x) / frame_w
        rh = (max_y - min_y) / frame_h

        return (rx, ry, rw, rh), cut_ratio
