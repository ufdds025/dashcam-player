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
    open_video_capture,
    preprocess_clahe_otsu,
    sample_frame_indices,
)
from dashcam_player.utils.log import get_logger

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

        roi = _compute_union_roi(all_gps_bboxes, frame_w, frame_h)
        corner = _bbox_to_corner(all_gps_bboxes[0], frame_w, frame_h)
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
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        _, binary = preprocess_clahe_otsu(gray)

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        dilated = cv2.dilate(binary, kernel, iterations=2)

        contours, _ = cv2.findContours(
            dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        frame_h, frame_w = frame.shape[:2]
        bboxes: list[list[list[float]]] = []
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

            bboxes.append([
                [float(x), float(y)],
                [float(x + w), float(y)],
                [float(x + w), float(y + h)],
                [float(x), float(y + h)],
            ])

        return bboxes

    @staticmethod
    def _is_gps_bbox(
        bbox: list[list[float]], frame_w: int, frame_h: int
    ) -> bool:
        """判斷 bounding box 是否可能含 GPS 座標

        僅使用位置/尺寸粗篩，不實際讀取文字。
        實際的文字驗證在 OCR 階段進行。
        """
        return _is_likely_gps_bbox(bbox, frame_w, frame_h)
