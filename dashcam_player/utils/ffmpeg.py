from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from dashcam_player.models.capture_settings import SCALE_ORIGINAL
from dashcam_player.utils.log import get_logger
from dashcam_player.utils.subprocess_utils import run_no_window as _run_no_window

log = get_logger()


def win32_short_path(path: str) -> str:
    """Windows 8.3 短路徑（例：C:\\Users\\JUNHER~1\\... 而非完整中文名），OpenCV/FFmpeg ANSI API 相容用。
    非 Windows 回傳原值。

    重要：GetShortPathNameW 只能對「已存在」的路徑生效。若 path 指向尚未建立的檔案
    （例如截圖寫入前的目標路徑），直接整段轉換一定失敗、悄悄回退成原始路徑，
    導致路徑中的中文字元未被消除，造成 cv2.imwrite 在非 Unicode locale 下寫入失敗。
    因此只轉換「已存在的目錄部分」，檔名部分（程式自行組成，ASCII 安全）原樣拼回。

    注意：圖片寫入請改用 cv_utils.imwrite_safe()，使用 cv2.imencode + Python open()
    繞過 MinGW OpenCV 的 ANSI API 限制。本函數仍用於 cv2.VideoCapture 等不支援
    imencode 的場景。
    """
    if sys.platform != "win32":
        return path
    try:
        import ctypes

        dir_part, name_part = os.path.split(path)

        def _short(p: str) -> str:
            buf = ctypes.create_unicode_buffer(260)
            n = ctypes.windll.kernel32.GetShortPathNameW(p, buf, 260)
            if n == 0:
                err = ctypes.windll.kernel32.GetLastError()
                log.warning(f"GetShortPathNameW 失敗 (error={err}) for: {p}")
                return p
            return buf.value

        if dir_part and os.path.isdir(dir_part):
            short_dir = _short(dir_part)
            return os.path.join(short_dir, name_part)

        return _short(path)
    except Exception:
        return path


def _bundle_dir() -> str | None:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return str(sys._MEIPASS)
    return None


_FFMPEG_CACHE: tuple[str | None, str | None] | None = None


def find_ffmpeg() -> tuple[str | None, str | None]:
    """尋找 ffmpeg/ffprobe。僅快取正向結果——負結果每次重探，
    讓使用者中途安裝 FFmpeg 後無須重啟即可被找到（shutil.which 成本可忽略）。"""
    global _FFMPEG_CACHE
    if _FFMPEG_CACHE is not None:
        return _FFMPEG_CACHE
    bundle = _bundle_dir()
    if bundle:
        ffmpeg = os.path.join(bundle, "ffmpeg.exe")
        ffprobe = os.path.join(bundle, "ffprobe.exe")
        if os.path.isfile(ffmpeg) and os.path.isfile(ffprobe):
            _FFMPEG_CACHE = (ffmpeg, ffprobe)
            return _FFMPEG_CACHE
    path = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    probe_path = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if path and probe_path:
        _FFMPEG_CACHE = (path, probe_path)
    return path, probe_path


def _clear_ffmpeg_cache() -> None:
    """清除 find_ffmpeg 快取（僅供測試使用）。"""
    global _FFMPEG_CACHE
    _FFMPEG_CACHE = None


def ffprobe_json(
    video_path: str,
    ffprobe_path: str | None = None,
    extra_args: list[str] | None = None,
) -> dict | None:
    """呼叫 ffprobe 回傳 raw JSON dict（底層工具，不解析結果）。

    預設使用 ``-show_format -show_streams``；若需不同參數（例如
    ``-show_frames -select_streams d``），透過 ``extra_args`` 覆寫。
    """
    if ffprobe_path is None:
        _, ffprobe_path = find_ffmpeg()
    if ffprobe_path is None:
        return None

    cmd: list[str] = [
        ffprobe_path,
        "-v",
        "quiet",
        "-print_format",
        "json",
    ]
    if extra_args:
        cmd.extend(extra_args)
    else:
        cmd.extend(["-show_format", "-show_streams"])
    cmd.append(video_path)

    try:
        result = _run_no_window(cmd, timeout=10)
        if result.returncode != 0:
            return None
        return json.loads(result.stdout.decode("utf-8", errors="replace"))
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def probe_media_info(video_path: str, ffprobe_path: str | None = None) -> dict | None:
    if ffprobe_path is None:
        _, ffprobe_path = find_ffmpeg()
    if ffprobe_path is None:
        return None

    data = ffprobe_json(video_path, ffprobe_path)
    if data is None:
        return None

    info: dict = {
        "has_audio": False,
        "video_codec": "",
        "audio_codec": None,
        "duration": 0.0,
        "format_name": "",
    }

    for stream in data.get("streams", []):
        codec_type = stream.get("codec_type", "")
        if codec_type == "video" and not info["video_codec"]:
            info["video_codec"] = stream.get("codec_name", "")
        elif codec_type == "audio":
            info["has_audio"] = True
            info["audio_codec"] = stream.get("codec_name", "")

    fmt = data.get("format", {})
    info["duration"] = float(fmt.get("duration", 0))
    info["format_name"] = fmt.get("format_name", "")

    return info


_MP4_SAFE_CODECS = {
    "h264",
    "avc1",
    "hevc",
    "h265",
    "mpeg4",
    "mpeg2video",
    "mjpeg",
}


def suggest_container(video_codec: str, preferred: str = "MP4") -> str:
    """若 video_codec 與 preferred 容器不相容，回傳安全的 MKV 以免 stream copy 失敗被迫 re-encode。"""
    if preferred.upper() == "MP4" and video_codec.lower() not in _MP4_SAFE_CODECS:
        return "MKV"
    return preferred.upper()


_FORMAT_NAME_TO_EXT = {
    "mov": "mov",
    "mp4": "mp4",
    "m4a": "m4a",
    "3gp": "3gp",
    "matroska": "mkv",
    "webm": "webm",
    "avi": "avi",
    "mpegts": "ts",
    "mpeg": "mpg",
    "flv": "flv",
    "wmv": "wmv",
    "asf": "wmv",
}

_KNOWN_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".mov",
    ".avi",
    ".ts",
    ".mts",
    ".m2ts",
    ".flv",
    ".wmv",
    ".webm",
    ".mpg",
    ".mpeg",
    ".3gp",
    ".m4a",
}


def detect_input_format(video_path: str, format_name: str = "") -> str:
    """偵測輸入檔案原始格式，回傳副檔名字串（不含點）。

    優先使用檔案實際副檔名（若為已知容器），
    否則從 ffprobe format_name 推斷，最終回退 ``mp4``。
    """
    ext = os.path.splitext(video_path)[1].lower()
    if ext in _KNOWN_EXTENSIONS:
        return ext.lstrip(".")

    for name in format_name.split(","):
        name = name.strip().lower()
        if name in _FORMAT_NAME_TO_EXT:
            return _FORMAT_NAME_TO_EXT[name]

    return "mp4"


def get_keyframe_positions(video_path: str, ffprobe_path: str | None = None) -> list[float] | None:
    """取得影片所有關鍵影格的時間（秒），用於精確 seek 優化。

    回傳時間（秒）排序陣列；失敗或無 ffprobe 回傳 None。
    結果不快取（呼叫端應自行快取）。
    """
    if ffprobe_path is None:
        _, ffprobe_path = find_ffmpeg()
    if ffprobe_path is None:
        return None

    cmd = [
        ffprobe_path,
        "-v",
        "error",
        "-skip_frame",
        "nokey",  # 只輸出關鍵幀，長影片輸出量減少 10~100 倍
        "-select_streams",
        "v:0",
        "-show_entries",
        "frame=pts_time",
        "-of",
        "csv=p=0",
        video_path,
    ]
    try:
        result = _run_no_window(cmd, timeout=30)
        if result.returncode != 0:
            return None
        times: list[float] = []
        for line in result.stdout.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            # csv（skip_frame nokey 後只有 pts_time；相容舊格式 key_frame,pts_time）
            parts = line.split(",")
            try:
                times.append(float(parts[-1]))
            except ValueError:
                continue
        return sorted(times)
    except (subprocess.TimeoutExpired, OSError):
        return None


def build_export_cmd(
    ffmpeg_path: str,
    video_path: str,
    start_sec: float,
    end_sec: float,
    out_path: str,
    use_copy: bool = True,
    keep_audio: bool = True,
    has_audio: bool = False,
    codec: str = "libx264",
    crf: int = 18,
    preset: str = "medium",
    scale_width: str = SCALE_ORIGINAL,
    preserve_gps: bool = True,
) -> list[str]:
    """build FFmpeg export command.

    Args:
        preserve_gps: When True, map ``0:v`` / ``0:a?`` / ``0:d?`` to retain
            GPS data track while excluding subtitle streams. Defaults to True.
    """
    cmd = [ffmpeg_path, "-y"]
    cmd += ["-i", video_path]
    cmd += ["-ss", f"{start_sec:.3f}"]
    cmd += ["-t", f"{end_sec - start_sec:.3f}"]

    if use_copy:
        # 保留視訊 + 音訊 + 資料串流 (GPS data track 在資料串流)
        # 不使用 -map 0 以避免帶入 codec=none 的字幕串流
        if preserve_gps:
            cmd += ["-map", "0:v", "-map", "0:a?", "-map", "0:d?"]
        cmd += ["-c:v", "copy"]
    else:
        # 重新編碼時也保留 GPS data track
        if preserve_gps:
            cmd += ["-map", "0:v", "-map", "0:a?", "-map", "0:d?"]
        cmd += ["-c:v", codec, "-crf", str(crf), "-preset", preset]
        if scale_width != SCALE_ORIGINAL:
            try:
                w = int(scale_width)
                cmd += ["-vf", f"scale={w}:-2"]
            except ValueError:
                pass

    if has_audio and keep_audio:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-an"]

    cmd.append(out_path)
    return cmd
