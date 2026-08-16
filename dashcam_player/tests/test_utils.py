from __future__ import annotations

import os
import time

from dashcam_player.utils.ffmpeg import get_keyframe_positions
from dashcam_player.utils.time_utils import format_time, parse_time_input


class TestFormatTime:
    def test_zero(self) -> None:
        assert format_time(0) == "00:00:00"

    def test_positive(self) -> None:
        assert format_time(3661) == "01:01:01"

    def test_exact_hour(self) -> None:
        assert format_time(7200) == "02:00:00"


class TestParseTimeInput:
    def test_hh_mm_ss(self) -> None:
        assert parse_time_input("01:30:45") == 5445.0

    def test_mm_ss(self) -> None:
        assert parse_time_input("05:30") == 330.0

    def test_ss(self) -> None:
        assert parse_time_input("120") == 120.0

    def test_invalid(self) -> None:
        assert parse_time_input("abc") is None


class TestGetKeyframePositions:
    def test_nonexistent_file_returns_none(self) -> None:
        # 不存在的檔案應安全回傳 None（不拋例外）
        assert get_keyframe_positions("__no_such_file__.mp4") is None

    def test_missing_ffprobe_returns_none(self, monkeypatch) -> None:
        # ffprobe 不存在時應回傳 None
        import dashcam_player.utils.ffmpeg as ffmpeg_mod
        monkeypatch.setattr(ffmpeg_mod, "find_ffmpeg", lambda: (None, None))
        assert get_keyframe_positions("dummy.mp4") is None


class TestOsmCacheCleanup:
    """cleanup_cache 的大小上限（_MAX_CACHE_SIZE_BYTES）與例外安全。"""

    def test_size_cap_removes_oldest(self, monkeypatch, tmp_path) -> None:
        import dashcam_player.utils.osm_map as osm

        monkeypatch.setattr(osm, "_MAX_CACHE_AGE_DAYS", 30)
        monkeypatch.setattr(osm, "_MAX_CACHE_FILES", 1000)  # 檔案數上限不觸發
        monkeypatch.setattr(osm, "_MAX_CACHE_SIZE_BYTES", 1000)  # 大小上限 1000 bytes
        monkeypatch.setattr(osm, "_TILE_CACHE_DIR", str(tmp_path))

        now = time.time()
        for i in range(3):  # 3 × 500 bytes = 1500 > 1000
            p = tmp_path / f"osm_{i}.png"
            p.write_bytes(b"x" * 500)
            os.utime(p, (now - 100 + i, now - 100 + i))  # 最舊 → 最新

        removed = osm.cleanup_cache()
        assert removed == 1
        remaining = sorted(p.name for p in tmp_path.iterdir())
        assert remaining == ["osm_1.png", "osm_2.png"]

    def test_under_limit_nothing_removed(self, monkeypatch, tmp_path) -> None:
        import dashcam_player.utils.osm_map as osm

        monkeypatch.setattr(osm, "_MAX_CACHE_AGE_DAYS", 30)
        monkeypatch.setattr(osm, "_MAX_CACHE_FILES", 1000)
        monkeypatch.setattr(osm, "_MAX_CACHE_SIZE_BYTES", 10000)
        monkeypatch.setattr(osm, "_TILE_CACHE_DIR", str(tmp_path))

        (tmp_path / "osm_0.png").write_bytes(b"x" * 500)
        assert osm.cleanup_cache() == 0
        assert (tmp_path / "osm_0.png").exists()

    def test_force_clears_all(self, monkeypatch, tmp_path) -> None:
        import dashcam_player.utils.osm_map as osm

        monkeypatch.setattr(osm, "_TILE_CACHE_DIR", str(tmp_path))
        (tmp_path / "osm_0.png").write_bytes(b"x")
        (tmp_path / "osm_1.png").write_bytes(b"x")
        assert osm.cleanup_cache(force=True) == 2
        assert list(tmp_path.iterdir()) == []

