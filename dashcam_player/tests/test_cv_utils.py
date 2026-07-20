from __future__ import annotations

import cv2
import numpy as np

from dashcam_player.utils.cv_utils import (
    apply_resolution,
    decode_fourcc,
    imwrite_safe,
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
