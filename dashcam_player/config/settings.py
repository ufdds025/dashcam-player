from __future__ import annotations

from PyQt5.QtCore import QByteArray, QSettings

from dashcam_player.models.capture_settings import CaptureSettings
from dashcam_player.models.gps_roi import GPSROISetting


class AppSettings:
    """應用設定持久化（使用 QSettings）"""

    ORGANIZATION = "DashcamTools"
    APPLICATION = "DashcamPlayer"

    def __init__(self) -> None:
        self._store = QSettings(self.ORGANIZATION, self.APPLICATION)

    # ---- 一般設定 ----

    @property
    def last_open_dir(self) -> str:
        return self._store.value("last_open_dir", "", type=str)

    @last_open_dir.setter
    def last_open_dir(self, value: str) -> None:
        self._store.setValue("last_open_dir", value)

    @property
    def last_output_dir(self) -> str:
        return self._store.value("last_output_dir", "", type=str)

    @last_output_dir.setter
    def last_output_dir(self, value: str) -> None:
        self._store.setValue("last_output_dir", value)

    @property
    def window_geometry(self) -> QByteArray | None:
        data = self._store.value("window_geometry")
        return data if isinstance(data, QByteArray) else None

    @window_geometry.setter
    def window_geometry(self, value: QByteArray) -> None:
        self._store.setValue("window_geometry", value)

    @property
    def window_state(self) -> QByteArray | None:
        data = self._store.value("window_state")
        return data if isinstance(data, QByteArray) else None

    @window_state.setter
    def window_state(self, value: QByteArray) -> None:
        self._store.setValue("window_state", value)

    # ---- Capture 設定 ----

    def load_capture_settings(self) -> CaptureSettings:
        data: dict = {}
        skip_keys = {"start_frame", "end_frame", "start_time", "end_time", "window_geometry"}
        for key, field in CaptureSettings.__dataclass_fields__.items():
            if key in skip_keys:
                continue
            val = self._store.value(f"capture/{key}")
            if val is not None:
                # QSettings 會把 bool 存成字串 "true"/"false"，自動轉回 bool
                # 因 from __future__ import annotations，field.type 可能是字串 'bool'
                field_type = field.type
                if isinstance(val, str) and (field_type is bool or field_type == "bool"):
                    val = val.lower() in ("true", "1", "yes")
                data[key] = val
        return CaptureSettings.from_dict(data)

    def save_capture_settings(self, settings: CaptureSettings) -> None:
        for key, val in settings.to_dict().items():
            self._store.setValue(f"capture/{key}", val)

    # ---- GPS ROI 設定 ----

    def _roi_key(self, resolution_w: int, resolution_h: int, file_format: str) -> str:
        fmt = file_format.upper().lstrip(".")
        return f"gps_roi/{resolution_w}x{resolution_h}_{fmt}"

    def load_gps_roi(
        self, resolution_w: int, resolution_h: int, file_format: str
    ) -> GPSROISetting | None:
        key = self._roi_key(resolution_w, resolution_h, file_format)
        roi_x = self._store.value(f"{key}/roi_x")
        if roi_x is None:
            return None
        roi_y = self._store.value(f"{key}/roi_y")
        roi_w = self._store.value(f"{key}/roi_w")
        roi_h = self._store.value(f"{key}/roi_h")
        if None in (roi_y, roi_w, roi_h):
            return None

        return GPSROISetting(
            resolution_w=resolution_w,
            resolution_h=resolution_h,
            file_format=file_format,
            roi_x=float(roi_x),
            roi_y=float(roi_y),
            roi_w=float(roi_w),
            roi_h=float(roi_h),
            corner=self._store.value(f"{key}/corner", "", type=str),
            layout=self._store.value(f"{key}/layout", "", type=str),
            source=self._store.value(f"{key}/source", "user_adjusted", type=str),
            confidence=float(self._store.value(f"{key}/confidence", 1.0, type=float)),
            timestamp=float(self._store.value(f"{key}/timestamp", 0.0, type=float)),
        )

    def save_gps_roi(self, setting: GPSROISetting) -> None:
        key = self._roi_key(setting.resolution_w, setting.resolution_h, setting.file_format)
        self._store.setValue(f"{key}/roi_x", setting.roi_x)
        self._store.setValue(f"{key}/roi_y", setting.roi_y)
        self._store.setValue(f"{key}/roi_w", setting.roi_w)
        self._store.setValue(f"{key}/roi_h", setting.roi_h)
        self._store.setValue(f"{key}/corner", setting.corner)
        self._store.setValue(f"{key}/layout", setting.layout)
        self._store.setValue(f"{key}/source", setting.source)
        self._store.setValue(f"{key}/confidence", setting.confidence)
        self._store.setValue(f"{key}/timestamp", setting.timestamp)
        self._store.sync()

    def get_all_gps_roi_keys(self) -> list[str]:
        self._store.sync()
        all_keys: list[str] = []
        for group in self._store.childGroups():
            if group.startswith("gps_roi/"):
                all_keys.append(group)
        return all_keys

    # ---- GPS 訊號過濾 ----

    def load_gps_filter(self) -> tuple[bool, float]:
        """讀取 GPS 飄移過濾設定 (enabled, max_distance_m)。"""
        enabled = self._store.value("capture/gps_filter_enabled", False)
        if isinstance(enabled, str):
            enabled = enabled.lower() in ("true", "1", "yes")
        max_d = self._store.value("capture/gps_filter_max_distance", 2000.0, type=float)
        try:
            max_d = float(max_d)
        except (TypeError, ValueError):
            max_d = 2000.0
        if max_d <= 0:
            max_d = 2000.0
        return bool(enabled), max_d
