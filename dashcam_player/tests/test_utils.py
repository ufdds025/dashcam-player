from __future__ import annotations

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

