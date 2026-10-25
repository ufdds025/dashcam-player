from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt5.QtCore import QObject, QThread, pyqtSignal

from dashcam_player.utils.gps_parser import GPSParser, GPSPoint, interpolate_gps
from dashcam_player.utils.log import get_logger

if TYPE_CHECKING:
    from dashcam_player.config.settings import AppSettings

log = get_logger()

# 背景載入執行緒的「保留區」：close() 不再阻塞主執行緒等待 OCR 結束時，
# 此集合持有執行緒強參考，避免 GPSManager 被 GC 時於執行緒仍運行中將其摧毀
# （否則 Qt 噴 "QThread: Destroyed while thread is still running"）。
# 執行緒 run() 結束（finished 信號）後自動移出。
_thread_graveyard: set = set()


def _retire_thread(thread) -> None:
    _thread_graveyard.discard(thread)


class _GPSLoadThread(QThread):
    """內部 GPS 背景載入執行緒，封裝於 GPSManager 內部不向外暴露。"""

    # (video_path, has_gps, track_count, tracks)
    # ⚠️ 不可命名為 finished——會遮蔽 QThread 內建信號，執行緒崩潰時完全靜默
    result_ready = pyqtSignal(str, bool, int, object)

    def __init__(self, parser: GPSParser, run_ocr: bool = False, parent: object | None = None) -> None:
        super().__init__(parent)
        self.parser = parser
        self._run_ocr = run_ocr
        self._stop = False
        # 執行緒無 parent，由 graveyard 持有參考；結束後自動清除
        self.setParent(None)
        _thread_graveyard.add(self)
        self.finished.connect(lambda: _retire_thread(self))

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        if self._stop:
            return
        # 步驟 1: 快速檢查外部檔案 + embedded metadata
        has_gps = self.parser.has_gps_data()
        if self._stop:
            return
        # 步驟 2: 若無外部 GPS，且允許 OCR，才跑 OCR 探測
        #         skip_ocr=True 時不載入 PaddleOCR（避免不必要的模型載入）
        if not has_gps and self._run_ocr:
            log.info("GPS 偵測：無外部檔案/embedded metadata，開始 OCR 探測...")
            has_gps = self.parser.has_ocr_gps()
            if has_gps:
                log.info("GPS 偵測：OCR 探測成功，找到 GPS 資料")
            else:
                log.info("GPS 偵測：OCR 探測失敗，未找到 GPS 資料")
        elif not has_gps:
            log.info("GPS 偵測：無外部 GPS 且跳過 OCR（skip_ocr=True）")
        else:
            log.info("GPS 偵測：找到外部檔案/embedded metadata GPS")
        if self._stop:
            return
        # 步驟 3: 確認有 GPS 才做全片提取
        track_count = 0
        tracks: list[GPSPoint] = []
        if has_gps:
            tracks = self.parser.extract_gps_tracks()
            track_count = len(tracks)
            log.info(f"GPS 偵測：全片提取完成，{track_count} 個軌跡點")
        if self._stop:
            return
        self.result_ready.emit(self.parser.video_path, has_gps, track_count, tracks)


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
        self._has_speed_data: bool = False  # 快取：軌跡中是否有 speed 欄位（避免每幀 O(n) 掃描）
        self._load_thread: _GPSLoadThread | None = None
        self._closed: bool = False

    # ==================================================================
    # 背景載入
    # ==================================================================

    def start_background_load(self, run_ocr: bool = False) -> None:
        """啟動背景 GPS 載入執行緒，不阻塞 UI。

        run_ocr=False（預設）: 僅检查外部檔案 + embedded metadata，不跑 OCR
                             不載入 PaddleOCR
        run_ocr=True: 額外跑 OCR 探測（偵測畫面文字 GPS），載入時輸出狀態訊息

        載入完成後觸發 gps_loaded 信號。
        _skip_ocr 語意：True=跳過 OCR，False=允許 OCR；與 run_ocr 相反。
        """
        if self._closed:
            return
        # 防止重複啟動：若已有背景執行緒在跑，直接返回
        if self._load_thread and self._load_thread.isRunning():
            log.debug("GPS 背景載入已在進行中，忽略重複請求")
            return
        if run_ocr:
            self.gps_status_message.emit("正在啟動 PaddleOCR 辨識引擎，請稍候...")
        else:
            self.gps_status_message.emit("正在偵測 GPS 軌跡...")
        # 修正：run_ocr 與 _skip_ocr 語意相反（run=True 意味不 skip）
        self._parser._skip_ocr = not run_ocr
        # 同步更新 detector 的旗標，避免透由 has_gps_data() 間接調 OCR 時不一致
        try:
            self._parser._detector._skip_ocr = not run_ocr
        except AttributeError:
            pass
        self._load_thread = _GPSLoadThread(self._parser, run_ocr=run_ocr, parent=self)
        self._load_thread.result_ready.connect(self._on_load_finished)
        self._load_thread.start()
        log.info(f"GPS 背景載入啟動: run_ocr={run_ocr} (parser._skip_ocr={self._parser._skip_ocr})")

    def start_ocr_detection(self) -> None:
        """在已有 GPSManager 的情況下，額外跑 OCR 探測。

        供使用者手動開啟「顯示 GPS 軌跡」時呼叫。
        若已載入過，先清除舊結果再重新探測。
        """
        if self._closed:
            return
        self._tracks_loaded = False
        self._tracks = []
        self._has_speed_data = False
        self.start_background_load(run_ocr=True)

    def _on_load_finished(
        self, video_path: str, has_gps: bool, track_count: int, tracks: list
    ) -> None:
        if self._closed:
            return
        self._tracks_loaded = True
        # 直接接收背景執行緒回傳的軌跡，避免二次 extract_gps_tracks() 全片掃描
        self._tracks = tracks if tracks else []
        self._has_speed_data = any(p.speed is not None for p in self._tracks)
        log.info(f"GPS 載入完成: has_gps={has_gps}, track_count={track_count}")
        self.gps_status_message.emit(
            f"GPS 偵測完成{'（找到 GPS 軌跡）' if has_gps else '（未找到 GPS 軌跡）'}"
        )
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

    def get_speed_at_time(self, seconds: float) -> float | None:
        """取得指定時間的 GPS 速度（km/h，線性插值）。

        回傳 km/h 或 None。NMEA 的節(knots)會自動轉為 km/h (×1.852)。
        若軌跡點無 speed 欄位則回傳 None，不會觸發 OCR。
        """
        if not self._tracks_loaded or not self._tracks:
            return None
        # 使用快取的 _has_speed_data（O(1)），避免每幀 O(n) 掃描全部軌跡點
        if not self._has_speed_data:
            return None
        # 二分搜尋最近區間，插值 speed（knots → km/h）
        import bisect

        # 若時間在範圍外，回傳端點速度
        if seconds <= self._tracks[0].time:
            s = self._tracks[0].speed
            return s * 1.852 if s is not None else None
        if seconds >= self._tracks[-1].time:
            s = self._tracks[-1].speed
            return s * 1.852 if s is not None else None
        idx = bisect.bisect_left(self._tracks, seconds, key=lambda p: p.time)
        if idx == 0 or idx >= len(self._tracks):
            s = self._tracks[idx if idx < len(self._tracks) else -1].speed
            return s * 1.852 if s is not None else None
        p1, p2 = self._tracks[idx - 1], self._tracks[idx]
        # 若兩點皆無 speed，無法插值
        if p1.speed is None and p2.speed is None:
            return None
        if p1.speed is None:
            return p2.speed * 1.852  # type: ignore[operator]
        if p2.speed is None:
            return p1.speed * 1.852
        dt = p2.time - p1.time
        if dt < 1e-9:
            return p1.speed * 1.852
        ratio = (seconds - p1.time) / dt
        # knots → km/h 插值後轉換
        speed_knots = p1.speed + (p2.speed - p1.speed) * ratio  # type: ignore[operator]
        return speed_knots * 1.852

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
        # 若無外部 GPS，用單幀輕量探測 OCR
        if not has_gps:
            self.gps_status_message.emit("正在啟動 PaddleOCR 辨識引擎，請稍候...")
            has_gps = self._parser.has_ocr_gps()
        # 確認有 GPS 才做全片提取
        if has_gps:
            tracks = self._parser.extract_gps_tracks()
            if tracks:
                self._tracks = tracks
                self._tracks_loaded = True
                self._has_speed_data = any(p.speed is not None for p in self._tracks)
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
                self._load_thread.result_ready.disconnect()
            except (TypeError, RuntimeError):
                pass
            # 不阻塞主執行緒等待 OCR（全片提取可能耗時數十秒）。
            # 改以短 timeout join；逾時則交由背景執行緒自然結束（不強制 terminate，
            # 避免跳過 OCR/VideoCapture 的 finally 釋放造成資源洩漏）。
            if not self._load_thread.wait(500):
                log.warning("GPS 載入執行緒未在 500ms 內停止，交由背景自然結束")
            self._load_thread = None
        self._parser.close_ocr()
        self._tracks.clear()
        self._tracks_loaded = False
