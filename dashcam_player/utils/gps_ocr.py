"""GPS OCR 模組

從行車紀錄器影片畫面中 OCR 辨識 GPS 座標文字。
僅在 metadata / 外部檔案都無法偵測 GPS 時作為 fallback 使用。

支援的文字格式:
- 24.338535N 120.455958E 5Km/h
- 24.338535N, 120.455958E 5Km/h
- N24.338535 E120.455958

使用方式:
    ocr = GPSOCR()
    point = ocr.ocr_frame(frame)  # 單幀辨識
    tracks = ocr.extract_tracks(video_path)  # 完整軌跡提取
"""

from __future__ import annotations

import re
from typing import Any

import cv2
import numpy as np

from dashcam_player.utils.ffmpeg import win32_short_path
from dashcam_player.utils.log import get_logger

log = get_logger()

# GPS 座標 regex: 24.338535N 120.455958E
# 支援格式:
#   24.338535N 120.455958E
#   24.338535N, 120.455958E
#   24.338535 N 120.455958 E
_GPS_PATTERN = re.compile(r"(\d{1,3}\.\d{2,})\s*([NS])\s*[,，\s]\s*(\d{1,3}\.\d{2,})\s*([EW])")


class GPSOCR:
    """從影片畫面 OCR 辨識 GPS 座標

    EasyOCR reader 為模組層級單例，同一次執行期間所有影片共用，
    避免每次開新影片都重新載入（2-10 秒）。
    """

    _shared_reader = None  # 模組層級單例，跨影片重用

    def __init__(self) -> None:
        self._reader = None

    @classmethod
    def close_shared_reader(cls) -> None:
        """釋放共享 EasyOCR reader（程式結束時呼叫）"""
        cls._shared_reader = None

    def _get_reader(self):
        """延遲初始化 EasyOCR reader（共享單例）"""
        # 優先使用實例級 reader（測試注入用），否則用共享單例
        if self._reader is not None:
            return self._reader
        if GPSOCR._shared_reader is not None:
            self._reader = GPSOCR._shared_reader
            return self._reader
        try:
            import easyocr

            gpu = self._detect_cuda()
            GPSOCR._shared_reader = easyocr.Reader(["en"], gpu=gpu)
            self._reader = GPSOCR._shared_reader
            log.info(f"EasyOCR reader 已初始化 (GPU={'啟用' if gpu else '停用'})")
        except ImportError:
            log.error("easyocr 未安裝，請執行: pip install easyocr")
            return None
        except Exception as e:
            log.error(f"EasyOCR 初始化失敗: {e}")
            return None
        return self._reader

    @staticmethod
    def _detect_cuda() -> bool:
        """偵測 CUDA 是否可用"""
        try:
            import torch

            return torch.cuda.is_available()
        except ImportError:
            return False

    def ocr_frame(
        self, frame: np.ndarray, roi: tuple[float, float, float, float] | None = None
    ) -> tuple[float, float] | None:
        """從單幀畫面辨識 GPS 座標，回傳 (lat, lon) 或 None

        Args:
            frame: 影片幀（BGR）
            roi: 選用的 ROI (x, y, w, h) 相對座標 0~1，
                 提供時跳過 4 角落掃描，直接裁切該區域後 readtext。
        """
        reader: Any = self._get_reader()
        if reader is None:
            return None

        if roi is not None:
            return self._ocr_with_roi(frame, reader, roi)

        # 策略 1: 依序嘗試 4 個角落裁切區域（較快）
        rois = self._crop_all_corners(frame)
        for corner_roi in rois:
            if corner_roi is None:
                continue
            # 嘗試多種預處理方式
            for processed in self._preprocess_multi(corner_roi):
                results = reader.readtext(processed)
                for _, text, _conf in results:
                    coord = self._parse_gps_text(text)
                    if coord is not None:
                        return coord
                combined = " ".join(t for _, t, _ in results)
                coord = self._parse_gps_text(combined)
                if coord is not None:
                    return coord

        # 策略 2: 全畫面 OCR（較慢但較準確）
        results = reader.readtext(frame)
        for _, text, _conf in results:
            coord = self._parse_gps_text(text)
            if coord is not None:
                return coord
        combined = " ".join(t for _, t, _ in results)
        return self._parse_gps_text(combined)

    def _ocr_with_roi(
        self,
        frame: np.ndarray,
        reader: Any,
        roi: tuple[float, float, float, float],
    ) -> tuple[float, float] | None:
        """使用已知 ROI 裁切後直接 readtext（0-1 次呼叫）"""
        h, w = frame.shape[:2]
        x1 = int(roi[0] * w)
        y1 = int(roi[1] * h)
        x2 = int((roi[0] + roi[2]) * w)
        y2 = int((roi[1] + roi[3]) * h)
        x1 = max(0, x1)
        y1 = max(0, y1)
        x2 = min(w, x2)
        y2 = min(h, y2)
        if x2 <= x1 or y2 <= y1:
            return None

        cropped = frame[y1:y2, x1:x2].copy()
        gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY)
        scaled = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        _, binary = cv2.threshold(scaled, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        results = reader.readtext(binary)
        for _, text, _conf in results:
            coord = self._parse_gps_text(text)
            if coord is not None:
                return coord
        combined = " ".join(t for _, t, _ in results)
        return self._parse_gps_text(combined)

    def extract_tracks(self, video_path: str, interval_sec: float = 1.0) -> list[dict]:
        """從影片均勻取樣提取 GPS 軌跡

        Args:
            video_path: 影片路徑
            interval_sec: 取樣間隔（秒），預設 1 秒

        Returns:
            list of {"lat": float, "lon": float, "time": float}
        """
        cap = cv2.VideoCapture(win32_short_path(video_path))
        if not cap.isOpened():
            log.error(f"無法開啟影片: {video_path}")
            return []

        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if fps <= 0 or total_frames <= 0:
            cap.release()
            return []

        duration = total_frames / fps
        frame_interval = int(fps * interval_sec)
        if frame_interval < 1:
            frame_interval = 1

        tracks: list[dict] = []
        last_coord: tuple[float, float] | None = None
        frame_idx = 0

        log.info(
            f"OCR 取樣開始: 影片長度 {duration:.1f}s, "
            f"取樣間隔 {interval_sec}s (每 {frame_interval} 幀)"
        )

        while frame_idx < total_frames:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ret, frame = cap.read()
            if not ret:
                break

            coord = self.ocr_frame(frame)
            if coord is not None:
                lat, lon = coord
                # 去重：與上一個點相同則跳過
                if last_coord is not None:
                    if abs(lat - last_coord[0]) < 1e-6 and abs(lon - last_coord[1]) < 1e-6:
                        frame_idx += frame_interval
                        continue

                sec = frame_idx / fps
                tracks.append({"lat": lat, "lon": lon, "time": sec})
                last_coord = (lat, lon)
                log.debug(f"OCR @ {sec:.1f}s: {lat}, {lon}")

            frame_idx += frame_interval

        cap.release()
        log.info(f"OCR 取樣完成: {len(tracks)} 個軌跡點")
        return tracks

    def _crop_all_corners(self, frame: np.ndarray) -> list[np.ndarray | None]:
        """裁切 4 個角落的 GPS 文字區域

        行車紀錄器 GPS 文字通常出現在:
        - 左上角（最常見）
        - 右上角
        - 左下角
        - 右下角

        回傳: [左上, 右上, 左下, 右下] 的裁切結果列表
        """
        h, w = frame.shape[:2]
        if h < 20 or w < 20:
            return []

        # 裁切比例：高度 15%、寬度 50%（GPS 文字通常跨多行）
        crop_h = int(h * 0.15)
        crop_w = int(w * 0.50)

        # 左上角
        top_left = frame[0:crop_h, 0:crop_w].copy()
        # 右上角
        top_right = frame[0:crop_h, w - crop_w : w].copy()
        # 左下角
        bottom_left = frame[h - crop_h : h, 0:crop_w].copy()
        # 右下角
        bottom_right = frame[h - crop_h : h, w - crop_w : w].copy()

        return [top_left, top_right, bottom_left, bottom_right]

    def _preprocess_multi(self, roi: np.ndarray) -> list[np.ndarray]:
        """多種預處理方式，提升不同字體/背景的辨識率

        回傳: 多種預處理結果的列表
        """
        results = []

        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        scaled = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)

        # 方式 1: OTSU 二值化（標準）
        _, binary_otsu = cv2.threshold(scaled, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        results.append(binary_otsu)

        # 方式 2: 反轉 + OTSU（處理白字黑底）
        _, binary_inv = cv2.threshold(scaled, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        results.append(binary_inv)

        # 方式 3: 自適應二值化（處理不均勻照明）
        adaptive = cv2.adaptiveThreshold(
            scaled, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
        )
        results.append(adaptive)

        # 方式 4: CLAHE 對比增強 → OTSU（白天低對比度場景）
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(scaled)
        _, binary_clahe = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        results.append(binary_clahe)

        # 方式 5: CLAHE → 反轉（白天白字場景）
        _, binary_clahe_inv = cv2.threshold(
            enhanced, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
        )
        results.append(binary_clahe_inv)

        # 方式 6: 邊緣偵測（Canny）→ 膨脹（強化文字輪廓）
        edges = cv2.Canny(scaled, 50, 150)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        dilated = cv2.dilate(edges, kernel, iterations=1)
        results.append(dilated)

        return results

    @staticmethod
    def _parse_gps_text(text: str) -> tuple[float, float] | None:
        """從 OCR 文字中解析 GPS 座標"""
        if not text:
            return None

        m = _GPS_PATTERN.search(text)
        if m is None:
            return None

        try:
            lat = float(m.group(1))
            lat_dir = m.group(2).upper()
            lon = float(m.group(3))
            lon_dir = m.group(4).upper()

            if lat_dir == "S":
                lat = -lat
            if lon_dir == "W":
                lon = -lon

            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                return None

            return (lat, lon)
        except (ValueError, IndexError):
            return None
