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

    def test_conflict_with_single_capture_naming(self, controller):
        """單張截圖命名 {prefix}_{timestamp}.{ext} 也應被視為衝突。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            open(os.path.join(tmpdir, "capture_20260816_120000.jpg"), "w").close()
            result = controller.check_batch_conflict(tmpdir, "capture", "jpg")
            assert len(result) == 1

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


# ============================================================
# Worker → PlaybackManager → Controller 信號轉發鏈（regression）
# ============================================================


class TestSignalRelayChain:
    """確保 VideoWorker 輸出信號確實接到 PlaybackManager，
    否則 video_loaded / frame_ready 永遠不會觸發（Phase 4 重構後曾漏接）。
    """

    def test_frame_relay(self, qapp):
        from dashcam_player.core.controller import Controller

        ctrl = Controller()
        received: list = []
        ctrl.frame_ready.connect(lambda f, i: received.append((f, i)))

        import numpy as np

        frame = np.zeros((10, 10, 3), dtype=np.uint8)
        ctrl.worker.frame_ready.emit(frame, 7)

        assert len(received) == 1
        assert received[0][1] == 7
        assert ctrl.current_frame is frame

    def test_video_info_relay(self, qapp):
        from dashcam_player.core.controller import Controller
        from dashcam_player.models.video_info import VideoInfo

        ctrl = Controller()
        loaded: list = []
        ctrl.video_loaded.connect(loaded.append)

        info = VideoInfo(path="x.mp4", fps=30.0, frame_count=900, duration=30.0)
        ctrl.worker.video_info_ready.emit(info)

        assert len(loaded) == 1
        assert ctrl.video_info is info
        assert ctrl.total_frames == 900

    def test_play_finished_relay(self, qapp):
        from dashcam_player.core.controller import Controller

        ctrl = Controller()
        finished: list = []
        ctrl.play_finished.connect(lambda: finished.append(True))

        ctrl.worker.play_finished.emit()

        assert finished == [True]

    def test_error_occurred_relay(self, qapp):
        from dashcam_player.core.controller import Controller

        ctrl = Controller()
        received: list = []
        ctrl.error_occurred.connect(lambda title, msg: received.append((title, msg)))

        ctrl._capture.error_occurred.emit("批次截圖失敗", "無法開啟影片: x.mp4")

        assert received == [("批次截圖失敗", "無法開啟影片: x.mp4")]

    def test_worker_error_relay(self, qapp):
        """VideoWorker 的 seek 失敗等錯誤經 Controller.error_occurred 轉發。"""
        from dashcam_player.core.controller import Controller

        ctrl = Controller()
        received: list = []
        ctrl.error_occurred.connect(lambda title, msg: received.append((title, msg)))

        ctrl.worker.error_occurred.emit("跳轉失敗", "無法跳轉至幀 42")

        assert received == [("跳轉失敗", "無法跳轉至幀 42")]

    def test_capture_load_failed_no_longer_relayed(self, qapp):
        """CaptureManager 已移除 load_failed 信號（改用 error_occurred）。"""
        from dashcam_player.core.controller import Controller

        ctrl = Controller()
        assert not hasattr(ctrl._capture, "load_failed")

    def test_capture_single_uses_current_frame(self, qapp):
        """capture_single 不再接收 frame 參數，改由 Controller.current_frame 提供。"""
        import numpy as np

        from dashcam_player.core.controller import Controller
        from dashcam_player.models.resolution import Resolution

        ctrl = Controller()
        ctrl.video_info = VideoInfo(path="t.mp4", fps=30.0, frame_count=3000, duration=100.0)

        captured: dict = {}

        def _fake_capture(frame, out_dir, prefix, img_format, quality, resolution,
                          frame_idx, fps, gps_manager):
            captured.update(frame=frame, frame_idx=frame_idx, fps=fps)
            return "ok"

        ctrl._capture.capture_single = _fake_capture  # type: ignore[assignment]

        ctrl._playback._current_frame = np.zeros((10, 10, 3), dtype=np.uint8)
        ctrl._playback._current_idx = 42

        result = ctrl.capture_single(
            "out", "cap", "JPEG", 95, Resolution.ORIGINAL, 42
        )
        assert result == "ok"
        assert captured["frame"].shape == (10, 10, 3)
        assert captured["frame_idx"] == 42

    def test_gps_manager_facade_signal_lifecycle(self, qapp):
        """GPSManager 信號連線由 GPSFacade 管理，替換 manager 後不會重複連接。"""
        from dashcam_player.core.controller import Controller
        from dashcam_player.core.gps_manager import GPSManager

        ctrl = Controller()
        received: list = []
        ctrl.gps_ready.connect(lambda *a: received.append(a))

        mgr1 = GPSManager("a.mp4", skip_ocr=True, parent=ctrl)
        mgr2 = GPSManager("b.mp4", skip_ocr=True, parent=ctrl)
        ctrl.gps_manager = mgr1
        ctrl.gps_manager = mgr2
        ctrl._gps.set_video_context("b.mp4", None)

        # 只有新 manager 的信號有連接；emit 一次只觸發一次
        mgr2.gps_loaded.emit("b.mp4", True, 5)
        assert received == [("b.mp4", True, 5)]

        received.clear()
        mgr1.gps_loaded.emit("b.mp4", True, 3)
        assert received == []  # 舊 manager 已 disconnect

    def test_gps_manager_facade_property(self, qapp):
        """Controller.gps_manager 不應直接洩漏 GPSFacade 內部屬性。"""
        from dashcam_player.core.controller import Controller

        ctrl = Controller()
        assert ctrl.gps_manager is ctrl._gps.manager

    def test_gps_status_message_forwarded_via_facade(self, qapp):
        """GPSManager.gps_status_message 經 GPSFacade 轉發至 Controller.status_message。"""
        from dashcam_player.core.controller import Controller
        from dashcam_player.core.gps_manager import GPSManager

        ctrl = Controller()
        msgs: list = []
        ctrl.status_message.connect(lambda m: msgs.append(m))

        mgr = GPSManager("a.mp4", skip_ocr=True, parent=ctrl)
        ctrl.gps_manager = mgr
        mgr.gps_status_message.emit("hello")
        assert msgs == ["hello"]

    def test_gps_facade_stale_result_guard(self, qapp):
        """on_gps_loaded 需 path 與目前 video context 相符才轉發（過期結果保護）。"""
        from dashcam_player.core.controller import Controller
        from dashcam_player.core.gps_manager import GPSManager

        ctrl = Controller()
        received: list = []
        ctrl.gps_ready.connect(lambda *a: received.append(a))

        mgr = GPSManager("a.mp4", skip_ocr=True, parent=ctrl)
        ctrl.gps_manager = mgr
        ctrl._gps.set_video_context("b.mp4", None)

        mgr.gps_loaded.emit("a.mp4", True, 5)
        assert received == []  # 影片路徑不符 → 丟棄

        ctrl._gps.set_video_context("a.mp4", None)
        mgr.gps_loaded.emit("a.mp4", True, 5)
        assert received == [("a.mp4", True, 5)]
