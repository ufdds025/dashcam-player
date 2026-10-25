from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from dashcam_player.utils.gps_ocr import (
    GPSOCR,
    _cap_ocr_width,
    _clean_ocr_confusions,
    _stabilize_ocr_tracks,
)


class TestCleanOcrConfusions:
    def test_number_tokens_fixed(self) -> None:
        assert _clean_ocr_confusions("24.06806lN 12O.358O15E") == "24.068061N 120.358015E"

    def test_hemisphere_letters_preserved(self) -> None:
        assert _clean_ocr_confusions("S24.338535 W120.455958") == "S24.338535 W120.455958"

    def test_l_and_i_inside_number(self) -> None:
        assert _clean_ocr_confusions("24.B68061N") == "24.868061N"

    def test_no_change_for_clean_text(self) -> None:
        assert _clean_ocr_confusions("24.338535N 120.455958E 5Km/h") == "24.338535N 120.455958E 5Km/h"


class TestStabilizeOcrTracks:
    def test_dict_tracks_fix_truncated_integer(self) -> None:
        points = [
            {"lat": 4.068061, "lon": 120.358015, "time": 0.0},
            {"lat": 24.068055, "lon": 120.358010, "time": 1.0},
            {"lat": 24.068049, "lon": 120.358005, "time": 2.0},
        ]
        out = _stabilize_ocr_tracks(points)
        assert out[0]["lat"] == pytest.approx(24.068061)

    def test_object_tracks_fix_both_axes(self) -> None:
        points = [
            SimpleNamespace(lat=24.068061, lon=20.358015, time=0.0),
            SimpleNamespace(lat=24.068055, lon=120.358010, time=1.0),
            SimpleNamespace(lat=24.068049, lon=120.358005, time=2.0),
        ]
        out = _stabilize_ocr_tracks(points)
        assert out[0].lon == pytest.approx(120.358015)
        assert out[0].lat == pytest.approx(24.068061)

    def test_consistent_tracks_unchanged(self) -> None:
        points = [
            {"lat": 24.068061, "lon": 120.358015, "time": 0.0},
            {"lat": 24.068055, "lon": 120.358010, "time": 1.0},
            {"lat": 24.068049, "lon": 120.358005, "time": 2.0},
        ]
        out = _stabilize_ocr_tracks(points)
        assert out == points

    def test_fewer_than_three_unchanged(self) -> None:
        points = [{"lat": 4.068, "lon": 120.358, "time": 0.0}]
        assert _stabilize_ocr_tracks(points) == points


class TestCapOcrWidth:
    def test_narrow_crop_unchanged(self) -> None:
        crop = np.zeros((100, 500), dtype=np.uint8)
        out = _cap_ocr_width(crop, 0.01)
        assert out.shape == (100, 500)

    def test_left_corner_keeps_left_window(self) -> None:
        crop = np.zeros((100, 1200), dtype=np.uint8)
        crop[:, :900] = 7
        out = _cap_ocr_width(crop, 0.01)
        assert out.shape == (100, 900)
        assert np.all(out == 7)

    def test_right_corner_keeps_right_window(self) -> None:
        crop = np.zeros((100, 1200), dtype=np.uint8)
        crop[:, -900:] = 9
        out = _cap_ocr_width(crop, 0.9)
        assert out.shape == (100, 900)
        assert np.all(out == 9)


class _FakeOCR:
    def __init__(self, results: dict[int, str]) -> None:
        self.results = results
        self.calls: list[int] = []

    def predict(self, img: np.ndarray):
        h = img.shape[0]
        self.calls.append(h)
        if h in self.results:
            return [{"rec_texts": [self.results[h]]}]
        return [{"rec_texts": []}]


class TestRecognizeMultiScale:
    def test_upscale_then_fallback_single(self, monkeypatch) -> None:
        ocr = GPSOCR()
        fake = _FakeOCR({100: "24.068061N 120.358015E 102Km/h"})
        monkeypatch.setattr(ocr, "_ensure_ocr", lambda: fake)
        img = np.zeros((100, 300), dtype=np.uint8)
        result = ocr._recognize(img)
        assert result == (24.068061, 120.358015)
        assert fake.calls == [200, 100]  # 2x 放大失敗後退回 1x

    def test_large_image_no_upscale(self, monkeypatch) -> None:
        ocr = GPSOCR()
        fake = _FakeOCR({1000: "24.068061N 120.358015E 102Km/h"})
        monkeypatch.setattr(ocr, "_ensure_ocr", lambda: fake)
        img = np.zeros((1000, 1200), dtype=np.uint8)
        assert ocr._recognize(img) == (24.068061, 120.358015)
        assert fake.calls == [1000]

    def test_all_scales_fail_returns_none(self, monkeypatch) -> None:
        ocr = GPSOCR()
        fake = _FakeOCR({})
        monkeypatch.setattr(ocr, "_ensure_ocr", lambda: fake)
        img = np.zeros((100, 300), dtype=np.uint8)
        assert ocr._recognize(img) is None
