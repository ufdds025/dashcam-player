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
        result = controller.check_batch_conflict("/nonexistent/path", "capture", "jpg", "CLIP")
        assert result == []

    def test_empty_dir(self, controller):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = controller.check_batch_conflict(tmpdir, "capture", "jpg", "CLIP")
            assert result == []

    def test_no_conflict(self, controller):
        with tempfile.TemporaryDirectory() as tmpdir:
            open(os.path.join(tmpdir, "unrelated.jpg"), "w").close()
            result = controller.check_batch_conflict(tmpdir, "capture", "jpg", "CLIP")
            assert result == []

    def test_has_conflict(self, controller):
        with tempfile.TemporaryDirectory() as tmpdir:
            open(os.path.join(tmpdir, "capture_CLIP_f000001.jpg"), "w").close()
            open(os.path.join(tmpdir, "capture_CLIP_f000002.jpg"), "w").close()
            result = controller.check_batch_conflict(tmpdir, "capture", "jpg", "CLIP")
            assert len(result) == 2

    def test_conflict_with_single_capture_naming(self, controller):
        """單張與批次現在使用相同幀編號命名，故舊單張檔也視為衝突。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            open(os.path.join(tmpdir, "capture_CLIP_f000050.jpg"), "w").close()
            result = controller.check_batch_conflict(tmpdir, "capture", "jpg", "CLIP")
            assert len(result) == 1

    def test_conflict_wrong_ext(self, controller):
        with tempfile.TemporaryDirectory() as tmpdir:
            open(os.path.join(tmpdir, "capture_CLIP_f000001.png"), "w").close()
            result = controller.check_batch_conflict(tmpdir, "capture", "jpg", "CLIP")
            assert result == []

    def test_conflict_wrong_prefix(self, controller):
        with tempfile.TemporaryDirectory() as tmpdir:
            open(os.path.join(tmpdir, "other_CLIP_f000001.jpg"), "w").close()
            result = controller.check_batch_conflict(tmpdir, "capture", "jpg", "CLIP")
            assert result == []

    def test_conflict_wrong_source(self, controller):
        """不同來源（不同 stem）不視為衝突，解決跨來源撞名問題。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            open(os.path.join(tmpdir, "capture_OTHER_f000001.jpg"), "w").close()
            result = controller.check_batch_conflict(tmpdir, "capture", "jpg", "CLIP")
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
        ctrl._worker.frame_ready.emit(frame, 7)

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
        ctrl._worker.video_info_ready.emit(info)

        assert len(loaded) == 1
        assert ctrl.video_info is info
        assert ctrl.total_frames == 900

    def test_play_finished_relay(self, qapp):
        from dashcam_player.core.controller import Controller

        ctrl = Controller()
        finished: list = []
        ctrl.play_finished.connect(lambda: finished.append(True))

        ctrl._worker.play_finished.emit()

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

        ctrl._worker.error_occurred.emit("跳轉失敗", "無法跳轉至幀 42")

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
        ctrl._gps_manager = mgr1
        ctrl._gps_manager = mgr2
        ctrl._gps.set_video_context("b.mp4", None)

        # 只有新 manager 的信號有連接；emit 一次只觸發一次
        mgr2.gps_loaded.emit("b.mp4", True, 5)
        assert received == [("b.mp4", True, 5)]

        received.clear()
        mgr1.gps_loaded.emit("b.mp4", True, 3)
        assert received == []  # 舊 manager 已 disconnect

    def test_gps_manager_facade_property(self, qapp):
        """Controller._gps_manager 不應直接洩漏 GPSFacade 內部屬性。"""
        from dashcam_player.core.controller import Controller

        ctrl = Controller()
        assert ctrl._gps_manager is ctrl._gps.manager

    def test_gps_status_message_forwarded_via_facade(self, qapp):
        """GPSManager.gps_status_message 經 GPSFacade 轉發至 Controller.status_message。"""
        from dashcam_player.core.controller import Controller
        from dashcam_player.core.gps_manager import GPSManager

        ctrl = Controller()
        msgs: list = []
        ctrl.status_message.connect(lambda m: msgs.append(m))

        mgr = GPSManager("a.mp4", skip_ocr=True, parent=ctrl)
        ctrl._gps_manager = mgr
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
        ctrl._gps_manager = mgr
        ctrl._gps.set_video_context("b.mp4", None)

        mgr.gps_loaded.emit("a.mp4", True, 5)
        assert received == []  # 影片路徑不符 → 丟棄

        ctrl._gps.set_video_context("a.mp4", None)
        mgr.gps_loaded.emit("a.mp4", True, 5)
        assert received == [("a.mp4", True, 5)]


class TestGetCurrentSpeedGPSPriority:
    """get_current_speed 回傳 GPS 優先（R3 驗收）。"""

    def _make_controller(self):
        from unittest.mock import Mock

        from dashcam_player.core.controller import Controller
        from dashcam_player.models.video_info import VideoInfo

        ctrl = Controller()
        ctrl.video_info = VideoInfo(path="test.mp4", fps=30.0, frame_count=3000, duration=100.0)
        ctrl._speed = Mock()
        ctrl._gps = Mock()
        return ctrl

    def test_gps_priority_when_both_available(self, qapp):
        """GPS 與視覺都有 → 回 GPS。"""
        ctrl = self._make_controller()

        # GPS 回傳有效速度，視覺也回傳有效速度
        ctrl._gps.get_current_speed.return_value = 60.0
        ctrl._speed.get_current_speed.return_value = (45.0, "vision")

        speed, source = ctrl.get_current_speed(100)

        assert speed == 60.0
        assert source == "gps"
        ctrl._gps.get_current_speed.assert_called_once_with(100, 30.0)
        ctrl._speed.get_current_speed.assert_not_called()  # GPS 優先，不呼叫視覺

    def test_vision_fallback_when_no_gps(self, qapp):
        """只有視覺 → 回視覺且 source='vision'。"""
        ctrl = self._make_controller()

        # GPS 回傳 None，視覺回傳有效速度
        ctrl._gps.get_current_speed.return_value = None
        ctrl._speed.get_current_speed.return_value = (45.0, "vision")

        speed, source = ctrl.get_current_speed(100)

        assert speed == 45.0
        assert source == "vision"
        ctrl._gps.get_current_speed.assert_called_once_with(100, 30.0)
        ctrl._speed.get_current_speed.assert_called_once_with(100, 30.0)

    def test_both_none_returns_none(self, qapp):
        """兩者皆無 → 回 (None, '')。"""
        ctrl = self._make_controller()

        ctrl._gps.get_current_speed.return_value = None
        ctrl._speed.get_current_speed.return_value = (None, "")

        speed, source = ctrl.get_current_speed(100)

        assert speed is None
        assert source == ""


class TestVideoPlayerSpeedUpdated:
    """VideoPlayer._on_speed_updated 在背景掃描時不覆蓋畫面速度（WO-1026c）。"""

    def test_on_speed_updated_does_not_overwrite_current_speed(self, qapp):
        """收到 vision speed_updated 時，_current_speed_kmh 與 speed_label 不變。"""
        # 在 qapp fixture 執行後（QApplication 已建立）再 import VideoPlayer
        from unittest.mock import MagicMock, Mock

        from dashcam_player.core.controller import Controller
        from dashcam_player.core.video_player import VideoPlayer
        from dashcam_player.models.video_info import VideoInfo

        # 建立 VideoPlayer，並 mock 其 UI 元件與 controller
        player = VideoPlayer()
        player.ctrl = Mock(spec=Controller)
        player.ctrl.video_info = VideoInfo(path="test.mp4", fps=30.0, frame_count=3000, duration=100.0)

        # Mock controls.speed_label
        mock_speed_label = MagicMock()
        player.controls = MagicMock()
        player.controls.speed_label = mock_speed_label

        # Mock statusBar
        mock_statusbar = MagicMock()
        player.statusBar = Mock(return_value=mock_statusbar)

        # 先設定一個 GPS 速度（模擬播放中已有 GPS 速度顯示）
        player._current_speed_kmh = 60.0
        player._current_speed_source = "gps"
        mock_speed_label.text.return_value = "60.0 km/h"

        # 呼叫 _on_speed_updated（背景掃描發出的 vision 速度）
        player._on_speed_updated(time_sec=12.3, speed_kmh=45.0, confidence=0.8, source="vision")

        # 驗證：_current_speed_kmh 與 _current_speed_source 不變（仍為 GPS）
        assert player._current_speed_kmh == 60.0
        assert player._current_speed_source == "gps"

        # 驗證：speed_label 沒有被呼叫 setText（即不更新畫面顯示）
        mock_speed_label.setText.assert_not_called()

        # 驗證：狀態列顯示了掃描進度訊息
        mock_statusbar.showMessage.assert_called()
        msg = mock_statusbar.showMessage.call_args[0][0]
        assert "視覺測速掃描中" in msg
        assert "vision" in msg
