from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt5.QtCore import QObject, QThread, pyqtSignal

from dashcam_player.utils.gps_parser import GPSParser, GPSPoint, interpolate_gps
from dashcam_player.utils.log import get_logger

if TYPE_CHECKING:
    from dashcam_player.config.settings import AppSettings

log = get_logger()


class _GPSLoadThread(QThread):
    """內部 GPS 背景載入執行緒，封裝於 GPSManager 內部不向外暴露。"""

    # (video_path, has_gps, track_count, tracks)
    finished = pyqtSignal(str, bool, int, object)

    def __init__(self, parser: GPSParser, parent: object | None = None) -> None:
        super().__init__(parent)
        self.parser = parser
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        if self._stop:
            return
        # 步驟 1: 快速檢查外部檔案 + embedded metadata
        has_gps = self.parser.has_gps_data()
        if self._stop:
            return
        # 步驟 2: 若無外部 GPS 且 skip_ocr=True，用單幀輕量探測 OCR
        #         不可直接呼叫 extract_gps_tracks()，否則會觸發全片掃描
        if not has_gps and getattr(self.parser, "_skip_ocr", False):
            has_gps = self.parser._has_ocr_gps()
            if self._stop:
                return
        # 步驟 3: 確認有 GPS 才做全片提取
        track_count = 0
        tracks: list[GPSPoint] = []
        if has_gps:
            tracks = self.parser.extract_gps_tracks()
            track_count = len(tracks)
        if self._stop:
            return
        self.finished.emit(self.parser.video_path, has_gps, track_count, tracks)


class GPSManager(QObject):
    """獨立 GPS 管理器，負責載入、解析、插值與資源管理。

    Controller 透過此類別存取所有 GPS 功能，不直接操作 GPSParser。
    背景載入執行緒、OCR 資源管理皆封裝於內部。
    """

    gps_loaded = pyqtSignal(str, bool, int)  # (video_path, has_gps, track_count)
    gps_status_message = pyqtSignal(str)  # 狀態訊息
    gps_roi_setup_needed = pyqtSignal(str, int, int, str)  # (path, width, height, format)

    def __init__(
        self,
        video_path: str,
        skip_ocr: bool = True,
        app_settings: AppSettings | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._video_path = video_path
        self._app_settings = app_settings
        self._parser: GPSParser = GPSParser(
            video_path, skip_ocr=skip_ocr, app_settings=app_settings
        )
        self._tracks: list[GPSPoint] = []
        self._tracks_loaded: bool = False
        self._load_thread: _GPSLoadThread | None = None
        self._closed: bool = False

    # ==================================================================
    # 背景載入
    # ==================================================================

    def start_background_load(self, run_ocr: bool = False) -> None:
        """啟動背景 GPS 載入執行緒，不阻塞 UI。

        run_ocr=False（預設）: 僅检查外部檔案 + embedded metadata，不跑 OCR
        run_ocr=True: 額外跑 OCR 探測（偵測畫面文字 GPS）

        載入完成後觸發 gps_loaded 信號。
        """
        if self._closed:
            return
        self.gps_status_message.emit("正在偵測 GPS 軌跡...")
        self._parser._skip_ocr = run_ocr
        self._load_thread = _GPSLoadThread(self._parser, parent=self)
        self._load_thread.finished.connect(self._on_load_finished)
        self._load_thread.start()

    def start_ocr_detection(self) -> None:
        """在已有 GPSManager 的情況下，額外跑 OCR 探測。

        供使用者手動開啟「顯示 GPS 軌跡」時呼叫。
        若已載入過，先清除舊結果再重新探測。
        """
        if self._closed:
            return
        self._tracks_loaded = False
        self._tracks = []
        self.start_background_load(run_ocr=True)

    def _on_load_finished(
        self, video_path: str, has_gps: bool, track_count: int, tracks: list
    ) -> None:
        if self._closed:
            return
        self._tracks_loaded = True
        # 直接接收背景執行緒回傳的軌跡，避免二次 extract_gps_tracks() 全片掃描
        self._tracks = tracks if tracks else []
        log.info(f"GPS 載入完成: has_gps={has_gps}, track_count={track_count}")
        self.gps_loaded.emit(video_path, has_gps, track_count)

        # 自動偵測失敗且無 GPS → 觸發 ROI 設定 UI
        if not has_gps and self._parser._roi_auto_failed is not None:
            w, h, fmt = self._parser._roi_auto_failed
            self.gps_roi_setup_needed.emit(video_path, w, h, fmt)

        self._load_thread = None

    # ==================================================================
    # 同步查詢
    # ==================================================================

    def get_position_at_time(self, seconds: float) -> tuple[float, float] | None:
        """取得指定時間的 GPS 座標（線性插值）。

        回傳 (lat, lon) 或 None（無資料／尚未載入完成）。
        不會觸發 OCR 或阻塞等待背景載入。
        """
        if not self._tracks_loaded or not self._tracks:
            return None
        return interpolate_gps(seconds, self._tracks)

    def extract_tracks_sync(self) -> list[GPSPoint]:
        """同步提取完整 GPS 軌跡點。

        若 skip_ocr=True 且無外部／embedded GPS，
        此方法會先用單幀探測 OCR，確認有 GPS 才做全片提取。
        批次截圖等需要完整軌跡的場景使用。

        回傳內部軌跡的唯讀參考（呼叫端不應修改）。
        """
        if self._tracks_loaded:
            return self._tracks
        if self._tracks:
            return self._tracks
        # 快速檢查外部檔案 + embedded metadata
        has_gps = self._parser.has_gps_data()
        # 若無外部 GPS 且 skip_ocr=True，用單幀輕量探測
        if not has_gps and getattr(self._parser, "_skip_ocr", False):
            has_gps = self._parser._has_ocr_gps()
        # 確認有 GPS 才做全片提取
        if has_gps:
            tracks = self._parser.extract_gps_tracks()
            if tracks:
                self._tracks = tracks
                self._tracks_loaded = True
        return self._tracks

    def is_loaded(self) -> bool:
        """背景載入是否已完成（不論是否有 GPS 資料）。"""
        return self._tracks_loaded

    def has_gps_data(self) -> bool:
        """檢查當前影片是否有 GPS 資料。

        背景載入完成前透過 parser 快速偵測（外部檔案／embedded metadata），
        載入完成後以實際軌跡點為準。
        """
        if self._tracks_loaded:
            return len(self._tracks) > 0
        return self._parser.has_gps_data()

    def get_bounds(self) -> tuple[float, float, float, float] | None:
        """GPS 軌跡邊界框 (min_lat, min_lon, max_lat, max_lon)。"""
        if not self._tracks:
            return None
        lats = [p.lat for p in self._tracks]
        lons = [p.lon for p in self._tracks]
        return (min(lats), min(lons), max(lats), max(lons))

    def get_track_count(self) -> int:
        """GPS 軌跡點數量。"""
        if self._tracks_loaded:
            return len(self._tracks)
        return 0

    @property
    def has_roi_setting(self) -> bool:
        """檢查當前影片是否有已知的 ROI 設定"""
        if not self._app_settings or not self._video_path:
            return False
        import os

        w = self._parser._video_width
        h = self._parser._video_height
        fmt = self._parser._file_format or os.path.splitext(self._video_path)[1]
        if w <= 0 or h <= 0:
            return False
        roi = self._app_settings.load_gps_roi(w, h, fmt)
        return roi is not None

    def export_gpx(self, output_path: str) -> bool:
        """匯出 GPX 軌跡檔。"""
        if not self.has_gps_data():
            return False
        return self._parser.to_gpx(output_path)

    # ==================================================================
    # 測試注入
    # ==================================================================

    def set_parser(self, parser: GPSParser) -> None:
        """替換內部 parser（測試用 mock 注入）。"""
        self._parser = parser
        self._tracks = []
        self._tracks_loaded = False

    # ==================================================================
    # 資源管理
    # ==================================================================

    def close(self) -> None:
        """釋放所有資源（OCR、背景執行緒）。

        呼叫後此實例不可再使用，應建立新實例。
        """
        if self._closed:
            return
        self._closed = True
        if self._load_thread and self._load_thread.isRunning():
            self._load_thread.stop()
            try:
                self._load_thread.finished.disconnect()
            except (TypeError, RuntimeError):
                pass
            # EasyOCR 載入需 2-10 秒，最多等待 15 秒
            if not self._load_thread.wait(15000):
                log.warning("GPS 載入執行緒逾時，強制終止")
                self._load_thread.terminate()
                self._load_thread.wait(5000)
            self._load_thread = None
        self._parser.close_ocr()
        self._tracks.clear()
        self._tracks_loaded = False
