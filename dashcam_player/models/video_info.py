from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class VideoInfo:
    path: str
    duration: float = 0.0
    fps: float = 30.0
    frame_count: int = 0
    width: int = 0
    height: int = 0
    has_audio: bool = False
    video_codec: str = ""
    audio_codec: str | None = None
    format_name: str = ""
    fourcc: str = ""
    hw_backends: list[tuple[str, str]] = field(default_factory=list)
    has_gps: bool = False  # 是否有 GPS 資料
    gps_track_count: int = 0  # GPS 軌跡點數量
    probe_info: dict | None = None  # ffprobe 結果快取，避免重複 probe

    @property
    def display_name(self) -> str:
        return os.path.basename(self.path)

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "duration": self.duration,
            "fps": self.fps,
            "frame_count": self.frame_count,
            "width": self.width,
            "height": self.height,
            "has_audio": self.has_audio,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "format_name": self.format_name,
            "fourcc": self.fourcc,
            "hw_backends": self.hw_backends,
            "has_gps": self.has_gps,
            "gps_track_count": self.gps_track_count,
            "probe_info": self.probe_info,
        }

    @classmethod
    def from_dict(cls, data: dict) -> VideoInfo:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
