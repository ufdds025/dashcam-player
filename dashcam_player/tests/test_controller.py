from __future__ import annotations

import os
import tempfile

import pytest

from dashcam_player.models.video_info import VideoInfo


@pytest.fixture(scope="session")
def qapp():
    """Minimal QApplication for QObject-based tests."""
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def controller(qapp):
    """Controller with fake video_info, no real video file."""
    from dashcam_player.core.controller import Controller

    ctrl = Controller()
    ctrl.video_info = VideoInfo(path="test.mp4", fps=30.0, frame_count=3000, duration=100.0)
    return ctrl


# ============================================================
# validate_batch_range
# ============================================================


class TestValidateBatchRange:
    def test_no_video(self, qapp):
        from dashcam_player.core.controller import Controller

        ctrl = Controller()
        assert ctrl.validate_batch_range() == "請先開啟影片"

    def test_no_markers(self, controller):
        controller._capture_start_frame = -1
        controller._capture_end_frame = -1
        assert controller.validate_batch_range() == "請先正確設定開始與結束標記（開始 < 結束）"

    def test_start_equals_end(self, controller):
        controller._capture_start_frame = 100
        controller._capture_end_frame = 100
        assert controller.validate_batch_range() == "請先正確設定開始與結束標記（開始 < 結束）"

    def test_start_greater_than_end(self, controller):
        controller._capture_start_frame = 200
        controller._capture_end_frame = 100
        assert controller.validate_batch_range() == "請先正確設定開始與結束標記（開始 < 結束）"

    def test_valid_range(self, controller):
        controller._capture_start_frame = 100
        controller._capture_end_frame = 200
        assert controller.validate_batch_range() is None


# ============================================================
# check_batch_conflict
# ============================================================


class TestCheckBatchConflict:
    def test_nonexistent_dir(self, controller):
        result = controller.check_batch_conflict("/nonexistent/path", "capture", "jpg")
        assert result == []

    def test_empty_dir(self, controller):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = controller.check_batch_conflict(tmpdir, "capture", "jpg")
            assert result == []

    def test_no_conflict(self, controller):
        with tempfile.TemporaryDirectory() as tmpdir:
            open(os.path.join(tmpdir, "other_file.jpg"), "w").close()
            result = controller.check_batch_conflict(tmpdir, "capture", "jpg")
            assert result == []

    def test_has_conflict(self, controller):
        with tempfile.TemporaryDirectory() as tmpdir:
            open(os.path.join(tmpdir, "capture_frame000001.jpg"), "w").close()
            open(os.path.join(tmpdir, "capture_frame000002.jpg"), "w").close()
            result = controller.check_batch_conflict(tmpdir, "capture", "jpg")
            assert len(result) == 2

    def test_conflict_wrong_ext(self, controller):
        with tempfile.TemporaryDirectory() as tmpdir:
            open(os.path.join(tmpdir, "capture_frame000001.png"), "w").close()
            result = controller.check_batch_conflict(tmpdir, "capture", "jpg")
            assert result == []

    def test_conflict_wrong_prefix(self, controller):
        with tempfile.TemporaryDirectory() as tmpdir:
            open(os.path.join(tmpdir, "other_frame000001.jpg"), "w").close()
            result = controller.check_batch_conflict(tmpdir, "capture", "jpg")
            assert result == []


# ============================================================
# validate_export_times
# ============================================================


class TestValidateExportTimes:
    def test_no_video(self, qapp):
        from dashcam_player.core.controller import Controller

        ctrl = Controller()
        s, e, err = ctrl.validate_export_times("00:01:00", "00:02:00")
        assert err == "請先開啟影片"

    def test_valid_hh_mm_ss(self, controller):
        s, e, err = controller.validate_export_times("00:01:00", "00:02:00")
        assert err is None
        assert s == 60.0
        assert e == 120.0

    def test_valid_mm_ss(self, controller):
        s, e, err = controller.validate_export_times("1:30", "3:00")
        assert err is None
        assert s == 90.0
        assert e == 180.0

    def test_invalid_format(self, controller):
        s, e, err = controller.validate_export_times("abc", "00:02:00")
        assert err == "時間格式錯誤，請使用 hh:mm:ss 格式"

    def test_start_ge_end(self, controller):
        s, e, err = controller.validate_export_times("00:03:00", "00:02:00")
        assert err == "開始時間必須小於結束時間"

    def test_start_equals_end(self, controller):
        s, e, err = controller.validate_export_times("00:02:00", "00:02:00")
        assert err == "開始時間必須小於結束時間"

    def test_fallback_to_markers(self, controller):
        controller._capture_start_frame = 300
        controller._capture_end_frame = 600
        s, e, err = controller.validate_export_times("", "")
        assert err is None
        assert s == pytest.approx(10.0)
        assert e == pytest.approx(20.0)

    def test_markers_invalid(self, controller):
        controller._capture_start_frame = 600
        controller._capture_end_frame = 300
        s, e, err = controller.validate_export_times("", "")
        assert err == "開始標記必須小於結束標記"

    def test_no_markers_no_times(self, controller):
        controller._capture_start_frame = -1
        controller._capture_end_frame = -1
        s, e, err = controller.validate_export_times("", "")
        assert err == "請先設定開始與結束標記，或輸入時間範圍（hh:mm:ss）"
