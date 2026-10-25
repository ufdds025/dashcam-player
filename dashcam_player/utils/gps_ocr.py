"""GPS OCR 模組

從行車紀錄器影片畫面中 OCR 辨識 GPS 座標文字。
僅在 metadata / 外部檔案都無法偵測 GPS 時作為 fallback 使用。

使用 PaddleOCR 作為主要 OCR 引擎，辨識率遠優於 Tesseract。

支援的文字格式:
- 24.338535N 120.455958E 5Km/h
- 24.338535N, 120.455958E 5Km/h
- N24.338535 E120.455958
- 24.068061N120.358015E102Km/h  （PaddleOCR 連讀，無空格）
- 120.312459E 24.084121N        （PaddleOCR 行序顛倒）

使用方式:
    ocr = GPSOCR()
    point = ocr.ocr_frame(frame)  # 單幀辨識
    tracks = ocr.extract_tracks(video_path)  # 完整軌跡提取
"""

from __future__ import annotations

import re

import cv2
import numpy as np

from dashcam_player.utils.cv_utils import open_video_capture
from dashcam_player.utils.log import get_logger
from dashcam_player.utils.pts_loader import get_reliable_fps_count

log = get_logger()

# GPS 座標 regex: 24.338535N 120.455958E
# \s*[,，]?\s* 讓 N/S 與經度數字之間的逗號或空白可省略，
# 以處理 PaddleOCR 將經緯度連讀（24.068061N120.358015E）的情況。
_GPS_PATTERN = re.compile(
    r"(\d{1,3}\.\d{2,})\s*([NS])\s*[,，]?\s*(\d{1,3}\.\d{2,})\s*([EW])"
)
# 反向格式（PaddleOCR 行序顛倒：經度在前緯度在後）: 120.312459E 24.084121N
_GPS_REVERSED_PATTERN = re.compile(
    r"(\d{1,3}\.\d{2,})\s*([EW])\s*[,，]?\s*(\d{1,3}\.\d{2,})\s*([NS])"
)
# 前綴格式: N24.338535 E120.455958
_GPS_PREFIX_PATTERN = re.compile(
    r"([NS])\s*(\d{1,3}\.\d{2,})\s*[,，]?\s*([EW])\s*(\d{1,3}\.\d{2,})"
)

# PaddleOCR 偵測引擎對寬圖會降取樣（實測 2560 寬 crop 縮至 ~736 後文字 <30px），
# 導致 GPS 座標前導數字被裁掉（24.068061 → 4.068061）。限制 OCR 視窗寬度 ≤ 900px，
# 避免文字被縮小到偵測下限以下。
_MAX_OCR_WIDTH = 900


def _match_gps_text(text: str) -> tuple[float, float] | None:
    """從 OCR 文字解析 GPS 座標。

    支援格式:
      - 24.338535N 120.455958E          （標準）
      - 24.338535N, 120.455958E         （逗號分隔）
      - 24.338535N120.455958E           （PaddleOCR 連讀，無空格）
      - N24.338535 E120.455958          （前綴格式）
      - 120.312459E 24.084121N          （PaddleOCR 行序顛倒）

    S/W 為負號；超出 ±90/±180 回 None。
    """
    if not text:
        return None

    text = _clean_ocr_confusions(text)

    # 1) 標準順序：緯度在前、經度在後
    m = _GPS_PATTERN.search(text)
    if m is not None:
        return _parse_gps_match(m, reversed=False)

    # 2) 反向順序：PaddleOCR 有時把經度行排在緯度行前面
    m = _GPS_REVERSED_PATTERN.search(text)
    if m is not None:
        return _parse_gps_match(m, reversed=True)

    # 3) 前綴格式: N24.338535 E120.455958
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


def _parse_gps_match(
    m: re.Match[str], *, reversed: bool
) -> tuple[float, float] | None:
    """從 regex match 解析座標。reversed=True 時交換緯經組的群組位置。"""
    try:
        if not reversed:
            # groups: (lat_num, NS, lon_num, EW)
            lat = float(m.group(1))
            lat_dir = m.group(2).upper()
            lon = float(m.group(3))
            lon_dir = m.group(4).upper()
        else:
            # groups: (lon_num, EW, lat_num, NS) → 交換
            lon = float(m.group(1))
            lon_dir = m.group(2).upper()
            lat = float(m.group(3))
            lat_dir = m.group(4).upper()
    except ValueError:
        return None
    if lat_dir == "S":
        lat = -lat
    if lon_dir == "W":
        lon = -lon
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return (lat, lon)


# PaddleOCR 常見字元混淆（lang="en" 模型對白底小字的誤讀）
# 只對「含小數點的數字子串」做替換，N/S/E/W 半球字母一律保留。
_NUMERIC_TOKEN_RE = re.compile(r"\d*[\dOIlSBZ.][\dOIlSBZ.]*")
# O/l/I/B/Z/S 與數字高度混淆：O→0, l→1, I→1, B→8, Z→2, S→5
_OCR_CHAR_MAP = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "B": "8", "Z": "2"})


def _clean_ocr_confusions(text: str) -> str:
    """在數字子串內修正 PaddleOCR 常見字元混淆。

    例：OCR 讀出 ``24.068061N 120.358015E`` 時，若誤讀為
    ``24.06806lN 12O.358O15E``，此函式可還原為可解析格式。
    S 不直接轉 5（S 同時是南方半球字母，需由 regex 判斷語意位置）。
    """
    def _fix(match: re.Match[str]) -> str:
        tok = match.group(0)
        # 若 token 包含 N/S/E/W 字母則跳過（半球標記，不是數字）
        if re.search(r"[NSEWnsew]", tok):
            return tok
        return tok.translate(_OCR_CHAR_MAP)

    return _NUMERIC_TOKEN_RE.sub(_fix, text)


def _stabilize_ocr_tracks(points: list) -> list:
    """整數部分眾數修正：OCR 常截斷前導數字（24.068 → 4.068）。

    同一支影片的緯度/經度整數部分穩定（跨越國界/經度邊界在影片內幾乎不可能），
    以整數部分的眾數為準，修正截斷或誤讀造成的離群整數部分。
    例：4.068061 的整數部分 4 離眾數 24 太遠 → 還原為 24.068061。

    同時支援 dict（{"lat":…, "lon":…}，extract_tracks 的輸出）與
    帶 lat/lon 屬性的物件（GPSPoint），避免 gps_ocr↔gps_parser 循環 import。
    """
    if len(points) < 3:
        return points
    from collections import Counter

    def _lat(p):
        return p["lat"] if isinstance(p, dict) else p.lat

    def _lon(p):
        return p["lon"] if isinstance(p, dict) else p.lon

    def _set_lat(p, v):
        if isinstance(p, dict):
            p["lat"] = v
        else:
            p.lat = v

    def _set_lon(p, v):
        if isinstance(p, dict):
            p["lon"] = v
        else:
            p.lon = v

    lat_ints = Counter(int(_lat(p)) for p in points)
    lon_ints = Counter(int(_lon(p)) for p in points)
    if not lat_ints or not lon_ints:
        return points
    lat_mode = lat_ints.most_common(1)[0][0]
    lon_mode = lon_ints.most_common(1)[0][0]

    for p in points:
        li = int(_lat(p))
        loi = int(_lon(p))
        # 整數部分相差 >1 度（>100km）→ 截斷或誤讀，以眾數還原
        if abs(li - lat_mode) > 1:
            _set_lat(p, _lat(p) - li + lat_mode)
        if abs(loi - lon_mode) > 1:
            _set_lon(p, _lon(p) - loi + lon_mode)
    return points


def _cap_ocr_width(crop: np.ndarray, roi_x: float) -> np.ndarray:
    """限制 OCR crop 寬度，避免 PaddleOCR 降取樣把文字縮到偵測下限以下。

    實測（2560x1440 dashcam）：
    - crop 寬 ≤900px 時 PaddleOCR 可讀出 24.068061 的前導「2」
    - crop 寬 1350px（auto ROI）時被降取樣 → 文字縮小 → 讀成 4.068061/068061

    對過寬的 crop 只保留角落側 900px 視窗（GPS 文字位於角落，不影響座標）。
    """
    h, w = crop.shape[:2]
    if w <= _MAX_OCR_WIDTH:
        return crop
    # 左側角落（roi_x < 0.5）→ 保留左視窗；右側角落 → 保留右視窗
    if roi_x < 0.5:
        return crop[:, :_MAX_OCR_WIDTH]
    return crop[:, w - _MAX_OCR_WIDTH:]


_OCR_LOCK = __import__("threading").Lock()
_OCR_INSTANCE = None


def _get_paddle_ocr():
    """懶載入全域單例 PaddleOCR（執行緒安全，避免多執行緒同時載入/推論導致 Tensor 競態）"""
    global _OCR_INSTANCE
    with _OCR_LOCK:
        if _OCR_INSTANCE is not None:
            return _OCR_INSTANCE
        log.info("PaddleOCR 載入中...")
        from paddleocr import PaddleOCR

        _OCR_INSTANCE = PaddleOCR(use_textline_orientation=True, lang="en")
        log.info("PaddleOCR 載入完成")
        return _OCR_INSTANCE


class GPSOCR:
    """從影片畫面 OCR 辨識 GPS 座標 — PaddleOCR 版本

    優點：
    - 辨識率遠優於 Tesseract（白字黑框場景 100% vs 0%）
    - 內建文字偵測，無需手動前置處理
    - Python binding，無 subprocess overhead
    """

    def __init__(self, app_settings=None) -> None:
        self._ocr = None  # 懶載入
        self._app_settings = app_settings

    def _ensure_ocr(self):
        """確保 PaddleOCR 已初始化"""
        if self._ocr is None:
            self._ocr = _get_paddle_ocr()
        return self._ocr

    def ocr_frame(
        self,
        frame: np.ndarray,
        roi: tuple[float, float, float, float] | None = None,
        fast: bool = False,
        video_width: int = 0,
        video_height: int = 0,
        file_format: str = "",
    ) -> tuple[float, float] | None:
        """從單幀畫面辨識 GPS 座標，回傳 (lat, lon) 或 None

        Args:
            frame: 影片幀（BGR 格式，numpy ndarray）
            roi: 選用的 ROI (x, y, w, h) 相對座標 0~1
            fast: 快速模式（供 extract_tracks 批次處理）
            video_width: 影片寬度（用於持久化 ROI）
            video_height: 影片高度（用於持久化 ROI）
            file_format: 檔案格式（用於持久化 ROI）

        Returns:
            (lat, lon) 座標組，若辨識失敗回傳 None
        """
        if roi is not None:
            h, w = frame.shape[:2]
            # 若 ROI 過窄（<0.25 寬度）先擴至 0.32，避免「24」被切成「4」（日誌實測 0.205→切掉首位 2）
            rx, ry, rw, rh = roi
            if rw < 0.25 and rx < 0.02 and ry < 0.2:
                expand = 0.32 - rw
                rx = max(0.0, rx - expand * 0.2)  # 向左多留 20%
                rw = 0.32
                roi = (rx, ry, rw, rh)
                log.debug(f"GPS ROI 過窄自動擴寬: {roi}")
                # 持久化擴寬後的 ROI
                if self._app_settings and video_width > 0 and video_height > 0:
                    try:
                        from dashcam_player.models.gps_roi import GPSROISetting
                        new_setting = GPSROISetting(
                            resolution_w=video_width,
                            resolution_h=video_height,
                            file_format=file_format,
                            roi_x=rx,
                            roi_y=ry,
                            roi_w=rw,
                            roi_h=rh,
                            corner="top_left",
                            source="auto_expanded",
                            confidence=1.0,
                        )
                        self._app_settings.save_gps_roi(new_setting)
                        log.info(f"已持久化自動擴寬的 GPS ROI: {new_setting.key}")
                    except Exception as e:
                        log.warning(f"持久化擴寬 ROI 失敗: {e}")
            # 加少量 padding（擴寬後再算）
            x1 = int(roi[0] * w)
            y1 = int(roi[1] * h)
            x2 = int((roi[0] + roi[2]) * w)
            y2 = int((roi[1] + roi[3]) * h)
            pad_x = max(8, int((x2 - x1) * 0.06))
            pad_y = max(6, int((y2 - y1) * 0.10))
            x1 = max(0, x1 - pad_x)
            y1 = max(0, y1 - pad_y)
            x2 = min(w, x2 + pad_x)
            y2 = min(h, y2 + pad_y)
            if x2 <= x1 or y2 <= y1:
                return None
            crop = frame[y1:y2, x1:x2]
            crop = _cap_ocr_width(crop, roi[0])
            res = self._recognize(crop)
            if res is not None:
                return res
            # 左上角 A 柱備援：僅在首次 ROI 完全失敗且寬度足夠時才右移，且右移後寬度不變窄
            if roi[1] < 0.2 and roi[0] < 0.02 and roi[2] >= 0.30:
                shifted = (roi[0] + 0.015, roi[1], roi[2], roi[3])
                if shifted[0] + shifted[2] > 1.0:
                    shifted = (1.0 - shifted[2], shifted[1], shifted[2], shifted[3])
                sx1 = int(shifted[0] * w)
                sy1 = int(shifted[1] * h)
                sx2 = int((shifted[0] + shifted[2]) * w)
                sy2 = int((shifted[1] + shifted[3]) * h)
                pad_x2 = max(8, int((sx2 - sx1) * 0.06))
                pad_y2 = max(6, int((sy2 - sy1) * 0.10))
                sx1 = max(0, sx1 - pad_x2)
                sy1 = max(0, sy1 - pad_y2)
                sx2 = min(w, sx2 + pad_x2)
                sy2 = min(h, sy2 + pad_y2)
                if sx2 > sx1 and sy2 > sy1:
                    crop2 = frame[sy1:sy2, sx1:sx2]
                    res2 = self._recognize(crop2)
                    if res2 is not None:
                        log.info(f"PaddleOCR ROI 右移備援成功: {roi} -> {shifted}")
                        return res2
            # ROI 失敗，退回全畫面
            return self._recognize(frame)
        else:
            return self._recognize(frame)

    def _recognize(self, image: np.ndarray) -> tuple[float, float] | None:
        """使用 PaddleOCR 辨識影像中的 GPS 座標（加全域鎖避免併發 Tensor 錯誤）。

        多尺度策略（實測 2560x1440 dashcam 的 ~42px 小字）：
        - 寬 ROI crop 會被 PaddleOCR 降取樣，文字縮到偵測下限以下，
          前導「2」被裁掉（24.068061 → 4.068061）。因此先限制寬度再放大。
        - 窄 crop 直接放大 2x 讓文字夠大。
        以最高倍率且成功解析的結果優先（放大後資訊最完整）。
        """
        h, w = image.shape[:2]
        if h <= 0 or w <= 0:
            return None

        # 小圖放大策略：crop 高度 < 500（典型 ROI 高度）或寬度 < 800 時放大 2x
        upscale = h < 500 or w < 800
        scales = (2.0, 1.0) if upscale else (1.0,)

        for scale in scales:
            img = image
            if scale > 1.0:
                img = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            coord = self._recognize_single(img)
            if coord is not None:
                if scale > 1.0:
                    log.debug(f"PaddleOCR 小圖放大 {scale:.1f}x 辨識成功")
                return coord
        return None

    def _recognize_single(self, image: np.ndarray) -> tuple[float, float] | None:
        """單一尺度的 PaddleOCR 辨識（加全域鎖避免併發 Tensor 錯誤）"""
        try:
            ocr = self._ensure_ocr()
            with _OCR_LOCK:
                result = ocr.predict(image)
            if not result:
                return None

            full_text = ""
            for res in result:
                texts = res["rec_texts"] if "rec_texts" in res else []
                for text in texts:
                    full_text += " " + text

            if not full_text.strip():
                return None

            coord = _match_gps_text(full_text)
            if coord is not None:
                log.debug(f"PaddleOCR 辨識成功: {full_text.strip()[:60]}")
            return coord

        except Exception as e:
            log.warning(f"PaddleOCR 辨識例外: {e}")
            return None

    def extract_tracks(
        self,
        video_path: str,
        interval_sec: float = 1.0,
        roi: tuple[float, float, float, float] | None = None,
        video_width: int = 0,
        video_height: int = 0,
        file_format: str = "",
    ) -> list[dict]:
        """從影片均勻取樣提取 GPS 軌跡

        Args:
            video_path: 影片路徑
            interval_sec: 取樣間隔（秒），預設 1 秒
            roi: 選用的 ROI (x, y, w, h) 相對座標 0~1
            video_width: 影片寬度（用於持久化 ROI）
            video_height: 影片高度（用於持久化 ROI）
            file_format: 檔案格式（用於持久化 ROI）

        Returns:
            list of {"lat": float, "lon": float, "time": float}
        """
        cap = open_video_capture(video_path)
        if not cap.isOpened():
            log.error(f"無法開啟影片: {video_path}")
            return []

        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        # 部分 AVI 容器 FPS 中繼資料損毀（實測 30fps 影片回傳 2560 = 影片寬度；
        # 本檔宣稱 600fps/26477 幀，實際 5fps/223 幀），導致取樣間隔與時間戳全錯。
        # 優先以 ffprobe 的實際資料取代檔頭值。
        reliable = get_reliable_fps_count(video_path)
        if reliable is not None:
            fps, total_frames = reliable
        # ffprobe 不可用時的回退：以「幀數 + 首末幀時間差」重新推導
        elif fps <= 0 or fps > 120 or fps < 1:
            if total_frames > 1:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ret, _ = cap.read()
                if ret:
                    start_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
                    cap.set(cv2.CAP_PROP_POS_FRAMES, total_frames - 1)
                    ret, _ = cap.read()
                    if ret:
                        end_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
                        if end_ms > start_ms:
                            fps = (total_frames - 1) * 1000.0 / (end_ms - start_ms)
                            log.info(f"FPS 中繼資料損毀，改由時間戳推導: {fps:.1f} fps")
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
            f"取樣間隔 {interval_sec}s (每 {frame_interval} 幂)"
        )

        while frame_idx < total_frames:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ret, frame = cap.read()
            if not ret:
                break

            coord = self.ocr_frame(frame, roi=roi, fast=True,
                                   video_width=video_width, video_height=video_height, file_format=file_format)
            if coord is not None:
                lat, lon = coord
                if last_coord is not None:
                    if (
                        abs(lat - last_coord[0]) < 1e-6
                        and abs(lon - last_coord[1]) < 1e-6
                    ):
                        frame_idx += frame_interval
                        continue

                sec = frame_idx / fps
                tracks.append({"lat": lat, "lon": lon, "time": sec})
                last_coord = (lat, lon)
                log.debug(f"OCR @ {sec:.1f}s: {lat}, {lon}")

            frame_idx += frame_interval

        cap.release()
        tracks = _stabilize_ocr_tracks(tracks)
        log.info(f"OCR 取樣完成: {len(tracks)} 個軌跡點")
        return tracks

    @staticmethod
    def _parse_gps_text(text: str) -> tuple[float, float] | None:
        """從 OCR 文字中解析 GPS 座標（委派至模組級 _match_gps_text）"""
        return _match_gps_text(text)
