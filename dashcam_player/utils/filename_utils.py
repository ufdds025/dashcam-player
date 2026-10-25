"""Filename helpers for screenshot capture.

截圖檔名產生：嵌入來源影片可識別名稱（stem）+ 真實幀編號，
使檔名成為 (來源, 幀) 的確定性函式，從而：

  - 不同來源 → 不同檔名（不再跨來源撞名）
  - 同來源同幀重擷 → 同檔名（覆蓋，不產生「同名資料不同名檔」）
"""
from __future__ import annotations

import os
import re

_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t\x00-\x1f]')
_MULTI_UNDERSCORE = re.compile(r"_+")

MAX_STEM_LEN = 40


def sanitize_component(name: str, max_len: int = MAX_STEM_LEN) -> str:
    """淨化檔名元件：非法字元→底線、壓縮連續底線、去除頭尾空白/點、截斷。"""
    if not name:
        return "video"
    cleaned = _ILLEGAL.sub("_", name)
    cleaned = _MULTI_UNDERSCORE.sub("_", cleaned)
    cleaned = cleaned.strip(" ._")
    if not cleaned:
        return "video"
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(" ._")
    return cleaned


def source_stem(video_path: str) -> str:
    """來源影片 basename（去副檔名）經淨化後的識別名稱。"""
    if not video_path:
        return "video"
    base = os.path.splitext(os.path.basename(video_path))[0]
    return sanitize_component(base)


def normalize_ext(img_format: str) -> str:
    """JPEG→jpg，其餘→png。"""
    return "jpg" if img_format.upper() == "JPEG" else "png"


def build_capture_filename(
    video_path: str,
    frame_idx: int,
    prefix: str = "",
    img_format: str = "JPEG",
) -> str:
    """產出 ``{prefix}_{stem}_f{frame_idx:06d}.{ext}``（prefix 空白時省略前標）。"""
    stem = source_stem(video_path)
    core = f"{stem}_f{int(frame_idx):06d}"
    ext = normalize_ext(img_format)
    prefix_clean = sanitize_component(prefix) if prefix else ""
    if prefix_clean:
        return f"{prefix_clean}_{core}.{ext}"
    return f"{core}.{ext}"


def capture_glob(prefix: str, stem: str, ext: str) -> str:
    """回傳與 build_capture_filename 一致的衝突比對 glob 樣式。"""
    prefix_part = f"{sanitize_component(prefix)}_" if prefix else ""
    return f"{prefix_part}{stem}_f*.{ext}"
