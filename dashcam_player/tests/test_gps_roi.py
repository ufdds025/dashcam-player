"""GPS ROI 模組測試"""

from __future__ import annotations

import time

import pytest

from dashcam_player.models.gps_roi import GPSROIDetectionResult, GPSROISetting
from dashcam_player.utils.gps_roi_detector import (
    _bbox_to_corner,
    _compute_union_roi,
    _is_likely_gps_bbox,
)
from dashcam_player.utils.gps_roi_templates import get_template_by_corner, get_templates


class TestGPSROISetting:
    def test_key_property(self) -> None:
        s = GPSROISetting(
            resolution_w=1920,
            resolution_h=1080,
            file_format=".MP4",
            roi_x=0.0,
            roi_y=0.0,
            roi_w=0.5,
            roi_h=0.15,
        )
        assert s.key == "1920x1080_MP4"

    def test_key_strips_dot(self) -> None:
        s = GPSROISetting(
            resolution_w=1280,
            resolution_h=720,
            file_format=".avi",
            roi_x=0.0,
            roi_y=0.0,
            roi_w=0.5,
            roi_h=0.15,
        )
        assert s.key == "1280x720_AVI"

    def test_key_no_dot(self) -> None:
        s = GPSROISetting(
            resolution_w=640,
            resolution_h=480,
            file_format="MP4",
            roi_x=0.0,
            roi_y=0.0,
            roi_w=0.5,
            roi_h=0.15,
        )
        assert s.key == "640x480_MP4"

    def test_default_timestamp(self) -> None:
        before = time.time()
        s = GPSROISetting(
            resolution_w=1920,
            resolution_h=1080,
            file_format=".MP4",
            roi_x=0.0,
            roi_y=0.0,
            roi_w=0.5,
            roi_h=0.15,
        )
        after = time.time()
        assert before <= s.timestamp <= after

    def test_default_confidence(self) -> None:
        s = GPSROISetting(
            resolution_w=1920,
            resolution_h=1080,
            file_format=".MP4",
            roi_x=0.0,
            roi_y=0.0,
            roi_w=0.5,
            roi_h=0.15,
        )
        assert s.confidence == 1.0

    def test_default_source(self) -> None:
        s = GPSROISetting(
            resolution_w=1920,
            resolution_h=1080,
            file_format=".MP4",
            roi_x=0.0,
            roi_y=0.0,
            roi_w=0.5,
            roi_h=0.15,
        )
        assert s.source == "user_adjusted"

    def test_custom_values(self) -> None:
        s = GPSROISetting(
            resolution_w=1920,
            resolution_h=1080,
            file_format=".MP4",
            roi_x=0.1,
            roi_y=0.2,
            roi_w=0.3,
            roi_h=0.4,
            corner="top_right",
            layout="two_line",
            source="auto_detected",
            confidence=0.85,
        )
        assert s.roi_x == 0.1
        assert s.roi_y == 0.2
        assert s.roi_w == 0.3
        assert s.roi_h == 0.4
        assert s.corner == "top_right"
        assert s.layout == "two_line"
        assert s.source == "auto_detected"
        assert s.confidence == 0.85


class TestGPSROIDetectionResult:
    def test_basic(self) -> None:
        r = GPSROIDetectionResult(
            video_path="/path/to/video.mp4",
            video_width=1920,
            video_height=1080,
            file_format=".MP4",
            detected_roi=(0.0, 0.0, 0.5, 0.15),
            detected_corner="top_left",
            confidence=0.75,
            sample_frame_idx=0,
        )
        assert r.video_width == 1920
        assert r.video_height == 1080
        assert r.detected_roi == (0.0, 0.0, 0.5, 0.15)
        assert r.detected_corner == "top_left"
        assert r.confidence == 0.75


class TestTemplates:
    def test_get_templates_all(self) -> None:
        templates = get_templates()
        assert len(templates) >= 4

    def test_get_templates_filtered(self) -> None:
        templates = get_templates(min_width=0, min_height=0)
        assert len(templates) >= 4

    def test_get_template_by_corner(self) -> None:
        t = get_template_by_corner("top_left")
        assert t is not None
        assert t.corner == "top_left"
        assert t.roi_x == 0.0
        assert t.roi_y == 0.0

    def test_get_template_by_corner_missing(self) -> None:
        t = get_template_by_corner("center")
        assert t is None

    def test_templates_have_required_fields(self) -> None:
        for t in get_templates():
            assert t.name
            assert t.corner in ("top_left", "top_right", "bottom_left", "bottom_right")
            assert t.layout in ("single_line", "two_line", "three_line")
            assert 0.0 <= t.roi_x <= 1.0
            assert 0.0 <= t.roi_y <= 1.0


class TestComputeUnionROI:
    def test_single_bbox(self) -> None:
        bboxes: list[list[list[float]]] = [[[100.0, 100.0], [200.0, 100.0], [200.0, 50.0], [100.0, 50.0]]]
        roi = _compute_union_roi(bboxes, 1920, 1080, padding_x=0, padding_y=0)
        assert roi[0] == pytest.approx(100 / 1920)
        assert roi[1] == pytest.approx(50 / 1080)
        assert roi[2] == pytest.approx(100 / 1920)
        assert roi[3] == pytest.approx(50 / 1080)

    def test_multiple_bboxes_union(self) -> None:
        bboxes: list[list[list[float]]] = [
            [[100.0, 100.0], [200.0, 100.0], [200.0, 50.0], [100.0, 50.0]],
            [[150.0, 120.0], [250.0, 120.0], [250.0, 70.0], [150.0, 70.0]],
        ]
        roi = _compute_union_roi(bboxes, 1920, 1080, padding_x=0, padding_y=0)
        assert roi[0] == pytest.approx(100 / 1920)
        assert roi[1] == pytest.approx(50 / 1080)
        assert roi[2] == pytest.approx(150 / 1920)
        assert roi[3] == pytest.approx(70 / 1080)

    def test_padding(self) -> None:
        bboxes: list[list[list[float]]] = [[[100.0, 100.0], [200.0, 100.0], [200.0, 50.0], [100.0, 50.0]]]
        roi = _compute_union_roi(bboxes, 1920, 1080, padding_x=0.1, padding_y=0.1)
        pad_x = int(1920 * 0.1)
        pad_y = int(1080 * 0.1)
        x1 = max(0, 100 - pad_x)
        y1 = max(0, 50 - pad_y)
        x2 = min(1920, 200 + pad_x)
        y2 = min(1080, 100 + pad_y)
        assert roi[0] == pytest.approx(x1 / 1920)
        assert roi[1] == pytest.approx(y1 / 1080)
        assert roi[2] == pytest.approx((x2 - x1) / 1920)
        assert roi[3] == pytest.approx((y2 - y1) / 1080)

    def test_clamp_to_frame(self) -> None:
        bboxes: list[list[list[float]]] = [[[-50.0, -50.0], [250.0, -50.0], [250.0, 150.0], [-50.0, 150.0]]]
        roi = _compute_union_roi(bboxes, 1920, 1080, padding_x=0, padding_y=0)
        assert 0 <= roi[0] <= 1
        assert 0 <= roi[1] <= 1


class TestIsLikelyGpsBBox:
    def test_top_left_corner(self) -> None:
        bbox: list[list[float]] = [[10.0, 10.0], [200.0, 10.0], [200.0, 40.0], [10.0, 40.0]]
        assert _is_likely_gps_bbox(bbox, 1920, 1080)

    def test_center_region(self) -> None:
        bbox: list[list[float]] = [[800.0, 400.0], [1000.0, 400.0], [1000.0, 430.0], [800.0, 430.0]]
        assert not _is_likely_gps_bbox(bbox, 1920, 1080)

    def test_too_small(self) -> None:
        bbox: list[list[float]] = [[10.0, 10.0], [30.0, 10.0], [30.0, 15.0], [10.0, 15.0]]
        assert not _is_likely_gps_bbox(bbox, 1920, 1080)

    def test_bottom_right_corner(self) -> None:
        bbox: list[list[float]] = [[1700.0, 900.0], [1900.0, 900.0], [1900.0, 930.0], [1700.0, 930.0]]
        assert _is_likely_gps_bbox(bbox, 1920, 1080)


class TestBBoxToCorner:
    def test_top_left(self) -> None:
        bbox: list[list[float]] = [[10.0, 10.0], [200.0, 10.0], [200.0, 50.0], [10.0, 50.0]]
        assert _bbox_to_corner(bbox, 1920, 1080) == "top_left"

    def test_top_right(self) -> None:
        bbox: list[list[float]] = [[1700.0, 10.0], [1900.0, 10.0], [1900.0, 50.0], [1700.0, 50.0]]
        assert _bbox_to_corner(bbox, 1920, 1080) == "top_right"

    def test_bottom_left(self) -> None:
        bbox: list[list[float]] = [[10.0, 950.0], [200.0, 950.0], [200.0, 1000.0], [10.0, 1000.0]]
        assert _bbox_to_corner(bbox, 1920, 1080) == "bottom_left"

    def test_bottom_right(self) -> None:
        bbox: list[list[float]] = [[1700.0, 950.0], [1900.0, 950.0], [1900.0, 1000.0], [1700.0, 1000.0]]
        assert _bbox_to_corner(bbox, 1920, 1080) == "bottom_right"


class TestGPSROIDetector:
    """GPSROIDetector 的單元測試（不依賴 EasyOCR，純 OpenCV 實作）"""

    def test_detect_empty_video(self, tmp_path) -> None:
        """不存在的影片回傳 None"""
        from dashcam_player.utils.gps_roi_detector import GPSROIDetector

        detector = GPSROIDetector()
        result = detector.detect(str(tmp_path / "nonexistent.mp4"))
        assert result is None

    def test_is_gps_bbox_filter(self) -> None:
        """靜態方法 _is_gps_bbox 的單元測試"""
        from dashcam_player.utils.gps_roi_detector import GPSROIDetector

        frame_h, frame_w = 1080, 1920
        bbox: list[list[float]] = [[10.0, 10.0], [200.0, 10.0], [200.0, 40.0], [10.0, 40.0]]
        assert GPSROIDetector._is_gps_bbox(bbox, frame_w, frame_h)

        bbox_center: list[list[float]] = [[800.0, 400.0], [1000.0, 400.0], [1000.0, 430.0], [800.0, 430.0]]
        assert not GPSROIDetector._is_gps_bbox(bbox_center, frame_w, frame_h)
