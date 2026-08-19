"""地圖顯示元件 (QWebEngineView + Leaflet.js)

提供左右分割模式下的右側 GPS 地圖顯示，
支援 OSM / Google Maps 切換、軌跡繪製與即時位置更新。
"""

from __future__ import annotations

from PyQt5.QtWebEngineWidgets import QWebEngineSettings, QWebEngineView
from PyQt5.QtWidgets import QVBoxLayout, QWidget

from dashcam_player.utils.log import get_logger
from dashcam_player.utils.osm_map import generate_map_html

log = get_logger()

# OSM 圖磚使用政策 (https://operations.osmfoundation.org/policies/tiles/)
# 要求請求需帶「可識別身份」的 User-Agent，否則容易被判定為濫用流量而擋掉。
# QtWebEngine 預設的 User-Agent 看起來像一般 Chrome，不會標示是哪個 App。
_MAP_USER_AGENT = "DashcamPlayer/1.0 (PyQt5 QtWebEngine; dashcam GPS map viewer)"


class MapView(QWidget):
    """Leaflet 地圖顯示元件

    使用方式:
        map_view = MapView()
        map_view.load_tracks(gps_tracks)  # 載入軌跡
        map_view.update_position(lat, lon)  # 更新當前位置
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tracks: list[dict] = []
        self._provider: str = "osm"
        self._google_maps_key: str = ""
        self._page_ready: bool = False

        # WebEngineView
        self.web_view = QWebEngineView()
        self.web_view.setMinimumWidth(320)
        self.web_view.loadFinished.connect(self._on_load_finished)

        # 允許本地 HTML 存取遠端 URL（OSM 圖磚）
        settings = self.web_view.page().settings()
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)

        # 帶上可識別身份的 User-Agent，避免圖磚請求被 OSM 判定為濫用流量而擋掉
        self.web_view.page().profile().setHttpUserAgent(_MAP_USER_AGENT)

        # 佈局
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.web_view)

    # ------------------------------------------------------------------
    # 公開方法
    # ------------------------------------------------------------------

    def load_tracks(
        self, tracks: list[dict], provider: str = "osm", google_maps_key: str = ""
    ) -> None:
        """載入 GPS 軌跡並重新繪製地圖"""
        self._tracks = tracks
        self._provider = provider
        self._google_maps_key = google_maps_key
        self._render_map()

    def update_position(self, lat: float, lon: float) -> None:
        """更新當前位置標記"""
        if not self._page_ready:
            return
        js = f"updatePosition({lat}, {lon});"
        self.web_view.page().runJavaScript(js)

    def clear(self) -> None:
        """清除地圖內容"""
        self._tracks = []
        self._render_map()

    def set_provider(self, provider: str, google_maps_key: str = "") -> None:
        """切換地圖來源"""
        self._provider = provider
        self._google_maps_key = google_maps_key
        if self._tracks:
            self._render_map()

    # ------------------------------------------------------------------
    # 內部方法
    # ------------------------------------------------------------------

    def _render_map(self) -> None:
        """產生地圖 HTML 並載入"""
        self._page_ready = False
        html = generate_map_html(
            tracks=self._tracks,
            provider=self._provider,
            google_maps_key=self._google_maps_key,
        )
        self.web_view.setHtml(html)

    def _on_load_finished(self, ok: bool) -> None:
        """頁面載入完成 callback"""
        self._page_ready = ok
        if ok:
            log.debug("地圖 HTML 載入完成")
        else:
            log.warning("地圖 HTML 載入失敗")

    def closeEvent(self, event) -> None:
        self.web_view.loadFinished.disconnect(self._on_load_finished)
        self.web_view.setHtml("")
        super().closeEvent(event)
