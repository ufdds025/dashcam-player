from __future__ import annotations

import cv2
import numpy as np
import pytest

from dashcam_player.utils.cv_utils import (
    apply_resolution,
    decode_fourcc,
    format_speed_label,
    imwrite_safe,
    open_video_capture,
    overlay_gps_text,
)


class TestDecodeFourcc:
    def test_known_codec(self) -> None:
        assert decode_fourcc(0x34363248) == "H264"

    def test_zero(self) -> None:
        assert decode_fourcc(0) == "unknown"

    def test_cache(self) -> None:
        assert decode_fourcc(0x34363248) == "H264"


class TestApplyResolution:
    def test_original(self, frame_1080p) -> None:
        result = apply_resolution(frame_1080p, "原始")
        assert result.shape[:2] == (1080, 1920)

    def test_downscale(self, frame_1080p) -> None:
        result = apply_resolution(frame_1080p, "1280x720")
        assert result.shape[0] <= 720
        assert result.shape[1] <= 1280

    def test_upscale_no_change(self, frame_1080p) -> None:
        result = apply_resolution(frame_1080p, "3840x2160")
        assert result.shape[:2] == (1080, 1920)

    def test_invalid_string(self, frame_1080p) -> None:
        result = apply_resolution(frame_1080p, "")
        assert result.shape[:2] == (1080, 1920)


class TestOverlayGpsText:
    def test_overlay_adds_text(self, frame_480p) -> None:
        result = overlay_gps_text(frame_480p.copy(), (25.123456, 121.654321))
        assert not np.array_equal(result, frame_480p)

    def test_different_coords(self, frame_480p) -> None:
        r1 = overlay_gps_text(frame_480p.copy(), (25.0, 121.0))
        r2 = overlay_gps_text(frame_480p.copy(), (25.5, 121.5))
        diff = np.sum(r1.astype(int) - r2.astype(int))
        assert diff != 0


class TestImwriteSafe:
    def test_write_jpeg(self, tmp_path) -> None:
        frame = np.random.randint(0, 256, (100, 100, 3), dtype=np.uint8)
        path = str(tmp_path / "test.jpg")
        ok = imwrite_safe(path, frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        assert ok

    def test_write_png(self, frame_small, tmp_path) -> None:
        path = str(tmp_path / "test.png")
        ok = imwrite_safe(path, frame_small)
        assert ok


class TestOpenVideoCapture:
    """open_video_capture 應原生支援中文/Unicode 路徑（opencv-python >= 4.8）。"""

    @staticmethod
    def _make_video(tmp_path, name: str = "test.mp4") -> str:
        path = str(tmp_path / name)
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(path, fourcc, 30.0, (64, 64))
        if not writer.isOpened():
            pytest.skip("mp4v 編碼器不可用，無法生成測試影片")
        for i in range(30):
            writer.write(np.full((64, 64, 3), i * 8, dtype=np.uint8))
        writer.release()
        return path

    def test_open_ascii_path(self, tmp_path) -> None:
        path = self._make_video(tmp_path)
        cap = open_video_capture(path)
        assert cap.isOpened()
        ret, frame = cap.read()
        assert ret and frame is not None
        cap.release()

    def test_open_unicode_path(self, tmp_path) -> None:
        path = self._make_video(tmp_path, "測試影片_2026_空間 .mp4")
        cap = open_video_capture(path)
        assert cap.isOpened(), "opencv >= 4.8 應原生支援中文路徑開啟影片"
        ret, _ = cap.read()
        assert ret
        cap.release()

    def test_nonexistent_file_returns_closed_cap(self, tmp_path) -> None:
        cap = open_video_capture(str(tmp_path / "no_such_file.mp4"))
        assert not cap.isOpened()
        cap.release()


class TestFormatSpeedLabel:
    """format_speed_label 單元測試（R2 驗收：is_vision=True 時含「實驗」；False 時不含）。"""

    def test_vision_source_has_experiment_label(self) -> None:
        label = format_speed_label(45.5, "vision")
        assert "45.5 km/h" in label
        assert "（實驗）" in label

    def test_gps_source_no_experiment_label(self) -> None:
        label = format_speed_label(60.0, "gps")
        assert "60.0 km/h" in label
        assert "（實驗）" not in label

    def test_empty_source_no_experiment_label(self) -> None:
        label = format_speed_label(30.2, "")
        assert "30.2 km/h" in label
        assert "（實驗）" not in label

    def test_unknown_source_no_experiment_label(self) -> None:
        label = format_speed_label(55.5, "unknown")
        assert "55.5 km/h" in label
        assert "（實驗）" not in label
