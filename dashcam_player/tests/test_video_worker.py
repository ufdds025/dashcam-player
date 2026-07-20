from __future__ import annotations

from dashcam_player.core.video_worker import _CmdType, _Command


class TestVideoWorkerCommandQueue:
    def test_open_drains_and_idles(self, video_worker) -> None:
        w = video_worker
        w._cmd_queue.put(_Command(_CmdType.PLAY))
        w.start = lambda: None
        w.open("dummy.mp4")
        assert w._cmd_queue.qsize() == 1
        assert w._cmd_queue.get_nowait().type == _CmdType.STOP

    def test_seek_enqueues_seek_command(self, video_worker) -> None:
        w = video_worker
        w.seek(42)
        cmd = w._cmd_queue.get_nowait()
        assert cmd.type == _CmdType.SEEK
        assert cmd.frame == 42

    def test_seek_clamps_out_of_range(self, video_worker) -> None:
        w = video_worker
        w.seek(9999)
        cmd = w._cmd_queue.get_nowait()
        assert cmd.frame == 99

    def test_step_forward_and_backward(self, video_worker) -> None:
        w = video_worker
        w.step_forward()
        cmd = w._cmd_queue.get_nowait()
        assert cmd.type == _CmdType.STEP_FWD
        w.step_backward()
        cmd = w._cmd_queue.get_nowait()
        assert cmd.type == _CmdType.STEP_BWD

    def test_set_play_enqueues_play_pause(self, video_worker) -> None:
        w = video_worker
        w.set_play(True)
        assert w._cmd_queue.get_nowait().type == _CmdType.PLAY
        w.set_play(False)
        assert w._cmd_queue.get_nowait().type == _CmdType.PAUSE

    def test_close_enqueues_stop(self, video_worker) -> None:
        w = video_worker
        w.close()
        assert w._stop is True
        assert w._cmd_queue.get_nowait().type == _CmdType.STOP

    def test_no_dual_state_variables(self, video_worker) -> None:
        w = video_worker
        assert not hasattr(w, "_play")
        assert not hasattr(w, "_seek_frame")

    def test_release_cap_async_enqueues(self, video_worker) -> None:
        w = video_worker
        w.release_cap_async()
        assert w._cmd_queue.get_nowait().type == _CmdType.RELEASE_CAP

    def test_nearest_keyframe_returns_target_without_table(self, video_worker) -> None:
        w = video_worker
        assert w._nearest_keyframe_idx(50) == 50

    def test_nearest_keyframe_snaps_backward(self, video_worker) -> None:
        w = video_worker
        w._fps = 10.0
        w._total_frames = 100
        w._keyframe_times = [0.0, 1.0, 2.0, 3.0]
        assert w._nearest_keyframe_idx(25) == 20
        assert w._nearest_keyframe_idx(5) == 0
        assert w._nearest_keyframe_idx(35) == 30

    def test_nearest_keyframe_clamps(self, video_worker) -> None:
        w = video_worker
        w._fps = 10.0
        w._total_frames = 100
        w._keyframe_times = [0.0, 1.0]
        assert 0 <= w._nearest_keyframe_idx(99) <= 99

