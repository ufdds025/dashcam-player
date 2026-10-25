"""SpeedTrack 磁碟快取

避免重複開啟同一影片時重複分析
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from dashcam_player.models.speed import SpeedTrack
from dashcam_player.utils.log import get_logger

log = get_logger()


def _cache_key(video_path: str, settings_hash: str = "") -> str:
    """生成快取鍵：path_hash + size + mtime + settings_hash"""
    try:
        stat = os.stat(video_path)
        path_hash = hashlib.md5(video_path.encode()).hexdigest()[:16]
        key = f"{path_hash}-{stat.st_size}-{int(stat.st_mtime)}"
        if settings_hash:
            key += f"-{settings_hash[:8]}"
        return key
    except OSError:
        return hashlib.md5(video_path.encode()).hexdigest()[:16]


def _cache_path(key: str) -> Path:
    """快取檔案路徑"""
    cache_dir = Path(tempfile.gettempdir()) / "dashcam_speed_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{key}.speedtrack.json"


def _compute_settings_hash(settings: dict) -> str:
    """計算設定摘要"""
    import json
    content = json.dumps(settings, sort_keys=True)
    return hashlib.md5(content.encode()).hexdigest()


def load_speedtrack(video_path: str, settings: dict | None = None) -> SpeedTrack | None:
    """載入快取的 SpeedTrack

    Args:
        video_path: 影片路徑
        settings: 當前掃描設定 (用於驗證快取有效性)

    Returns:
        SpeedTrack 或 None (快取未命中/失效)
    """
    try:
        settings_hash = _compute_settings_hash(settings) if settings else ""
        key = _cache_key(video_path, settings_hash)
        path = _cache_path(key)

        if not path.exists():
            return None

        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        # 驗證快取內容
        if data.get("settings_hash") != settings_hash:
            log.info(f"SpeedTrack 快取設定不符，忽略: {video_path}")
            return None

        track = SpeedTrack.from_dict(data["track"])
        log.info(f"SpeedTrack 快取命中: {video_path} ({len(track.points)} points)")
        return track

    except (OSError, json.JSONDecodeError, KeyError) as e:
        log.warning(f"SpeedTrack 快取讀取失敗: {e}")
        return None


def save_speedtrack(video_path: str, track: SpeedTrack, settings: dict | None = None) -> bool:
    """儲存 SpeedTrack 到快取"""
    try:
        settings_hash = _compute_settings_hash(settings) if settings else ""
        key = _cache_key(video_path, settings_hash)
        path = _cache_path(key)

        data = {
            "settings_hash": _compute_settings_hash(settings) if settings else "",
            "track": track.to_dict(),
        }

        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

        log.info(f"SpeedTrack 快取已儲存: {video_path}")
        return True

    except OSError as e:
        log.warning(f"SpeedTrack 快取寫入失敗: {e}")
        return False


def clear_speed_cache() -> int:
    """清除所有速度快取

    Returns:
        刪除的檔案數
    """
    cache_dir = Path(tempfile.gettempdir()) / "dashcam_speed_cache"
    if not cache_dir.exists():
        return 0

    count = 0
    for f in cache_dir.glob("*.speedtrack.json"):
        try:
            f.unlink()
            count += 1
        except OSError:
            pass
    log.info(f"已清除 {count} 個 SpeedTrack 快取")
    return count


def get_cache_stats() -> dict:
    """取得快取統計資訊"""
    cache_dir = Path(tempfile.gettempdir()) / "dashcam_speed_cache"
    if not cache_dir.exists():
        return {"count": 0, "size_bytes": 0}

    count = 0
    size = 0
    for f in cache_dir.glob("*.speedtrack.json"):
        try:
            count += 1
            size += f.stat().st_size
        except OSError:
            pass
    return {"count": count, "size_bytes": size}
