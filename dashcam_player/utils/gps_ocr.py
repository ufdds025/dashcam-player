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

import os
import re
import tempfile

import cv2
import numpy as np

from dashcam_player.utils.cv_utils import open_video_capture, preprocess_clahe_otsu
from dashcam_player.utils.log import get_logger
from dashcam_player.utils.subprocess_utils import run_no_window as _run_no_window

log = get_logger()

# GPS 座標 regex: 24.338535N 120.455958E
# 支援格式:
#   24.338535N 120.455958E
#   24.338535N, 120.455958E
#   24.338535 N 120.455958 E
_GPS_PATTERN = re.compile(r"(\d{1,3}\.\d{2,})\s*([NS])\s*[,，\s]\s*(\d{1,3}\.\d{2,})\s*([EW])")
# 前綴格式: N24.338535 E120.455958（半球字母在前，部分機種使用）
_GPS_PREFIX_PATTERN = re.compile(
    r"([NS])\s*(\d{1,3}\.\d{2,})\s*[,，\s]\s*([EW])\s*(\d{1,3}\.\d{2,})"
)


def _match_gps_text(text: str) -> tuple[float, float] | None:
    """從 OCR 文字解析 GPS 座標（純函式，零 tesseract 依賴）。

    支援格式:
      - 24.338535N 120.455958E
      - 24.338535N, 120.455958E
      - N24.338535 E120.455958

    S/W 為負號；超出 ±90/±180 回 None。
    """
    if not text:
        return None

    m = _GPS_PATTERN.search(text)
    if m is not None:
        try:
            lat = float(m.group(1))
            lat_dir = m.group(2).upper()
            lon = float(m.group(3))
            lon_dir = m.group(4).upper()
        except ValueError:
            return None
    else:
        m = _GPS_PREFIX_PATTERN.search(text)
        if m is None:
            return None
        try:
            lat_dir = m.group(1).upper()
            lat = float(m.group(2))
            lon_dir = m.group(3).upper()
            lon = float(m.group(4))
        except ValueError:
            return None

    if lat_dir == "S":
        lat = -lat
    if lon_dir == "W":
        lon = -lon

    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    return (lat, lon)


def _run_tesseract(
    image: np.ndarray, config: str = "--psm 6"
) -> str | None:
    """使用 Tesseract 命令行進行 OCR 處理

    Args:
        image: 灰階 numpy 陣列
        config: Tesseract 組態字串，--psm 6 表示整體文字區塊

    Returns:
        辨識出的文字，失敗時回傳 None
    """
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="dashcam_ocr_")
        os.close(fd)

        success = cv2.imwrite(tmp_path, image)
        if not success:
            log.error("Tesseract：無法將影像寫入暫存檔案")
            return None

        cmd = ["tesseract", tmp_path, "stdout"] + config.split()
        try:
            result = _run_no_window(cmd, timeout=10)
        except FileNotFoundError:
            log.error("Tesseract 未安裝或不在 PATH 中")
            return None

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
            log.warning(f"Tesseract 執行失敗: {stderr.strip()}")
            return None

        stdout = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
        text = stdout.strip()
        return text if text else None
    except FileNotFoundError:
        log.error(
            "Tesseract 二進位檔未找到！請先安裝 Tesseract OCR 並確保已加入 PATH。"
        )
        return None
    except Exception as e:
        log.warning(f"Tesseract 執行例外: {e}")
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


class GPSOCR:
    """從影片畫面 OCR 辨識 GPS 座標 - Tesseract 版本

    優點：
    - 無 PyTorch / CUDA 依賴，安裝極輕量
    - .exe 封裝體積增加極小（僅需包含 tesseract 二進位檔）
    - 處理 30-60 分鐘影片的總時間仍在可接受範圍（~30-60 秒）
    - 任何作業系統（Windows/macOS/Linux）皆可運行

    缺點：
    - 僅支援標準印刷體文字，手寫體辨識能力較差
    - GPS 文字通常為印刷體，足夠使用
    """

    def ocr_frame(
        self, frame: np.ndarray, roi: tuple[float, float, float, float] | None = None
    ) -> tuple[float, float] | None:
        """從單幀畫面辨識 GPS 座標，回傳 (lat, lon) 或 None

        Args:
            frame: 影片幀（BGR 格式，numpy ndarray）
            roi: 選用的 ROI (x, y, w, h) 相對座標 0~1，
                 提供時裁切該區域後 readtext。

        Returns:
            (lat, lon) 座標組，若辨識失敗回傳 None
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        if roi is not None:
            x1 = int(roi[0] * gray.shape[1])
            y1 = int(roi[1] * gray.shape[0])
            x2 = int((roi[0] + roi[2]) * gray.shape[1])
            y2 = int((roi[1] + roi[3]) * gray.shape[0])
            # 對外擴充邊距，避免緊貼文字的 ROI 裁切到文字
            pad_x = max(4, int((x2 - x1) * 0.05))
            pad_y = max(4, int((y2 - y1) * 0.15))
            x1 = max(0, x1 - pad_x)
            y1 = max(0, y1 - pad_y)
            x2 = min(gray.shape[1], x2 + pad_x)
            y2 = min(gray.shape[0], y2 + pad_y)
            if x2 <= x1 or y2 <= y1:
                return None
            gray = gray[y1:y2, x1:x2].copy()

        return self._recognize(gray)

    def _recognize(self, gray: np.ndarray) -> tuple[float, float] | None:
        """對灰階影像做 OCR 辨識（多策略依序嘗試）

        順序：
          1. CLAHE 灰階直送（Tesseract 內部自適應二值化，效果最佳）
          2. Otsu 二值化
          3. Otsu 放大 2x/3x（對小字元有效）
        """
        enhanced, binary = preprocess_clahe_otsu(gray)

        # 策略 1: 灰階直送
        text = _run_tesseract(enhanced, config="--psm 6")
        if text:
            coord = self._parse_gps_text(text)
            if coord is not None:
                return coord

        # 策略 2: Otsu 二值化
        text2 = _run_tesseract(binary, config="--psm 6")
        if text2:
            coord = self._parse_gps_text(text2)
            if coord is not None:
                return coord
        text3 = _run_tesseract(binary, config="--psm 3")
        if text3:
            coord = self._parse_gps_text(text3)
            if coord is not None:
                return coord

        # 策略 3: 放大重試
        for scale in (2.0, 3.0):
            up = cv2.resize(
                binary, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR
            )
            text4 = _run_tesseract(up, config="--psm 6")
            if text4:
                coord = self._parse_gps_text(text4)
                if coord is not None:
                    return coord

        return None

    def extract_tracks(
        self,
        video_path: str,
        interval_sec: float = 1.0,
        roi: tuple[float, float, float, float] | None = None,
    ) -> list[dict]:
        """從影片均勻取樣提取 GPS 軌跡

        Args:
            video_path: 影片路徑
            interval_sec: 取樣間隔（秒），預設 1 秒
            roi: 選用的 ROI (x, y, w, h) 相對座標 0~1，
                 提供時僅對該區域做 OCR 辨識

        Returns:
            list of {"lat": float, "lon": float, "time": float}
        """
        cap = open_video_capture(video_path)
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

            coord = self.ocr_frame(frame, roi=roi)
            if coord is not None:
                lat, lon = coord
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

    @staticmethod
    def _parse_gps_text(text: str) -> tuple[float, float] | None:
        """從 OCR 文字中解析 GPS 座標（委派至模組級 _match_gps_text）"""
        return _match_gps_text(text)
