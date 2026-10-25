from __future__ import annotations

import os
from typing import TYPE_CHECKING

import cv2
import numpy as np

from dashcam_player.utils.ffmpeg import win32_short_path
from dashcam_player.utils.log import get_logger

log = get_logger()

if TYPE_CHECKING:
    from PyQt5.QtGui import QImage

    from dashcam_player.models.resolution import Resolution


def detect_hw_backends() -> list[tuple[str, str]]:
    backends: list[tuple[str, str]] = []
    try:
        if cv2.cuda.getCudaEnabledDeviceCount() > 0:
            info = cv2.cuda.getDeviceInfo(0)
            backends.append(("CUDA", info.name()))
    except (cv2.error, AttributeError):
        pass
    try:
        if cv2.ocl.haveOpenCL():
            cv2.ocl.setUseOpenCL(True)
            if cv2.ocl.useOpenCL():
                dev = cv2.ocl.Device.getDefault()
                backends.append(("OpenCL", dev.name()))
    except (cv2.error, AttributeError):
        pass
    return backends


def configure_hw_acceleration(cap: cv2.VideoCapture, hw_backends: list[tuple[str, str]]) -> None:
    """設定 OpenCV 硬體解碼加速。

    注意：OpenCV 的 CAP_PROP_HW_ACCELERATION 只對 **CUDA** 後端有意義
    （VIDEO_ACCELERATION_ANY 會自動選用可用的最佳後端，含 CUDA）。
    OpenCL 並不由 CAP_PROP_HW_ACCELERATION 控制，強設 flag 反而可能套用
    不適用的後端（如 MFX/D3D11）造成解碼異常，因此 OpenCL-only 時不設 flag。

    僅在偵測到 CUDA 時啟用加速，避免舊版對 OpenCL 誤設 CAP_PROP_HW_DEVICE=1 的問題。
    """
    if not hasattr(cv2, "CAP_PROP_HW_ACCELERATION"):
        return
    if hw_backends and "CUDA" in [name for name, _ in hw_backends]:
        if not hasattr(cv2, "VIDEO_ACCELERATION_ANY"):
            return
        try:
            cap.set(cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY)
            if hasattr(cv2, "CAP_PROP_HW_DEVICE"):
                cap.set(cv2.CAP_PROP_HW_DEVICE, 0)
        except cv2.error:
            log.warning("設定 CUDA 硬體加速失敗，退回軟體解碼")


def open_video_capture(
    path: str,
    hw_backends: list[tuple[str, str]] | None = None,
) -> cv2.VideoCapture:
    """開啟影片解碼器（CAP_FFMPEG），完整支援中文/Unicode 路徑。

    opencv-python >= 4.8 的 FFmpeg 後端原生接受 UTF-8 路徑（已實測驗證），
    因此優先直接以原始路徑開啟；僅在失敗時退回 8.3 短檔名
    （win32_short_path，相容舊版 OpenCV）。

    Args:
        path: 影片路徑（可含中文/空白/其他 Unicode 字元）
        hw_backends: detect_hw_backends() 的結果；僅 CUDA 時啟用硬體加速

    Returns:
        cv2.VideoCapture；若所有方式皆失敗，回傳未開啟的 cap（isOpened() == False），
        呼叫端需自行檢查 isOpened() 並視為失敗處理。
    """
    cap = cv2.VideoCapture(path, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        cap.release()
        cap = cv2.VideoCapture(win32_short_path(path), cv2.CAP_FFMPEG)
    if cap.isOpened() and hw_backends:
        configure_hw_acceleration(cap, hw_backends)
    return cap


_FOURCC_DECODE_CACHE: dict[int, str] = {}


def decode_fourcc(fourcc_val: int) -> str:
    if fourcc_val in _FOURCC_DECODE_CACHE:
        return _FOURCC_DECODE_CACHE[fourcc_val]
    result = (
        "".join(chr((fourcc_val >> 8 * i) & 0xFF) for i in range(4)) if fourcc_val else "unknown"
    )
    _FOURCC_DECODE_CACHE[fourcc_val] = result
    return result


def apply_resolution(frame: np.ndarray, res_text: str | Resolution) -> np.ndarray:
    """套用解析度縮放。

    Args:
        frame: 原始影像
        res_text: 解析度設定，可為 Resolution 列舉或字串

    Returns:
        縮放後的影像（若為原始解析度或縮小失敗則回傳原 frame）
    """
    from dashcam_player.models.resolution import Resolution

    res: Resolution
    if isinstance(res_text, Resolution):
        res = res_text
    else:
        res = Resolution.from_value(res_text)

    if res.is_original:
        return frame

    src_h, src_w = frame.shape[:2]
    parts = res.value.split("x")
    if len(parts) != 2:
        return frame

    try:
        target_w = max(1, int(parts[0]))
        target_h = max(1, int(parts[1]))
        scale = min(target_w / src_w, target_h / src_h)
        if scale >= 1.0:
            return frame
        new_w = max(1, int(src_w * scale))
        new_h = max(1, int(src_h * scale))
        return cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
    except (ValueError, IndexError, cv2.error, MemoryError):
        return frame


def imwrite_safe(path: str, frame: np.ndarray, params: list[int] | None = None) -> bool:
    """使用 cv2.imencode + Python open() 寫入圖片，繞過 MinGW OpenCV ANSI API 限制，支援中文路徑。"""
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"):
        ext = ".png"
    try:
        success, buf = cv2.imencode(ext, frame, params or [])
        if not success:
            log.warning(f"imencode 失敗: {path}")
            return False
        with open(path, "wb") as f:
            f.write(buf.tobytes())
        return True
    except Exception as e:
        log.warning(f"寫入圖片失敗 {path}: {e}")
        return False


def frame_to_qimage(frame_bgr: np.ndarray) -> QImage | None:
    if frame_bgr is None or frame_bgr.size == 0:
        return None
    h, w = frame_bgr.shape[:2]
    from PyQt5.QtGui import QImage

    return QImage(frame_bgr.data, w, h, w * 3, QImage.Format_BGR888).copy()


def preprocess_clahe_otsu(gray: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """CLAHE 增強 + Otsu 二值化（GPS OCR / ROI 偵測共用前處理）。

    回傳 ``(enhanced, binary)``：enhanced 為 CLAHE 灰階，binary 為 Otsu 二值化結果。
    clipLimit=1.0：較低的對比度限制，避免白字與亮天空在均衡化後亮度趨同。
    """
    clahe = cv2.createCLAHE(clipLimit=1.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    _, binary = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return enhanced, binary


# 白字黑框（white text with black stroke）專用高門檻隔離
# 白字亮度接近 255，黑框接近 0；在白底（天空/過曝）場景下 Otsu 會把白字與白底合併
# 導致文字熔入背景。高門檻（≈200）可精準隔離白字內核，再反相為黑字白底供 OCR
# 擴充至 180/220 以覆蓋陰天 180 與過曝 210 背景的邊界情況
WHITE_TEXT_THRESHOLDS: tuple[int, ...] = (200, 190, 210, 180, 220)
# 黑框隔離（白字黑框的另一半特徵）：用於白底過曝時白核與天空同亮無法分離，
# 此時黑框（0）仍與背景（200-210）有明顯對比
BLACK_BORDER_THRESH: int = 60


def white_text_mask(gray: np.ndarray, thresh: int = 200) -> np.ndarray:
    """高門檻二值化隔離白字（白字白底 → 白字黑底）。

    回傳白字為 255、背景為 0 的 mask。適用於白字黑框的 GPS 疊加文字。
    """
    _, mask = cv2.threshold(gray, thresh, 255, cv2.THRESH_BINARY)
    return mask


def white_text_inverted(gray: np.ndarray, thresh: int = 200) -> np.ndarray:
    """高門檻 + 反相（白字黑底 → 黑字白底），直接餵給 OCR。

    OCR 引擎偏好黑字白底；白字直接餵入辨識率極低，反相後大幅提升。
    """
    return 255 - white_text_mask(gray, thresh)


def adaptive_white_text_inverted(gray: np.ndarray, block_size: int = 31, C: int = -15) -> np.ndarray:
    """自適應閾值 + 反相，對混合亮/暗背景的白字特別有效。

    當 ROI 同時包含亮天空 (>180) 與暗樹木 (<80) 時，
    全域閾值無法兼顾兩區，自適應閾值以局部區塊計算閾值，
    可在每個區域分離白字與背景。
    回傳黑字白底（OCR 引擎偏好）。
    """
    # 自適應二值化：對亮區用較高閾值，暗區用較低閾值
    adaptive = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block_size, C
    )
    # adaptive 已是白字黑底（白字=255），反相為黑字白底
    return 255 - adaptive


def black_border_mask(gray: np.ndarray, thresh: int = 60) -> np.ndarray:
    """低門檻隔離黑框（黑框白底 → 白框黑底）。

    白字黑框在亮天空下，白核與天空同亮（200 vs 255）難以分離，
    但黑框（0）與天空（200）對比極大，低門檻可穩定提取黑框輪廓，
    供 ROI 偵測分支使用（膨脹後可還原文字行級 bbox）。
    """
    _, mask = cv2.threshold(gray, thresh, 255, cv2.THRESH_BINARY_INV)
    return mask


def sample_frame_indices(total_frames: int, fps: float) -> list[int]:
    """回傳 0s / 3s / 6s 對應的幀索引列表（GPS 偵測取樣共用）。"""
    indices = [0]
    if total_frames > int(fps * 3):
        indices.append(int(fps * 3))
    if total_frames > int(fps * 6):
        indices.append(int(fps * 6))
    return indices


_CN_FONT_CACHE: dict[int, object] = {}
_CN_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msjh.ttc",
    r"C:\Windows\Fonts\msjhbd.ttc",
    r"C:\Windows\Fonts\mingliu.ttc",
    r"C:\Windows\Fonts\NotoSansTC-Regular.otf",
    r"C:\Windows\Fonts\NotoSansCJKtc-Regular.ttf",
    r"/System/Library/Fonts/PingFang.ttc",
    r"/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


def _get_cn_font(size: int) -> object | None:  # type: ignore[no-untyped-def]
    """取得中文字型（PIL），自動遍歷系統候選，無則退回預設。"""
    if size in _CN_FONT_CACHE:
        return _CN_FONT_CACHE[size]
    try:
        from PIL import ImageFont
    except ImportError:
        return None
    for p in _CN_FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                f = ImageFont.truetype(p, size)  # type: ignore[assignment]
                _CN_FONT_CACHE[size] = f
                return f
            except Exception:
                continue
    try:
        f = ImageFont.load_default()  # type: ignore[assignment]
        _CN_FONT_CACHE[size] = f  # type: ignore[assignment]
        return f
    except Exception:
        return None


def put_text_cn(
    frame: np.ndarray,
    text: str,
    org: tuple[int, int],
    font_size: int = 20,
    color: tuple[int, int, int] = (255, 255, 255),
    stroke_color: tuple[int, int, int] | None = (0, 0, 0),
    stroke_width: int = 2,
) -> np.ndarray:
    """在 BGR frame 上繪製中文（PIL），失敗時退回 cv2.putText（僅 ASCII）。"""
    # 純 ASCII 且無中文字符時直接用 cv2（效能佳，無需 PIL 轉換）
    try:
        text.encode("ascii")
        is_ascii = True
    except UnicodeEncodeError:
        is_ascii = False
    if is_ascii:
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = font_size / 30.0
        thickness = max(1, font_size // 15)
        x, y = org
        if stroke_color is not None:
            cv2.putText(frame, text, (x, y), font, scale, stroke_color, thickness + 1, cv2.LINE_AA)
        cv2.putText(frame, text, (x, y), font, scale, color, thickness, cv2.LINE_AA)
        return frame
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)
        return frame
    pil_font = _get_cn_font(font_size)
    if pil_font is None:
        cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)
        return frame
    # BGR → RGB PIL
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    pil = Image.fromarray(rgb)
    draw = ImageDraw.Draw(pil)
    x, y = org
    # PIL 的 y 為 baseline 上方，需微調
    if stroke_color is not None:
        # stroke 外框
        draw.text((x, y), text, font=pil_font, fill=stroke_color, stroke_width=stroke_width, stroke_fill=stroke_color)  # type: ignore[arg-type]
    draw.text((x, y), text, font=pil_font, fill=color)  # type: ignore[arg-type]
    # RGB → BGR
    res = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    np.copyto(frame, res)
    return frame


def overlay_gps_text(frame: np.ndarray, gps_coord: tuple) -> np.ndarray:
    frame = frame.copy()
    lat, lon = gps_coord
    text = f"GPS: {lat:.6f}, {lon:.6f}"
    h, w = frame.shape[:2]
    # 先以 cv2 估算文字寬度以決定 tx，實際繪製走 put_text_cn（支援中文環境不亂碼）
    font = cv2.FONT_HERSHEY_SIMPLEX
    text_size = cv2.getTextSize(text, font, 0.6, 2)[0]
    tx = w - text_size[0] - 10
    ty = h - 15
    return put_text_cn(frame, text, (tx, ty), font_size=18, color=(0, 0, 0), stroke_color=(255, 255, 255), stroke_width=2)


def overlay_speed_text(frame: np.ndarray, speed_kmh: float, confidence: float = 0.0, is_vision: bool = False) -> np.ndarray:
    """在畫面右下角疊加速度文字（白字黑框，中文環境不亂碼）

    Args:
        is_vision: 是否為視覺測速（實驗功能），若為 True 會在速度後加上「（實驗）」
    """
    frame = frame.copy()
    text = f"{speed_kmh:.1f} km/h"
    if is_vision:
        text += "（實驗）"
    h, w = frame.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX
    text_size = cv2.getTextSize(text, font, 0.7, 2)[0]
    tx = w - text_size[0] - 10
    ty = h - 15
    return put_text_cn(frame, text, (tx, ty), font_size=22, color=(0, 0, 0), stroke_color=(255, 255, 255), stroke_width=2)


def overlay_osm_thumbnail(frame: np.ndarray, osm_bytes: bytes, thumb_size: int = 160) -> np.ndarray:
    try:
        arr = np.frombuffer(osm_bytes, dtype=np.uint8)
        thumb = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if thumb is None:
            return frame
        thumb = cv2.resize(thumb, (thumb_size, thumb_size))
        h, w = frame.shape[:2]
        x1, y1 = w - thumb_size - 8, h - thumb_size - 38
        if x1 < 0 or y1 < 0:
            return frame
        x2, y2 = x1 + thumb_size, y1 + thumb_size
        overlay = frame[y1:y2, x1:x2].astype(np.float32)
        blended = cv2.addWeighted(thumb.astype(np.float32), 0.9, overlay, 0.1, 0)
        frame[y1:y2, x1:x2] = blended.astype(np.uint8)
        return frame
    except Exception:
        return frame


def write_exif_gps(image_path: str, gps_coord: tuple) -> None:
    if not image_path.lower().endswith(".jpg"):
        return
    try:
        import piexif

        lat, lon = gps_coord

        def _to_dms(value: float) -> tuple:
            d = int(abs(value))
            m = int((abs(value) - d) * 60)
            s = int(((abs(value) - d) * 60 - m) * 10000)
            return ((d, 1), (m, 1), (s, 10000))

        lat_ref = "N" if lat >= 0 else "S"
        lon_ref = "E" if lon >= 0 else "W"

        gps_ifd = {
            piexif.GPSIFD.GPSLatitudeRef: lat_ref,
            piexif.GPSIFD.GPSLatitude: _to_dms(lat),
            piexif.GPSIFD.GPSLongitudeRef: lon_ref,
            piexif.GPSIFD.GPSLongitude: _to_dms(lon),
        }

        try:
            exif_dict = piexif.load(image_path)
        except Exception:
            exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}

        exif_dict["GPS"] = gps_ifd
        exif_bytes = piexif.dump(exif_dict)
        piexif.insert(exif_bytes, image_path)
    except ImportError:
        log.debug("piexif 未安裝，跳過 EXIF GPS 寫入")
    except Exception as e:
        get_logger().warning(f"寫入 EXIF GPS 失敗: {e}")
