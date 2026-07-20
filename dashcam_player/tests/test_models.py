from __future__ import annotations

from dashcam_player.models.capture_settings import CaptureSettings
from dashcam_player.models.video_info import VideoInfo


class TestVideoInfo:
    def test_defaults(self) -> None:
        info = VideoInfo(path="/test/video.mp4")
        assert info.path == "/test/video.mp4"
        assert info.duration == 0.0
        assert info.fps == 30.0
        assert info.frame_count == 0
        assert info.display_name == "video.mp4"

    def test_to_dict_roundtrip(self) -> None:
        info = VideoInfo(
            path="/test/video.mp4",
            duration=120.5,
            fps=29.97,
            frame_count=3600,
            has_audio=True,
            video_codec="h264",
            width=1920,
            height=1080,
        )
        d = info.to_dict()
        restored = VideoInfo.from_dict(d)
        assert restored.path == info.path
        assert restored.duration == info.duration
        assert restored.fps == info.fps
        assert restored.has_audio == info.has_audio
        assert restored.resolution == "1920x1080"


class TestCaptureSettings:
    def test_defaults(self) -> None:
        s = CaptureSettings()
        assert s.fps == 30
        assert s.prefix == "capture"
        assert s.img_format == "JPEG"
        assert s.jpeg_quality == 95

    def test_to_dict_roundtrip(self) -> None:
        s = CaptureSettings(fps=15, prefix="test", jpeg_quality=80)
        d = s.to_dict()
        restored = CaptureSettings.from_dict(d)
        assert restored.fps == 15
        assert restored.prefix == "test"
        assert restored.jpeg_quality == 80


class TestVideoInfoProbe:
    def test_probe_info_field(self) -> None:
        info = VideoInfo(path="/test/video.mp4")
        assert info.probe_info is None
        info.probe_info = {"has_audio": True, "video_codec": "h264"}
        assert info.probe_info["has_audio"] is True

    def test_probe_info_in_to_dict(self) -> None:
        info = VideoInfo(
            path="/test/video.mp4",
            duration=100.0,
            hw_backends=[("CUDA", "Test GPU")],
            probe_info={"has_audio": True},
        )
        d = info.to_dict()
        assert "hw_backends" in d
        assert d["hw_backends"] == [("CUDA", "Test GPU")]
        assert "probe_info" in d
        assert d["probe_info"]["has_audio"] is True
