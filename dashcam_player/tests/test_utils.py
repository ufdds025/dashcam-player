from __future__ import annotations

import os
import time
from typing import Any

import pytest

from dashcam_player.utils.ffmpeg import (
    build_export_cmd,
    detect_input_format,
    get_keyframe_positions,
    suggest_container,
)
from dashcam_player.utils.gps_ocr import _match_gps_text
from dashcam_player.utils.time_utils import format_time, parse_time_input


class TestFormatTime:
    def test_zero(self) -> None:
        assert format_time(0) == "00:00:00"

    def test_positive(self) -> None:
        assert format_time(3661) == "01:01:01"

    def test_exact_hour(self) -> None:
        assert format_time(7200) == "02:00:00"

    def test_negative_clamps_to_zero(self) -> None:
        assert format_time(-5) == "00:00:00"


class TestParseTimeInput:
    def test_hh_mm_ss(self) -> None:
        assert parse_time_input("01:30:45") == 5445.0

    def test_mm_ss(self) -> None:
        assert parse_time_input("05:30") == 330.0

    def test_ss(self) -> None:
        assert parse_time_input("120") == 120.0

    def test_invalid(self) -> None:
        assert parse_time_input("abc") is None

    def test_fullwidth_colon(self) -> None:
        """全形冒號 U+FF1A 相容。"""
        assert parse_time_input("01：30：45") == 5445.0
        assert parse_time_input("05：30") == 330.0

    def test_fractional_seconds(self) -> None:
        assert parse_time_input("01:30:45.5") == 5445.5

    def test_out_of_range_fields_accepted_as_math(self) -> None:
        # 既有行為：不驗證欄位不驗證欄位範圍，純數學換算
        assert parse_time_input("00:99:99") == pytest.approx(99 * 60 + 99)


class TestGetKeyframePositions:
    def test_nonexistent_file_returns_none(self) -> None:
        # 不存在的檔案應安全回傳 None（不拋例外）
        assert get_keyframe_positions("__no_such_file__.mp4") is None

    def test_missing_ffprobe_returns_none(self, monkeypatch) -> None:
        # ffprobe 不存在時應回傳 None
        import dashcam_player.utils.ffmpeg as ffmpeg_mod
        monkeypatch.setattr(ffmpeg_mod, "find_ffmpeg", lambda: (None, None))
        assert get_keyframe_positions("dummy.mp4") is None


# ============================================================
# build_export_cmd
# ============================================================

_BASE: dict[str, Any] = {
    "ffmpeg_path": "ffmpeg",
    "video_path": "in.mp4",
    "start_sec": 1.0,
    "end_sec": 4.0,
    "out_path": "out.mp4",
}


def _arg_after(cmd: list[str], flag: str) -> str | None:
    if flag not in cmd:
        return None
    return cmd[cmd.index(flag) + 1]


class TestBuildExportCmd:
    def test_ss_and_t_durations(self) -> None:
        cmd = build_export_cmd(**_BASE)
        assert _arg_after(cmd, "-ss") == "1.000"
        assert _arg_after(cmd, "-t") == "3.000"
        assert cmd[-1] == "out.mp4"

    def test_stream_copy_preserve_gps_maps(self) -> None:
        cmd = build_export_cmd(**_BASE)
        maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
        assert maps == ["0:v", "0:a?", "0:d?"]
        assert _arg_after(cmd, "-c:v") == "copy"
        assert "-crf" not in cmd and "-preset" not in cmd

    def test_stream_copy_no_gps_no_maps(self) -> None:
        cmd = build_export_cmd(preserve_gps=False, **_BASE)
        assert "-map" not in cmd
        assert _arg_after(cmd, "-c:v") == "copy"

    def test_reencode_uses_codec_crf_preset(self) -> None:
        cmd = build_export_cmd(
            use_copy=False, codec="libx265", crf=22, preset="fast", **_BASE
        )
        assert _arg_after(cmd, "-c:v") == "libx265"
        assert _arg_after(cmd, "-crf") == "22"
        assert _arg_after(cmd, "-preset") == "fast"
        assert "-map" in cmd  # 重編碼也保留 GPS data track

    def test_reencode_scale_numeric(self) -> None:
        cmd = build_export_cmd(use_copy=False, scale_width="1280", **_BASE)
        assert _arg_after(cmd, "-vf") == "scale=1280:-2"

    def test_reencode_scale_original_no_vf(self) -> None:
        cmd = build_export_cmd(use_copy=False, scale_width="原始", **_BASE)
        assert "-vf" not in cmd

    def test_reencode_invalid_scale_ignored(self) -> None:
        cmd = build_export_cmd(use_copy=False, scale_width="not_a_number", **_BASE)
        assert "-vf" not in cmd

    def test_audio_copied_when_present_and_kept(self) -> None:
        cmd = build_export_cmd(has_audio=True, keep_audio=True, **_BASE)
        assert _arg_after(cmd, "-c:a") == "copy"

    def test_audio_dropped_when_disabled(self) -> None:
        cmd = build_export_cmd(has_audio=True, keep_audio=False, **_BASE)
        assert "-an" in cmd

    def test_audio_dropped_when_absent(self) -> None:
        cmd = build_export_cmd(has_audio=False, keep_audio=True, **_BASE)
        assert "-an" in cmd


class TestSuggestContainer:
    @pytest.mark.parametrize("codec", ["h264", "avc1", "hevc", "h265", "mpeg4"])
    def test_mp4_safe_codecs_stay_mp4(self, codec: str) -> None:
        assert suggest_container(codec, "MP4") == "MP4"

    @pytest.mark.parametrize("codec", ["ffv1", "prores", "unknown_codec"])
    def test_unsafe_codecs_become_mkv(self, codec: str) -> None:
        assert suggest_container(codec, "MP4") == "MKV"

    def test_case_insensitive_codec(self) -> None:
        assert suggest_container("H264", "MP4") == "MP4"

    def test_mkv_preferred_passthrough(self) -> None:
        assert suggest_container("ffv1", "MKV") == "MKV"


class TestDetectInputFormat:
    def test_known_extension_wins(self) -> None:
        assert detect_input_format("clip.mkv", "mov") == "mkv"

    def test_known_extension_case_insensitive(self) -> None:
        assert detect_input_format("CLIP.MP4") == "mp4"

    def test_falls_back_to_format_name(self) -> None:
        assert detect_input_format("clip.xyz", "mov,mp4,m4a") == "mov"

    def test_matroska_mapping(self) -> None:
        assert detect_input_format("clip.bin", "matroska,webm") == "mkv"

    def test_unknown_everything_defaults_mp4(self) -> None:
        assert detect_input_format("clip.bin", "") == "mp4"


class TestFindFfmpegCache:
    def setup_method(self) -> None:
        import dashcam_player.utils.ffmpeg as fm
        fm._clear_ffmpeg_cache()

    def teardown_method(self) -> None:
        import dashcam_player.utils.ffmpeg as fm
        fm._clear_ffmpeg_cache()

    def test_positive_result_cached(self, monkeypatch) -> None:
        import dashcam_player.utils.ffmpeg as fm

        calls = {"n": 0}

        def fake_which(name: str):
            calls["n"] += 1
            return f"C:/fake/{name}"

        monkeypatch.setattr(fm.shutil, "which", fake_which)
        r1 = fm.find_ffmpeg()
        r2 = fm.find_ffmpeg()
        assert r1 == ("C:/fake/ffmpeg", "C:/fake/ffprobe")
        assert r2 == r1
        assert calls["n"] == 2  # 第二次直接命中快取

    def test_negative_result_not_cached(self, monkeypatch) -> None:
        """A10：找不到時不可快取，使用者中途安裝 FFmpeg 後無須重啟。"""
        import dashcam_player.utils.ffmpeg as fm

        calls = {"n": 0}

        def fake_which(name: str):
            calls["n"] += 1
            return None

        monkeypatch.setattr(fm.shutil, "which", fake_which)
        assert fm.find_ffmpeg() == (None, None)
        assert fm.find_ffmpeg() == (None, None)
        # 每次 find_ffmpeg 對 ffmpeg/ffprobe 各試 2 個名稱 = 4 次 which；
        # 兩次呼叫共 8 次代表第二輪確實重新探測（負結果未快取）
        assert calls["n"] == 8


# ============================================================
# gps_ocr._match_gps_text（A11 純函式）
# ============================================================


class TestMatchGpsText:
    @pytest.mark.parametrize(
        "text,lat,lon",
        [
            ("24.338535N 120.455958E 5Km/h", 24.338535, 120.455958),
            ("24.338535N, 120.455958E", 24.338535, 120.455958),
            ("24.338535 N 120.455958 E", 24.338535, 120.455958),
            ("N24.338535 E120.455958", 24.338535, 120.455958),  # 前綴格式
            ("S24.338535 W120.455958", -24.338535, -120.455958),
            ("GPS: 12.50S , 70.25W speed", -12.5, -70.25),
            # PaddleOCR 連讀（無空格）
            ("24.068061N120.358015E102Km/h", 24.068061, 120.358015),
            ("24.084121N120.312459E98Km/h", 24.084121, 120.312459),
            # PaddleOCR 行序顛倒（經度在前緯度在後）
            ("120.312459E 24.084121N", 24.084121, 120.312459),
        ],
    )
    def test_valid_formats(self, text: str, lat: float, lon: float) -> None:
        result = _match_gps_text(text)
        assert result is not None
        assert result[0] == pytest.approx(lat)
        assert result[1] == pytest.approx(lon)

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "no coordinates here",
            "24.338535 120.455958",  # 缺半球字母
        ],
    )
    def test_invalid_returns_none(self, text: str) -> None:
        assert _match_gps_text(text) is None

    def test_latitude_out_of_range_rejected(self) -> None:
        assert _match_gps_text("91.5N 120.4E") is None

    def test_longitude_out_of_range_rejected(self) -> None:
        assert _match_gps_text("24.3N 181.2E") is None


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


class TestLinearFallback:
    """pts_loader._linear_fallback 交叉驗證邏輯"""

    def test_strategy_b_corrects_fps(self, monkeypatch) -> None:
        """r_frame_rate 錯誤但 nb_frames/format_duration 推算 fps 合理 → 策略 B"""
        import json
        from unittest.mock import MagicMock, patch

        from dashcam_player.utils.pts_loader import _linear_fallback

        # r_frame_rate=31, nb_frames=26477, format_duration=44.1
        # corrected_fps_b = 26477/44.1 = 600.4 → >120, 策略 B 被拒
        # 但改用 nb_frames=223, fps=5 時: corrected_fps_b = 223/44.1 = 5.06 → 合理
        fake_data = {
            "streams": [{"r_frame_rate": "31/1", "nb_frames": "223"}]
        }
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(fake_data).encode()

        with patch("dashcam_player.utils.pts_loader.subprocess.run", return_value=mock_result):
            result = _linear_fallback("dummy.avi", "/ffprobe", 10.0, format_duration=44.1)

        # fps 修正為 ~5.06，nb_frames 保持 223
        assert len(result.pts_list) == 223
        assert result.pts_list[1] - result.pts_list[0] == pytest.approx(1.0 / 5.06, rel=0.01)

    def test_strategy_a_corrects_nb_frames(self, monkeypatch) -> None:
        """nb_frames 錯誤但 format_duration * fps 推算合理 → 策略 B (fps 修正)"""
        import json
        from unittest.mock import MagicMock, patch

        from dashcam_player.utils.pts_loader import _linear_fallback

        # r_frame_rate=30, nb_frames=1800, format_duration=44.1
        # nb_frames=1800 < max_reasonable(5292) → 通過合理性檢查
        # reported_duration = 1800/30 = 60s vs actual 44.1s → >20% 差距
        # corrected_fps_b = 1800/44.1 = 40.8 → 在 1~120 範圍內，走 Strategy B
        fake_data = {
            "streams": [{"r_frame_rate": "30/1", "nb_frames": "1800"}]
        }
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(fake_data).encode()

        with patch("dashcam_player.utils.pts_loader.subprocess.run", return_value=mock_result):
            result = _linear_fallback("dummy.avi", "/ffprobe", 10.0, format_duration=44.1)

        # Strategy B 將 fps 修正為 1800/44.1 = 40.8，nb_frames 保持 1800
        assert len(result.pts_list) == 1800
        corrected_fps = 1800 / 44.1
        assert result.pts_list[1] - result.pts_list[0] == pytest.approx(1.0 / corrected_fps, rel=0.01)

    def test_both_wrong_returns_empty(self, monkeypatch) -> None:
        """r_frame_rate 與 nb_frames 皆錯誤，修正後仍不合理 → 回傳空結果"""
        import json
        from unittest.mock import MagicMock, patch

        from dashcam_player.utils.pts_loader import _linear_fallback

        # r_frame_rate=200/1, nb_frames=26477, format_duration=44.1
        # reported_duration = 26477/200 = 132.4s vs actual 44.1s → >20% ✓
        # corrected_fps_b = 26477/44.1 = 600.4 → >120, 拒絕 B
        # corrected_nb_a = int(44.1*200) = 8820, implied_fps = 8820/44.1 = 200 → >120, 拒絕 A
        fake_data = {
            "streams": [{"r_frame_rate": "200/1", "nb_frames": "26477"}]
        }
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(fake_data).encode()

        with patch("dashcam_player.utils.pts_loader.subprocess.run", return_value=mock_result):
            result = _linear_fallback("dummy.avi", "/ffprobe", 10.0, format_duration=44.1)

        assert result.pts_list == []
        assert result.quality == "UNSAFE"

