from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dashcam_player.models.resolution import Resolution


@dataclass
class ScreenshotSettings:
    """單張/批次截圖設定"""

    fps: int = 30
    prefix: str = "capture"
    img_format: str = "JPEG"
    jpeg_quality: int = 95
    resolution: Resolution = Resolution.ORIGINAL
    output_dir: str = "captures"


@dataclass
class ExportSettings:
    """影片擷取設定"""

    video_format: str = "MP4"
    keep_audio: bool = True
    use_stream_copy: bool = True
    video_codec: str = "libx264"
    video_crf: int = 18
    video_preset: str = "medium"
    video_scale_width: str = "原始"


@dataclass
class GpsDisplaySettings:
    """GPS 顯示設定"""

    show_gps_map: bool = False
    skip_gps_overlay: bool = False
    map_provider: str = "osm"
    google_maps_api_key: str = ""
    gps_filter_enabled: bool = False
    gps_filter_max_distance: float = 2000.0


# 所有 sub-dataclass 欄位名的並集，用於 __getattr__ 委派
_SUB_FIELD_NAMES: frozenset[str] = frozenset(
    f.name
    for cls in (ScreenshotSettings, ExportSettings, GpsDisplaySettings)
    for f in cls.__dataclass_fields__.values()
)


@dataclass
class CaptureSettings:
    """應用程式擷取設定（組合式）。

    向後相容：settings.fps 等平鋪存取仍可運作（經 __getattr__ 委派）。
    建構時支援兩種方式：
      - 新式：CaptureSettings(screenshot=ScreenshotSettings(fps=15))
      - 舊式：CaptureSettings(fps=15, prefix="test")  ← 自動分派到正確 sub-dataclass
    """

    screenshot: ScreenshotSettings = field(default_factory=ScreenshotSettings)
    export: ExportSettings = field(default_factory=ExportSettings)
    gps: GpsDisplaySettings = field(default_factory=GpsDisplaySettings)

    def __init__(self, **kwargs: object) -> None:
        """支援平鋪 kwarg 建構（向後相容舊版 API）。"""
        s_kw: dict = {}
        e_kw: dict = {}
        g_kw: dict = {}
        for k, v in kwargs.items():
            if k in ScreenshotSettings.__dataclass_fields__:
                s_kw[k] = v
            elif k in ExportSettings.__dataclass_fields__:
                e_kw[k] = v
            elif k in GpsDisplaySettings.__dataclass_fields__:
                g_kw[k] = v
            else:
                raise TypeError(f"CaptureSettings.__init__() got unexpected keyword argument '{k}'")
        object.__setattr__(self, "screenshot", ScreenshotSettings(**s_kw))
        object.__setattr__(self, "export", ExportSettings(**e_kw))
        object.__setattr__(self, "gps", GpsDisplaySettings(**g_kw))

    # ---- 向後相容：平鋪存取委派 ----

    def __getattr__(self, name: str) -> Any:
        # __dataclass_fields__ 在 __init__ 期間就存在，不會觸發 __getattr__
        if name in _SUB_FIELD_NAMES:
            for sub in (self.screenshot, self.export, self.gps):
                if hasattr(sub, name):
                    return getattr(sub, name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    def __setattr__(self, name: str, value: Any) -> None:
        # dataclass 自身的欄位（screenshot/export/gps）走正常流程
        if name in ("screenshot", "export", "gps"):
            super().__setattr__(name, value)
            return
        # sub-dataclass 欄位：委派寫入
        if name in _SUB_FIELD_NAMES:
            for sub in (self.screenshot, self.export, self.gps):
                if hasattr(sub, name):
                    setattr(sub, name, value)
                    return
        super().__setattr__(name, value)

    # ---- 序列化（保持 QSettings key 不變） ----

    def to_dict(self) -> dict:
        """展平為單層 dict，key 與舊版 CaptureSettings 完全相容。"""
        result: dict = {}
        for sub in (self.screenshot, self.export, self.gps):
            for f in sub.__dataclass_fields__.values():
                val = getattr(sub, f.name)
                if isinstance(val, Resolution):
                    val = val.value
                result[f.name] = val
        return result

    def replace(self, **kwargs: object) -> CaptureSettings:
        """Return a new CaptureSettings with the given fields replaced.

        Accepts flat field names (e.g. ``fps=15``) and routes them to the
        correct sub-dataclass automatically.
        """
        d = self.to_dict()
        d.update(kwargs)
        return CaptureSettings(**d)

    @classmethod
    def from_dict(cls, data: dict) -> CaptureSettings:
        """從展平 dict 建構（相容舊版 QSettings 格式）。"""
        s_kw: dict = {}
        e_kw: dict = {}
        g_kw: dict = {}
        for k, v in data.items():
            if k in ScreenshotSettings.__dataclass_fields__:
                s_kw[k] = v
            elif k in ExportSettings.__dataclass_fields__:
                e_kw[k] = v
            elif k in GpsDisplaySettings.__dataclass_fields__:
                g_kw[k] = v
        # Resolution 字串 → Enum
        if "resolution" in s_kw and isinstance(s_kw["resolution"], str):
            s_kw["resolution"] = Resolution.from_value(s_kw["resolution"])
        inst = cls.__new__(cls)
        object.__setattr__(inst, "screenshot", ScreenshotSettings(**s_kw))
        object.__setattr__(inst, "export", ExportSettings(**e_kw))
        object.__setattr__(inst, "gps", GpsDisplaySettings(**g_kw))
        return inst
