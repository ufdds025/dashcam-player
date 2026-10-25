"""GPS ROI 自動偵測引擎

使用 OpenCV 影像處理快速定位 GPS 文字區域（僅定位，不辨識文字）。
成功後可供後續 OCR 直接使用，免除每幀 4 角落 × 6 預處理的開銷。

無 PyTorch/CUDA 依賴，純 OpenCV 影像處理實作。
"""

from __future__ import annotations

import os
import re

import cv2
import numpy as np

from dashcam_player.models.gps_roi import GPSROIDetectionResult
from dashcam_player.utils.cv_utils import (
    BLACK_BORDER_THRESH,
    WHITE_TEXT_THRESHOLDS,
    black_border_mask,
    open_video_capture,
    preprocess_clahe_otsu,
    sample_frame_indices,
    white_text_mask,
)
from dashcam_player.utils.log import get_logger
from dashcam_player.utils.pts_loader import get_reliable_fps_count

log = get_logger()

_GPS_KEYWORDS = re.compile(r"[NSEW]", re.IGNORECASE)
_MIN_DIGIT_COUNT = 6


def _is_likely_gps_bbox(
    bbox: list[list[float]], frame_w: int, frame_h: int
) -> bool:
    """根據位置與尺寸粗篩 bounding box 是否可能含 GPS 文字"""
    xs = [p[0] for p in bbox]
    ys = [p[1] for p in bbox]
    cy = sum(ys) / len(ys)
    bw = max(xs) - min(xs)
    bh = max(ys) - min(ys)

    rel_cy = cy / frame_h
    rel_bw = bw / frame_w
    rel_bh = bh / frame_h

    in_corner = rel_cy < 0.20 or rel_cy > 0.80
    reasonable_size = 0.02 < rel_bw < 0.35 and 0.005 < rel_bh < 0.12
    not_too_small = bw > 30 and bh > 8

    return in_corner and reasonable_size and not_too_small


def _compute_union_roi(
    bboxes: list[list[list[float]]],
    frame_w: int,
    frame_h: int,
    padding_x: float = 0.03,
    padding_y: float = 0.05,
) -> tuple[float, float, float, float]:
    """計算多個 bounding box 的 union ROI（相對座標 0~1）"""
    min_x: float = float(frame_w)
    min_y: float = float(frame_h)
    max_x: float = 0.0
    max_y: float = 0.0

    for bbox in bboxes:
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        min_x = min(min_x, min(xs))
        min_y = min(min_y, min(ys))
        max_x = max(max_x, max(xs))
        max_y = max(max_y, max(ys))

    pad_x = int(frame_w * padding_x)
    pad_y = int(frame_h * padding_y)

    x1 = max(0, min_x - pad_x)
    y1 = max(0, min_y - pad_y)
    x2 = min(frame_w, max_x + pad_x)
    y2 = min(frame_h, max_y + pad_y)

    return (x1 / frame_w, y1 / frame_h, (x2 - x1) / frame_w, (y2 - y1) / frame_h)


def _bbox_to_corner(
    bbox: list[list[float]], frame_w: int, frame_h: int
) -> str:
    """判斷 bounding box 所在的角落"""
    xs = [p[0] for p in bbox]
    ys = [p[1] for p in bbox]
    cx = sum(xs) / len(xs)
    cy = sum(ys) / len(ys)

    top = cy < frame_h / 2
    left = cx < frame_w / 2

    if top and left:
        return "top_left"
    if top and not left:
        return "top_right"
    if not top and left:
        return "bottom_left"
    return "bottom_right"


def _sample_frame(video_path: str, frame_idx: int) -> np.ndarray | None:
    """從影片取樣指定幀"""
    cap = open_video_capture(video_path)
    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
    ret, frame = cap.read()
    cap.release()
    return frame if ret else None


class GPSROIDetector:
    """GPS ROI 自動偵測器

    取樣 3 幀（0s/3s/6s），使用 OpenCV 影像處理定位文字區塊，
    篩選可能含 GPS 的區塊後計算 union ROI。
    僅用於定位，不辨識文字內容。

    無 PyTorch/CUDA 依賴，純 OpenCV 影像處理。
    """

    def __init__(self) -> None:
        pass

    def detect(self, video_path: str) -> GPSROIDetectionResult | None:
        cap = open_video_capture(video_path)
        if not cap.isOpened():
            return None

        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()

        if frame_w <= 0 or frame_h <= 0:
            return None

        # 檔頭損壞的 AVI（宣稱 600fps/26477 幀，實際 5fps/223 幀）會讓取樣
        # 位置落在不存在的幀上 → 以 ffprobe 的實際資料取代檔頭值
        reliable = get_reliable_fps_count(video_path)
        if reliable is not None:
            fps, total_frames = reliable

        # 檔頭損壞的 AVI（宣稱 600fps/26477 幀，實際 5fps/223 幀）會讓取樣
        # 位置落在不存在的幀上 → 以 ffprobe 的實際資料取代檔頭值
        reliable = get_reliable_fps_count(video_path)
        if reliable is not None:
            fps, total_frames = reliable

        sample_indices = sample_frame_indices(total_frames, fps)

        all_gps_bboxes: list[list[list[float]]] = []
        best_frame_idx = 0

        for idx in sample_indices:
            frame = _sample_frame(video_path, idx)
            if frame is None:
                continue
            bboxes = self._detect_text_boxes(frame)
            gps_bboxes = [b for b in bboxes if self._is_gps_bbox(b, frame_w, frame_h)]
            if gps_bboxes:
                all_gps_bboxes.extend(gps_bboxes)
                if not best_frame_idx:
                    best_frame_idx = idx

        if not all_gps_bboxes:
            log.info("GPS ROI 自動偵測：未找到 GPS 文字區塊")
            return None

        # 去重（多門檻會產生重複 bbox）
        dedup: dict[tuple[int, int, int, int], list[list[float]]] = {}
        for b in all_gps_bboxes:
            xs = [p[0] for p in b]
            ys = [p[1] for p in b]
            key = (int(min(xs)), int(min(ys)), int(max(xs) - min(xs)), int(max(ys) - min(ys)))
            dedup[key] = b
        deduped = list(dedup.values())

        # 按角落分組，選多數決角落避免路面白線等雜訊把 union 拉成全畫面
        # 實測 captures/dash_20260827_*.png：raw top_left 21 vs bottom 共 15，若全 union 得 (0,0,1,1)
        # dedup 後因重複 bbox 合併會逆轉多數決（11 vs 7），故以 raw 計數為準決定主角落，
        # 再對 deduped 過濾得精準 ROI。
        from collections import Counter

        raw_corners = [_bbox_to_corner(b, frame_w, frame_h) for b in all_gps_bboxes]
        most_common = Counter(raw_corners).most_common(1)[0][0] if raw_corners else _bbox_to_corner(deduped[0], frame_w, frame_h)
        filtered = [b for b in deduped if _bbox_to_corner(b, frame_w, frame_h) == most_common]
        # 若過濾後過少（<2），可能是誤分組，退回全部
        target_bboxes = filtered if len(filtered) >= 2 else deduped

        roi = _compute_union_roi(target_bboxes, frame_w, frame_h)
        corner = _bbox_to_corner(target_bboxes[0], frame_w, frame_h)
        # 使用者回報左上角要「靠右一點」以避開 A 柱黑邊（見截圖左緣黑塊），
        # 若自動偵測的 ROI 貼齊左邊界 (x<0.02)，向右微調 0.015 並相應縮寬，保持右邊界不動
        if corner == "top_left" and roi[0] < 0.02:
            shift = 0.015
            # 確保不超出右邊界
            new_x = roi[0] + shift
            new_w = max(0.05, roi[2] - shift)
            # 右邊界 = 原 x+w 保持不變，故 new_w = 原右 - new_x
            orig_right = roi[0] + roi[2]
            new_w = max(0.05, orig_right - new_x)
            roi = (new_x, roi[1], new_w, roi[3])
            log.info(f"GPS ROI 頂左微調向右 {shift:.3f} 以避開 A 柱")
        fmt = os.path.splitext(video_path)[1]

        log.info(
            f"GPS ROI 自動偵測成功: corner={corner}, "
            f"roi=({roi[0]:.3f}, {roi[1]:.3f}, {roi[2]:.3f}, {roi[3]:.3f})"
        )

        return GPSROIDetectionResult(
            video_path=video_path,
            video_width=frame_w,
            video_height=frame_h,
            file_format=fmt,
            detected_roi=roi,
            detected_corner=corner,
            confidence=0.7,
            sample_frame_idx=best_frame_idx,
        )

    def _detect_text_boxes(
        self, frame: np.ndarray
    ) -> list[list[list[float]]]:
        """使用 OpenCV 影像處理偵測文字區域

        透過二值化、膨脹與輪廓檢測尋找可能含文字的區域。
        同時支援：
          - 傳統 Otsu 分支（黑字白底）
          - 白字黑框分支（高門檻 200 隔離白字內核，白字白底 → 白字黑底）
            實測：白字(255)黑框(0)在白底天空(≈190)場景，Otsu 二值化會把
            天空判為白，文字熔入背景導致 0 個 bbox；高門檻分支在
            captures/dash_20260827_*.png 上可穩定產出 5 個 GPS 相關 bbox。
          - HSV 白色遮罩分支（V>200,S<40，對過曝/偏色白字補強）
        三分支結果合併後由上層 _is_gps_bbox 統一過濾。
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        frame_h, frame_w = frame.shape[:2]
        all_bboxes: list[list[list[float]]] = []

        def _collect(mask: np.ndarray, dilated: np.ndarray) -> None:
            contours, _ = cv2.findContours(
                dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            for cnt in contours:
                area = cv2.contourArea(cnt)
                if area < 50 or area > frame_w * frame_h * 0.15:
                    continue
                x, y, w, h = cv2.boundingRect(cnt)
                if w == 0 or h == 0:
                    continue
                aspect_ratio = w / h if h > 0 else 0
                if aspect_ratio < 1.5 and aspect_ratio > 0:
                    if area < 200:
                        continue
                if w < 10 or h < 10:
                    continue
                all_bboxes.append([
                    [float(x), float(y)],
                    [float(x + w), float(y)],
                    [float(x + w), float(y + h)],
                    [float(x), float(y + h)],
                ])

        # --- 分支 A：傳統 Otsu（黑字白底 / 通用，含反相分支以涵蓋黑字/白字兩極性）---
        _, binary = preprocess_clahe_otsu(gray)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        dilated = cv2.dilate(binary, kernel, iterations=2)
        _collect(binary, dilated)
        # 反相：處理黑字白底（dummy 測試）或白底黑字以外極性
        binary_inv = 255 - binary
        dilated_inv = cv2.dilate(binary_inv, kernel, iterations=2)
        _collect(binary_inv, dilated_inv)

        # --- 分支 B：白字黑框高門檻（白字亮度 ≈255，背景≈140-190）---
        # 對每個門檻獨立膨脹並收集，確保 180-210 範圍內的白字皆可捕獲；
        # 使用 15x5 大核將離散字元合併為行級 bbox，符合 _is_likely_gps_bbox 的
        # 寬度/高度比例檢查（避免字元級碎片被過濾）。
        kernel_white = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 5))
        for thresh in WHITE_TEXT_THRESHOLDS:
            wm = white_text_mask(gray, thresh)
            dilated_w = cv2.dilate(wm, kernel_white, iterations=1)
            _collect(wm, dilated_w)

        # --- 分支 C：HSV 白色遮罩（對偏黃/過曝白字補強，灰階門檻可能漏掉）---
        try:
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            hsv_white = cv2.inRange(hsv, np.array([0, 0, 200]), np.array([180, 40, 255]))
            dilated_hsv = cv2.dilate(hsv_white, kernel_white, iterations=1)
            _collect(hsv_white, dilated_hsv)
        except cv2.error:
            pass

        # --- 分支 D：黑框低門檻（白字黑框在亮天空下白核與天空同亮，200 閾值失效；
        # 黑框 0 與天空 200 對比極大，低門檻可穩定提取邊框，膨脹後還原行級 bbox）---
        try:
            # 對過曝/陰天亮天空特別有效（bg 200-210 時 white 分支全失效）
            border = black_border_mask(gray, BLACK_BORDER_THRESH)
            # 黑框本身為細線，需先膨脹再找輪廓；後續 _is_likely_gps_bbox 會過濾掉 A 柱等大黑塊（bh>0.12）
            dilated_border = cv2.dilate(border, kernel_white, iterations=1)
            _collect(border, dilated_border)
        except cv2.error:
            pass

        return all_bboxes

    @staticmethod
    def _is_gps_bbox(
        bbox: list[list[float]], frame_w: int, frame_h: int
    ) -> bool:
        """判斷 bounding box 是否可能含 GPS 座標

        僅使用位置/尺寸粗篩，不實際讀取文字。
        實際的文字驗證在 OCR 階段進行。
        """
        return _is_likely_gps_bbox(bbox, frame_w, frame_h)
