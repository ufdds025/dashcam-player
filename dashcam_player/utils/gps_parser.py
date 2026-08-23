"""GPS 資料提取模組

從行車記錄器影片檔案中偵測並提取 GPS 軌跡資料。
支援的格式:
- 內嵌於影片檔案 (MOV/MP4 的 GPS metadata track)
- 外部 GPS 記錄檔 (.gpx, .srt, .nmea)

架構（A4 重構）:
    GPSParser            — 對外門面（facade），維持原有 API 不變
      ├─ GPSDetector     — 快速偵測是否有 GPS（外部/內嵌/選擇性 OCR）
      └─ GPSTrackExtractor — 多源 fallback 提取完整軌跡
    interpolate_gps()    — 純函數，供各層共用線性插值

使用方式:
    parser = GPSParser("video.mp4")
    if parser.has_gps_data():
        tracks = parser.extract_gps_tracks()
        lat, lon = parser.get_gps_at_time(10.5)  # 10.5秒時的座標
"""

from __future__ import annotations

import binascii
import bisect
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from dashcam_player.utils.cv_utils import open_video_capture, sample_frame_indices
from dashcam_player.utils.ffmpeg import ffprobe_json, find_ffmpeg
from dashcam_player.utils.gps_ocr import GPSOCR
from dashcam_player.utils.gps_roi_detector import GPSROIDetector
from dashcam_player.utils.log import get_logger

if TYPE_CHECKING:
    from dashcam_player.config.settings import AppSettings

log = get_logger()


@dataclass
class GPSPoint:
    """單一 GPS 座標點"""

    lat: float
    lon: float
    time: float  # 相對於影片開始的秒數
    altitude: float | None = None
    speed: float | None = None

    def to_dict(self) -> dict:
        return {
            "lat": self.lat,
            "lon": self.lon,
            "time": self.time,
            "altitude": self.altitude,
            "speed": self.speed,
        }


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """兩點間的大圓距離（公尺）"""
    import math

    R = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * R * math.asin(math.sqrt(a))


def filter_gps_drift(
    points: Sequence[GPSPoint], max_distance_m: float
) -> list[GPSPoint]:
    """過濾 GPS 飄移造成的異常點。

    規則：單點與前一點距離超過門檻，且其前後兩點彼此接近
    （代表該點跳離軌跡後又跳回原位）→ 視為飄移雜訊丟棄。
    連續大跳躍（真實快速移動）時前後兩點彼此仍遠，予以保留。
    端點（首/末點）一律保留。
    """
    if max_distance_m <= 0 or len(points) < 3:
        return list(points)
    result: list[GPSPoint] = []
    for i, p in enumerate(points):
        if 0 < i < len(points) - 1:
            prev = points[i - 1]
            nxt = points[i + 1]
            d_prev = _haversine_m(prev.lat, prev.lon, p.lat, p.lon)
            d_span = _haversine_m(prev.lat, prev.lon, nxt.lat, nxt.lon)
            if d_prev > max_distance_m and d_span <= max_distance_m:
                log.info(
                    f"GPS 飄移過濾: 丟棄異常點 {p.lat:.6f},{p.lon:.6f} @ {p.time:.1f}s"
                )
                continue
        result.append(p)
    return result


def _parse_iso_time(text: str) -> datetime | None:
    """解析 ISO 8601 時間字串（支援 Z / ±HH:MM 時區）

    對 naive datetime（無時區標記）強制設為 UTC，
    避免 .timestamp() 以系統本地時區解釋導致數小時誤差。
    """
    if not text:
        return None
    t = text.strip()
    try:
        t_norm = t.replace("Z", "+00:00")
        dt = datetime.fromisoformat(t_norm)
    except (ValueError, TypeError):
        try:
            dt = datetime.fromisoformat(t).replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# ==========================================================================
# 策略 1: 快速偵測（是否有 GPS）
# ==========================================================================


class GPSDetector:
    """快速偵測影片是否包含 GPS 資料。

    偵測順序：外部檔案 → 內嵌 metadata → (選擇性) OCR。
    OCR 為昂貴操作（啟動 Tesseract 需載入 tessdata），預設 skip。
    """

    def __init__(self, video_path: str, parser: GPSParser, skip_ocr: bool = True) -> None:
        self.video_path = video_path
        self._parser = parser  # 反向參考，用於呼叫 _has_ocr_gps
        self._skip_ocr = skip_ocr

    def detect(self) -> bool:
        if self._has_external_gps():
            return True
        if self._has_embedded_gps():
            return True
        if not self._skip_ocr and self._parser.has_ocr_gps():
            return True
        return False

    def _has_external_gps(self) -> bool:
        """檢查是否有同名的外部 GPS 檔案"""
        base = os.path.splitext(self.video_path)[0]
        for ext in [".gpx", ".nmea", ".srt"]:
            if os.path.isfile(base + ext):
                log.info(f"找到外部 GPS 檔案: {base + ext}")
                return True
        return False

    def _has_embedded_gps(self) -> bool:
        """使用 ffprobe 檢查影片內是否有 GPS/定位相關的 stream/metadata"""
        try:
            data = self._parser._probe_file()
            if not data:
                return False

            for stream in data.get("streams", []):
                tags = stream.get("tags", {})
                if stream.get("codec_type") == "data":
                    return True
                for key in tags.keys():
                    if any(k in key.lower() for k in ("gps", "location", "coordinate")):
                        return True

            fmt_tags = data.get("format", {}).get("tags", {})
            for key in fmt_tags.keys():
                if any(k in key.lower() for k in ("gps", "location", "coordinate")):
                    return True

        except Exception as e:
            log.warning(f"偵測 embedded GPS 時發生錯誤: {e}")

        return False


# ==========================================================================
# 策略 2: 多源 fallback 提取完整軌跡
# ==========================================================================


class GPSTrackExtractor:
    """從多個來源依序提取 GPS 軌跡（外部 → 內嵌 → OCR fallback）。

    純提取邏輯，不負責快取；快取由 GPSParser 管理。
    """

    def __init__(self, video_path: str, parser: GPSParser) -> None:
        self.video_path = video_path
        self._parser = parser

    def extract(self) -> list[GPSPoint]:
        tracks = self._extract_from_external()
        if not tracks:
            tracks = self._extract_from_embedded()
        if not tracks:
            tracks = self._extract_from_ocr()
        return tracks

    # ---- 外部檔案 ----

    def _extract_from_external(self) -> list[GPSPoint]:
        base = os.path.splitext(self.video_path)[0]

        gpx_path = base + ".gpx"
        if os.path.isfile(gpx_path):
            tracks, time_base = self._parser._parse_gpx(gpx_path)
            if time_base is not None:
                self._parser._time_base = time_base
            return tracks

        nmea_path = base + ".nmea"
        if os.path.isfile(nmea_path):
            return self._parser._parse_nmea(nmea_path)

        srt_path = base + ".srt"
        if os.path.isfile(srt_path):
            return self._parser._parse_srt(srt_path)

        return []

    # ---- 內嵌 metadata / data track ----

    def _extract_from_embedded(self) -> list[GPSPoint]:
        tracks: list[GPSPoint] = []

        data = self._parser._probe_file()
        if not data:
            return tracks

        tag_tracks = self._parser._extract_gps_from_tags(data)
        data_tracks = self._parser._extract_from_data_track()

        seen = set()
        all_tracks: list[GPSPoint] = []
        for pt in data_tracks + tag_tracks:
            key = (round(pt.lat, 6), round(pt.lon, 6), round(pt.time, 1))
            if key not in seen:
                seen.add(key)
                all_tracks.append(pt)
        all_tracks.sort(key=lambda p: p.time)
        return all_tracks

    # ---- OCR fallback ----

    def _extract_from_ocr(self) -> list[GPSPoint]:
        log.info("外部 GPS 檔案與內嵌 metadata 均無資料，啟動 Tesseract OCR 辨識...")
        if not self._parser._ocr:
            self._parser._ocr = GPSOCR()
        if self._parser._ocr_roi is not None:
            log.info(f"OCR 提取使用已設定 ROI: {self._parser._ocr_roi}")
        raw_tracks = self._parser._ocr.extract_tracks(
            self.video_path, interval_sec=1.0, roi=self._parser._ocr_roi
        )
        if not raw_tracks:
            return []

        points = [GPSPoint(lat=t["lat"], lon=t["lon"], time=t["time"]) for t in raw_tracks]
        log.info(f"Tesseract 提取 {len(points)} 個 GPS 軌跡點")
        return points


# ==========================================================================
# 門面（facade）— 對外 API 不變
# ==========================================================================


class GPSParser:
    """從影片檔案提取 GPS 軌跡資料（門面）。

    內部委派給 GPSDetector（快速偵測）與 GPSTrackExtractor（提取），
    對外維持原有 API，controller / gps_manager / batch_capture 皆不需修改。
    """

    def __init__(
        self,
        video_path: str,
        skip_ocr: bool = False,
        app_settings: AppSettings | None = None,
    ):
        self.video_path = video_path
        self._tracks: list[GPSPoint] = []
        self._has_gps: bool | None = None
        self._probe_data: dict | None = None
        self._time_base: float | None = None  # 絕對時間基準 (epoch seconds)
        self._ocr: GPSOCR | None = None
        self._skip_ocr: bool = (
            skip_ocr  # 若為 True，初始偵測跳過 OCR（延遲至 extract_gps_tracks 才執行）
        )
        self._extraction_attempted: bool = False  # 避免空結果重複掃描
        self._app_settings: AppSettings | None = app_settings
        self._roi_auto_failed: tuple[int, int, str] | None = None
        self._ocr_roi: tuple[float, float, float, float] | None = None
        self._video_width: int = 0
        self._video_height: int = 0
        self._file_format: str = ""

        self._detector = GPSDetector(video_path, self, skip_ocr=skip_ocr)
        self._extractor = GPSTrackExtractor(video_path, self)

    @property
    def tracks(self) -> list[GPSPoint]:
        return self._tracks

    @property
    def extraction_attempted(self) -> bool:
        return self._extraction_attempted

    def close_ocr(self) -> None:
        """釋放 OCR 資源（Tesseract 進程為無狀態，此處僅清理內部參照）"""
        if self._ocr:
            self._ocr = None

    # ------------------------------------------------------------------
    # 偵 測
    # ------------------------------------------------------------------

    def has_gps_data(self) -> bool:
        """偵測影片是否包含 GPS 資料 (快取結果)"""
        if self._has_gps is None:
            self._has_gps = self._detector.detect()
        return self._has_gps

    def has_ocr_gps(self) -> bool:
        """多幀輕量探測：嘗試影片前段多個取樣點

        流程：
          1. 取得影片解析度與格式
          2. 查 QSettings 是否有已知 ROI → 直接使用（0 次 readtext）
          3. 無已知 ROI → GPSROIDetector 自動偵測
          4. 自動偵測成功 → 儲存設定供後續使用
          5. 自動偵測失敗 → 記錄 _roi_auto_failed，供 UI 觸發手動設定
        """
        log.info("正在啟動文字辨識引擎 (Tesseract)...")
        caps: list = []
        try:
            import os

            import cv2

            cap = open_video_capture(self.video_path)
            if not cap.isOpened():
                log.warning("OCR 偵測：無法開啟影片")
                return False
            caps.append(cap)

            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fmt = os.path.splitext(self.video_path)[1]
            self._video_width = w
            self._video_height = h
            self._file_format = fmt

            # 步驟 1: 查已知 ROI 設定
            roi_setting = None
            if self._app_settings and w > 0 and h > 0:
                roi_setting = self._app_settings.load_gps_roi(w, h, fmt)
                if roi_setting:
                    log.info(f"找到已知 ROI 設定: {roi_setting.key}")
                    self._ocr_roi = (
                        roi_setting.roi_x, roi_setting.roi_y,
                        roi_setting.roi_w, roi_setting.roi_h,
                    )

            # 步驟 2: 取樣 3 幀測試
            sample_frames = sample_frame_indices(total_frames, fps)

            log.info(f"OCR 偵測：取樣 {len(sample_frames)} 幀 (frames {sample_frames})")
            ocr = GPSOCR()
            for idx in sample_frames:
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ret, frame = cap.read()
                if not ret or frame is None:
                    continue

                if roi_setting:
                    roi = (
                        roi_setting.roi_x, roi_setting.roi_y,
                        roi_setting.roi_w, roi_setting.roi_h,
                    )
                    coord = ocr.ocr_frame(frame, roi=roi)
                    if coord is None:
                        # ROI 失敗 → 全畫面重試（雙重策略），
                        # 成功則後續提取改用全畫面（_ocr_roi=None）
                        coord = ocr.ocr_frame(frame)
                        if coord is not None:
                            self._ocr_roi = None
                else:
                    coord = ocr.ocr_frame(frame)

                if coord is not None:
                    log.info(f"OCR 偵測到 GPS (frame {idx}): {coord[0]}, {coord[1]}")
                    self._ocr = ocr
                    return True

            # 步驟 3: 無已知 ROI 且取樣失敗 → 嘗試自動偵測
            if roi_setting is None and w > 0 and h > 0:
                log.info("無已知 ROI 且取樣失敗，啟動 GPS ROI 自動偵測...")
                detector = GPSROIDetector()
                result = detector.detect(self.video_path)
                if result is not None and self._app_settings:
                    from dashcam_player.models.gps_roi import GPSROISetting

                    new_setting = GPSROISetting(
                        resolution_w=result.video_width,
                        resolution_h=result.video_height,
                        file_format=result.file_format,
                        roi_x=result.detected_roi[0],
                        roi_y=result.detected_roi[1],
                        roi_w=result.detected_roi[2],
                        roi_h=result.detected_roi[3],
                        corner=result.detected_corner,
                        source="auto_detected",
                        confidence=result.confidence,
                    )
                    self._app_settings.save_gps_roi(new_setting)
                    log.info(f"GPS ROI 自動偵測成功並已儲存: {new_setting.key}")
                    self._ocr_roi = result.detected_roi
                    # 用新 ROI 再試一次
                    ocr2 = GPSOCR()
                    cap2 = open_video_capture(self.video_path)
                    if cap2.isOpened():
                        caps.append(cap2)
                        roi = result.detected_roi
                        for idx in sample_frames:
                            cap2.set(cv2.CAP_PROP_POS_FRAMES, idx)
                            ret2, frame2 = cap2.read()
                            if ret2 and frame2 is not None:
                                coord2 = ocr2.ocr_frame(frame2, roi=roi)
                                if coord2 is not None:
                                    log.info(
                                        f"ROI 偵測後 OCR 成功 (frame {idx}): {coord2[0]}, {coord2[1]}"
                                    )
                                    self._ocr = ocr2
                                    return True

            # 步驟 4: 全部失敗
            if w > 0 and h > 0:
                self._roi_auto_failed = (w, h, fmt)
            log.info("OCR 偵測：所有方法皆未辨識到 GPS 座標")
        except Exception as e:
            log.warning(f"OCR 偵測失敗: {e}")
        finally:
            # cv2 例外中途不再洩漏解碼器（舊版僅正常路徑 release）
            for c in caps:
                try:
                    c.release()
                except Exception:
                    pass
        return False

    # ------------------------------------------------------------------
    # 提取
    # ------------------------------------------------------------------

    def extract_gps_tracks(self) -> list[GPSPoint]:
        """提取所有 GPS 軌跡點

        若初始偵測時跳過了 OCR (skip_ocr=True)，但外部檔案與內嵌 metadata
        皆無 GPS，則在此時才執行 OCR — 確保 Tesseract 不被非必要載入。
        """
        if self._tracks:
            return self._tracks.copy()

        if self._extraction_attempted:
            return []

        # 如果初始偵測已嘗試所有來源（含 OCR）仍無 GPS，直接放棄
        if not self.has_gps_data() and not getattr(self, "_skip_ocr", False):
            self._extraction_attempted = True
            return []

        tracks = self._extractor.extract()
        self._extraction_attempted = True
        if tracks:
            self._has_gps = True
            self._tracks = sorted(tracks, key=lambda p: p.time)
            # GPS 飄移過濾（與檔案偵測位置差距過大的異常點丟棄）
            if self._app_settings is not None:
                enabled, max_d = self._app_settings.load_gps_filter()
                if enabled:
                    filtered = filter_gps_drift(self._tracks, max_d)
                    dropped = len(self._tracks) - len(filtered)
                    if dropped:
                        log.info(
                            f"GPS 飄移過濾: 移除 {dropped} 個異常點 "
                            f"(保留 {len(filtered)})"
                        )
                    self._tracks = filtered
            return self._tracks.copy()
        return []

    def _extract_gps_from_tags(self, data: dict) -> list[GPSPoint]:
        """從 ffprobe format/stream tags 中提取 GPS 座標"""
        fmt_tags = data.get("format", {}).get("tags", {})
        gps_data = self._parse_gps_tags(fmt_tags)
        if gps_data:
            return [GPSPoint(lat=gps_data[0], lon=gps_data[1], time=0.0)]

        for stream in data.get("streams", []):
            tags = stream.get("tags", {})
            gps_data = self._parse_gps_tags(tags)
            if gps_data:
                return [GPSPoint(lat=gps_data[0], lon=gps_data[1], time=0.0)]

        return []

    @staticmethod
    def _parse_gps_tags(tags: dict) -> tuple[float, float] | None:
        """從 tags dict 中找 GPS 座標（支援多種命名慣例）"""
        lat = lon = None

        for key, value in tags.items():
            kl = key.lower()
            try:
                sv = str(value).strip()
            except Exception:
                continue

            if kl in ("latitude", "lat", "gps_latitude", "gpslatitude"):
                try:
                    lat = float(sv)
                except ValueError:
                    pass
            elif kl in ("longitude", "lon", "lng", "gps_longitude", "gpslongitude"):
                try:
                    lon = float(sv)
                except ValueError:
                    pass
            elif any(k in kl for k in ("location", "gps", "coordinate", "position")):
                m = re.search(r"([\d.\-]+)\s*[,，]\s*([\d.\-]+)", sv)
                if m:
                    try:
                        v1 = float(m.group(1))
                        v2 = float(m.group(2))
                        if -90 <= v1 <= 90 and -180 <= v2 <= 180:
                            return (v1, v2)
                    except ValueError:
                        pass

        if lat is not None and lon is not None:
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return (lat, lon)
        return None

    def _extract_from_data_track(self) -> list[GPSPoint]:
        """從 data track 提取 GPS 資料（NMEA 等文字格式）"""
        _, ffprobe_path = find_ffmpeg()
        if not ffprobe_path:
            return []

        data = self._probe_file()
        if data is None:
            return []
        has_data_track = any(s.get("codec_type") == "data" for s in data.get("streams", []))
        if not has_data_track:
            return []

        try:
            data_frames = ffprobe_json(
                self.video_path,
                ffprobe_path,
                extra_args=["-show_frames", "-select_streams", "d", "-show_data"],
            )
            if data_frames is None:
                return []

            tracks = []

            for frame in data_frames.get("frames", []):
                raw_hex = frame.get("data", "")
                if not raw_hex:
                    continue

                pts_time = frame.get("pkt_pts_time")
                if pts_time is not None:
                    time_sec = float(pts_time)
                else:
                    dts_time = frame.get("pkt_dts_time")
                    if dts_time is not None:
                        time_sec = float(dts_time)
                    else:
                        log.warning("data track frame 無 pts/dts 時間資訊，跳過")
                        continue

                try:
                    text = binascii.unhexlify(raw_hex).decode("utf-8", errors="replace")
                except (binascii.Error, ValueError):
                    continue

                for line in text.strip().split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    point = self._parse_nmea_line(line)
                    if point:
                        point.time = time_sec
                        tracks.append(point)

            return tracks

        except Exception as e:
            log.warning(f"從 data track 提取 GPS 失敗: {e}")
            return []

    @staticmethod
    def _parse_nmea_line(line: str) -> GPSPoint | None:
        """解析單行 NMEA 句子，回傳 GPSPoint（time 不含，由呼叫者設定）"""
        if not (
            line.startswith("$GPGGA")
            or line.startswith("$GNGGA")
            or line.startswith("$GPRMC")
            or line.startswith("$GNRMC")
        ):
            return None

        parts = line.split(",")
        if len(parts) < 7:
            return None

        try:
            if line.startswith(("$GPGGA", "$GNGGA")) and len(parts) >= 10:
                if parts[6] == "0":
                    return None

                lat_deg = float(parts[2][:2])
                lat_min = float(parts[2][2:])
                lat = lat_deg + lat_min / 60.0
                if parts[3] == "S":
                    lat = -lat

                lon_deg = float(parts[4][:3])
                lon_min = float(parts[4][3:])
                lon = lon_deg + lon_min / 60.0
                if parts[5] == "W":
                    lon = -lon

                alt = float(parts[9]) if parts[9] else None
                return GPSPoint(lat=lat, lon=lon, time=0.0, altitude=alt)

            elif line.startswith(("$GPRMC", "$GNRMC")) and len(parts) >= 13:
                if parts[2] != "A":
                    return None

                lat_deg = float(parts[3][:2])
                lat_min = float(parts[3][2:])
                lat = lat_deg + lat_min / 60.0
                if parts[4] == "S":
                    lat = -lat

                lon_deg = float(parts[5][:3])
                lon_min = float(parts[5][3:])
                lon = lon_deg + lon_min / 60.0
                if parts[6] == "W":
                    lon = -lon

                speed = float(parts[7]) if parts[7] else None
                return GPSPoint(lat=lat, lon=lon, time=0.0, speed=speed)

        except (ValueError, IndexError):
            pass

        return None

    # ------------------------------------------------------------------
    # parse
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_gpx(gpx_path: str) -> tuple[list[GPSPoint], float | None]:
        """解析 GPX 檔案（支援 GPX 1.0 / 1.1，時間轉為相對秒數）"""
        tracks = []
        try:
            import xml.etree.ElementTree as ET

            tree = ET.parse(gpx_path)
            root = tree.getroot()

            tag = root.tag
            ns = ""
            if tag.startswith("{"):
                ns = tag[: tag.index("}") + 1]

            time_base: float | None = None

            for trkpt in root.iter(f"{ns}trkpt"):
                lat = float(trkpt.get("lat", 0))
                lon = float(trkpt.get("lon", 0))

                ele_elem = trkpt.find(f"{ns}ele")
                alt = float(ele_elem.text) if ele_elem is not None and ele_elem.text is not None else None

                time_elem = trkpt.find(f"{ns}time")
                if time_elem is not None and time_elem.text is not None:
                    dt = _parse_iso_time(time_elem.text)
                    if dt is None:
                        continue
                    abs_sec = dt.timestamp()
                    if time_base is None or abs_sec < time_base:
                        time_base = abs_sec
                    tracks.append(GPSPoint(lat=lat, lon=lon, time=abs_sec, altitude=alt))

            if not tracks and not ns:
                log.warning(f"GPX 解析失敗：無 namespace 且未找到 trkpt 元素（路徑: {gpx_path}）")

            if time_base is not None:
                for p in tracks:
                    p.time -= time_base
                return tracks, time_base

        except Exception as e:
            # 不變式：回傳的 tracks 必已相對化。部分解析的點帶絕對 epoch 時間，
            # 外流會被當相對秒數污染插值；回空列表讓呼叫端繼續走 fallback chain
            log.error(f"解析 GPX 失敗: {e}")
            return [], None

        return [], None

    @staticmethod
    def _parse_nmea(nmea_path: str) -> list[GPSPoint]:
        """解析 NMEA 檔案（時間轉為相對秒數）"""
        tracks: list[GPSPoint] = []
        raw_times: list[float] = []
        try:
            for enc in ("utf-8", "latin-1"):
                try:
                    with open(nmea_path, encoding=enc) as f:
                        for line in f:
                            ls = line.strip()
                            point = GPSParser._parse_nmea_line(ls)
                            if point is None:
                                continue
                            parts = ls.split(",")
                            time_str = None
                            if (
                                ls.startswith(("$GPRMC", "$GNRMC"))
                                and len(parts) >= 10
                                and parts[1]
                            ):
                                time_str = parts[1]
                            elif (
                                ls.startswith(("$GPGGA", "$GNGGA")) and len(parts) >= 2 and parts[1]
                            ):
                                time_str = parts[1]

                            if time_str:
                                try:
                                    h = int(time_str[:2])
                                    m = int(time_str[2:4])
                                    s = float(time_str[4:])
                                    abs_sec = h * 3600.0 + m * 60.0 + s
                                    raw_times.append(abs_sec)
                                    point.time = abs_sec
                                except (ValueError, IndexError):
                                    point.time = 0.0
                                    raw_times.append(0.0)
                            else:
                                point.time = 0.0
                                raw_times.append(0.0)

                            tracks.append(point)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                log.error(f"NMEA 檔案編碼無法識別: {nmea_path}")
                return []

        except Exception as e:
            log.error(f"解析 NMEA 失敗: {e}")

        if raw_times:
            cumulative_offset = 0.0
            offsets = [0.0] * len(raw_times)
            for i in range(1, len(raw_times)):
                if raw_times[i] < raw_times[i - 1] - 43200:
                    cumulative_offset += 86400.0
                offsets[i] = cumulative_offset

            for i in range(len(raw_times)):
                raw_times[i] += offsets[i]

            base = min(t for t in raw_times if t > 0) if any(t > 0 for t in raw_times) else 0.0
            for i, p in enumerate(tracks):
                if raw_times[i] > 0:
                    p.time = raw_times[i] - base

        return tracks

    @staticmethod
    def _parse_srt(srt_path: str) -> list[GPSPoint]:
        """解析 SRT (subtitle) 檔案中的 GPS 資料（含時間戳）"""
        tracks = []
        try:
            with open(srt_path, encoding="utf-8") as f:
                content = f.read()

            content = content.replace("\r\n", "\n").replace("\r", "\n")

            block_pattern = re.compile(
                r"(\d+)\s*\n"
                r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
                r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*\n"
                r"(.+?)(?=\n\n|\Z)",
                re.DOTALL,
            )
            gps_pattern = re.compile(r"GPS[\s:：]+([\d.\-]+)\s*[,，]\s*([\d.\-]+)")

            for block in block_pattern.finditer(content):
                text = block.group(10).strip()
                gps_match = gps_pattern.search(text)
                if not gps_match:
                    continue

                h, m, s, ms = (
                    int(block.group(2)),
                    int(block.group(3)),
                    int(block.group(4)),
                    int(block.group(5)),
                )
                start_sec = h * 3600.0 + m * 60.0 + s + ms / 1000.0
                lat = float(gps_match.group(1))
                lon = float(gps_match.group(2))
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    continue
                tracks.append(GPSPoint(lat=lat, lon=lon, time=start_sec))

        except Exception as e:
            log.error(f"解析 SRT 失敗: {e}")

        return tracks

    # ------------------------------------------------------------------
    # 查詢
    # ------------------------------------------------------------------

    def get_gps_at_time(self, seconds: float) -> tuple[float, float] | None:
        """取得指定時間點的 GPS 座標 (線性插值)

        使用 _tracks 直接讀取（跳過 extract_gps_tracks 的 .copy()），
        因為此方法僅讀取不作修改。_tracks 在首次 extract_gps_tracks 呼叫後被快取。
        """
        if not self._tracks:
            self.extract_gps_tracks()
        if not self._tracks:
            return None
        return interpolate_gps(seconds, self._tracks)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _probe_file(self) -> dict | None:
        """使用 ffprobe 取得影片資訊"""
        if self._probe_data is not None:
            return self._probe_data

        ffmpeg_path, ffprobe_path = find_ffmpeg()
        if ffprobe_path is None:
            return None

        data = ffprobe_json(self.video_path, ffprobe_path)
        if data is not None:
            self._probe_data = data
            return self._probe_data

        return None

    def to_gpx(self, output_path: str) -> bool:
        """將 GPS 軌跡匯出為 GPX 檔案（使用 ElementTree 產生 XML）"""
        if not self.has_gps_data():
            return False

        try:
            import xml.etree.ElementTree as ET

            tracks = self.extract_gps_tracks()

            NS = "http://www.topografix.com/GPX/1/1"
            ET.register_namespace("", NS)

            gpx = ET.Element(f"{{{NS}}}gpx", version="1.1")
            trk = ET.SubElement(gpx, f"{{{NS}}}trk")
            trkseg = ET.SubElement(trk, f"{{{NS}}}trkseg")

            for point in tracks:
                trkpt = ET.SubElement(
                    trkseg, f"{{{NS}}}trkpt", lat=str(point.lat), lon=str(point.lon)
                )
                if point.altitude is not None:
                    ele = ET.SubElement(trkpt, f"{{{NS}}}ele")
                    ele.text = str(point.altitude)
                if self._time_base is not None:
                    abs_ts = self._time_base + point.time
                    dt = datetime.fromtimestamp(abs_ts, tz=UTC)
                    time_str = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                    time_elem = ET.SubElement(trkpt, f"{{{NS}}}time")
                    time_elem.text = time_str

            tree = ET.ElementTree(gpx)
            tree.write(output_path, encoding="utf-8", xml_declaration=True)
            return True

        except Exception as e:
            log.error(f"匯出 GPX 失敗: {e}")
            return False


def interpolate_gps(
    seconds: float,
    points: Sequence[GPSPoint],
) -> tuple[float, float] | None:
    """對 GPSPoint 序列進行時間線性插值，取得指定秒數的 (lat, lon)

    使用 bisect 二分搜尋取代線性掃描，O(log n)。
    純函數 — 不依賴任何 parser 實例，可供 Controller / BatchCaptureWorker 等各層共用。
    """
    if not points:
        return None
    if seconds <= points[0].time:
        return (points[0].lat, points[0].lon)
    if seconds >= points[-1].time:
        return (points[-1].lat, points[-1].lon)

    idx = bisect.bisect_left(points, seconds, key=lambda p: p.time)
    if idx == 0:
        return (points[0].lat, points[0].lon)
    if idx >= len(points):
        return (points[-1].lat, points[-1].lon)

    p1, p2 = points[idx - 1], points[idx]
    if p1.time <= seconds <= p2.time:
        dt = p2.time - p1.time
        if dt < 1e-9:
            return (p1.lat, p1.lon)
        ratio = (seconds - p1.time) / dt
        lat = p1.lat + (p2.lat - p1.lat) * ratio
        lon = p1.lon + (p2.lon - p1.lon) * ratio
        return (lat, lon)
    return None
