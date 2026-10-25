"""AppSettings（QSettings 持久化）測試。

依賴 conftest.isolated_qsettings autouse fixture：所有 QSettings
導向每測獨立的暫存 INI，可自由讀寫不污染真實登錄檔。
重點覆蓋 A6（字串型別還原 int/float/bool）與損毀值 fallback。
"""

from __future__ import annotations

import pytest

from dashcam_player.config.settings import AppSettings
from dashcam_player.models.capture_settings import CaptureSettings
from dashcam_player.models.gps_roi import GPSROISetting


class TestCaptureSettingsRoundtrip:
    def test_defaults_roundtrip_types(self) -> None:
        """預設值 save→load 後型別必須正確（A6：INI 後端會回傳字串）。"""
        s = AppSettings()
        s.save_capture_settings(CaptureSettings())
        loaded = s.load_capture_settings()

        assert isinstance(loaded.fps, int) and loaded.fps == 30
        assert isinstance(loaded.jpeg_quality, int) and loaded.jpeg_quality == 95
        assert isinstance(loaded.video_crf, int) and loaded.video_crf == 18
        assert isinstance(loaded.gps_filter_max_distance, float)
        assert loaded.gps_filter_max_distance == pytest.approx(2000.0)
        assert isinstance(loaded.keep_audio, bool) and loaded.keep_audio is True
        assert isinstance(loaded.use_stream_copy, bool) and loaded.use_stream_copy is True

    def test_string_numerics_coerced(self) -> None:
        """QSettings 存成字串的數值必須還原為 int/float。"""
        s = AppSettings()
        s._store.setValue("capture/fps", "15")
        s._store.setValue("capture/jpeg_quality", "80")
        s._store.setValue("capture/video_crf", "23")
        s._store.setValue("capture/gps_filter_max_distance", "1500.5")
        s._store.sync()

        loaded = s.load_capture_settings()
        assert loaded.fps == 15 and isinstance(loaded.fps, int)
        assert loaded.jpeg_quality == 80
        assert loaded.video_crf == 23
        assert loaded.gps_filter_max_distance == pytest.approx(1500.5)

    def test_bool_strings_coerced(self) -> None:
        s = AppSettings()
        s._store.setValue("capture/use_stream_copy", "false")
        s._store.setValue("capture/keep_audio", "true")
        s._store.sync()

        loaded = s.load_capture_settings()
        assert loaded.use_stream_copy is False
        assert loaded.keep_audio is True

    def test_corrupted_numeric_falls_back_to_default(self) -> None:
        """損毀的字串數值略過該 key，改用 dataclass 預設值。"""
        s = AppSettings()
        s._store.setValue("capture/jpeg_quality", "not_a_number")
        s._store.setValue("capture/fps", "12.9.3")  # 無法解析
        s._store.sync()

        loaded = s.load_capture_settings()
        assert loaded.jpeg_quality == 95
        assert loaded.fps == 30

    def test_custom_values_roundtrip(self) -> None:
        s = AppSettings()
        custom = CaptureSettings(
            fps=10, prefix="dash", img_format="PNG",
            video_format="MKV", video_codec="libx265",
            show_gps_map=True,
        )
        s.save_capture_settings(custom)
        loaded = AppSettings().load_capture_settings()

        assert loaded.fps == 10
        assert loaded.prefix == "dash"
        assert loaded.img_format == "PNG"
        assert loaded.video_format == "MKV"
        assert loaded.video_codec == "libx265"
        assert loaded.show_gps_map is True


class TestGpsRoi:
    def _setting(self, **overrides: object) -> GPSROISetting:
        kwargs: dict = {
            "resolution_w": 1920,
            "resolution_h": 1080,
            "file_format": ".mp4",
            "roi_x": 0.05,
            "roi_y": 0.04,
            "roi_w": 0.45,
            "roi_h": 0.12,
            "corner": "top_left",
            "layout": "top_left",
            "source": "auto_detected",
            "confidence": 0.87,
            "timestamp": 1234567890.0,
        }
        kwargs.update(overrides)
        return GPSROISetting(**kwargs)

    def test_save_load_roundtrip(self) -> None:
        s = AppSettings()
        setting = self._setting()
        s.save_gps_roi(setting)

        loaded = s.load_gps_roi(1920, 1080, ".mp4")
        assert loaded is not None
        assert loaded.roi_x == pytest.approx(0.05)
        assert loaded.roi_y == pytest.approx(0.04)
        assert loaded.roi_w == pytest.approx(0.45)
        assert loaded.roi_h == pytest.approx(0.12)
        assert loaded.corner == "top_left"
        assert loaded.source == "auto_detected"
        assert loaded.confidence == pytest.approx(0.87)

    def test_load_missing_returns_none(self) -> None:
        assert AppSettings().load_gps_roi(999, 999, ".xyz") is None

    def test_get_all_gps_roi_keys_removed(self) -> None:
        """A7：壞死的 get_all_gps_roi_keys 已刪除，不可復活。"""
        assert not hasattr(AppSettings(), "get_all_gps_roi_keys")


class TestGpsFilter:
    def test_defaults(self) -> None:
        enabled, max_d = AppSettings().load_gps_filter()
        assert enabled is False
        assert max_d == pytest.approx(2000.0)

    def test_custom_values(self) -> None:
        s = AppSettings()
        s._store.setValue("capture/gps_filter_enabled", "true")
        s._store.setValue("capture/gps_filter_max_distance", "500.0")
        s._store.sync()

        enabled, max_d = s.load_gps_filter()
        assert enabled is True
        assert max_d == pytest.approx(500.0)

    def test_corrupted_distance_falls_back(self) -> None:
        s = AppSettings()
        s._store.setValue("capture/gps_filter_max_distance", "garbage")
        s._store.sync()
        _, max_d = s.load_gps_filter()
        assert max_d == pytest.approx(2000.0)

    def test_nonpositive_distance_falls_back(self) -> None:
        s = AppSettings()
        s._store.setValue("capture/gps_filter_max_distance", "-1")
        s._store.sync()
        _, max_d = s.load_gps_filter()
        assert max_d == pytest.approx(2000.0)


class TestCoerceByFieldType:
    """_coerce_by_field_type 邊界行為。"""

    @pytest.mark.parametrize("val,expected", [
        ("true", True), ("TRUE", True), ("1", True), ("yes", True),
        ("false", False), ("0", False), ("no", False), ("garbage", False),
    ])
    def test_bool_strings(self, val: str, expected: bool) -> None:
        from dashcam_player.config.settings import _coerce_by_field_type

        assert _coerce_by_field_type(val, "bool") is expected

    def test_int_from_float_value(self) -> None:
        from dashcam_player.config.settings import _coerce_by_field_type

        assert _coerce_by_field_type(95.0, "int") == 95

    def test_passthrough_unknown_type(self) -> None:
        from dashcam_player.config.settings import _coerce_by_field_type

        assert _coerce_by_field_type("MP4", "str") == "MP4"


class TestSpeedRoiHoodTop:
    """SpeedROI hood_top_y 存取（Phase C：跨機器 key 撞車比對用）。"""

    def test_roundtrip(self) -> None:
        from dashcam_player.models.speed_roi import SpeedROISetting

        s = AppSettings()
        s.save_speed_roi(SpeedROISetting(
            resolution_w=2560, resolution_h=1440, file_format="avi",
            roi_x=0.42, roi_y=0.55, roi_w=0.40, roi_h=0.10,
            hood_top_y=0.653,
        ))
        loaded = s.load_speed_roi(2560, 1440, "avi")
        assert loaded is not None
        assert loaded.hood_top_y == pytest.approx(0.653)

    def test_legacy_missing_key_returns_none(self) -> None:
        """舊存檔無 hood_top_y key → None，不影響舊框載入。"""
        from dashcam_player.models.speed_roi import SpeedROISetting

        s = AppSettings()
        s.save_speed_roi(SpeedROISetting(
            resolution_w=2560, resolution_h=1440, file_format="avi",
            roi_x=0.42, roi_y=0.55, roi_w=0.40, roi_h=0.10,
        ))
        # 模擬舊存檔：刪掉 hood key（save 時本來就不寫 None）
        s._store.remove("speed_roi/2560x1440_AVI/hood_top_y")
        loaded = s.load_speed_roi(2560, 1440, "avi")
        assert loaded is not None
        assert loaded.hood_top_y is None
        assert loaded.roi_x == pytest.approx(0.42)

    def test_corrupted_value_returns_none(self) -> None:
        s = AppSettings()
        from dashcam_player.models.speed_roi import SpeedROISetting

        s.save_speed_roi(SpeedROISetting(
            resolution_w=2560, resolution_h=1440, file_format="avi",
            roi_x=0.42, roi_y=0.55, roi_w=0.40, roi_h=0.10,
            hood_top_y=0.653,
        ))
        s._store.setValue("speed_roi/2560x1440_AVI/hood_top_y", "爛掉")
        loaded = s.load_speed_roi(2560, 1440, "avi")
        assert loaded is not None
        assert loaded.hood_top_y is None


class TestSpeedSettingsEnabled:
    """SpeedSettings.enabled 預設值與舊存檔回讀（R4 驗收）。"""

    def test_default_is_false(self) -> None:
        """SpeedSettings() 預設 enabled is False（省資源預設）。"""
        from dashcam_player.models.capture_settings import SpeedSettings

        settings = SpeedSettings()
        assert settings.enabled is False

    def test_legacy_true_roundtrip(self) -> None:
        """舊存檔 enabled=True 讀回仍為 True（QSettings bool 字串還原）。"""
        s = AppSettings()
        # 直接寫入舊版 key 模擬舊存檔
        s._store.setValue("capture/enabled", "true")
        s._store.sync()

        loaded = s.load_capture_settings()
        assert loaded.speed.enabled is True

    def test_false_roundtrip(self) -> None:
        """存 False 讀回仍為 False。"""
        s = AppSettings()
        s._store.setValue("capture/enabled", "false")
        s._store.sync()

        loaded = s.load_capture_settings()
        assert loaded.speed.enabled is False
