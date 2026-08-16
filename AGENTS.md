# AGENTS.md — 行車紀錄器影片播放與截圖工具

多模組 PyQt5 + OpenCV 應用程式。已從單一檔案重構為套件結構。

## 執行

```bash
pip install PyQt5 PyQtWebEngine opencv-python numpy pytesseract piexif requests
python -m dashcam_player.main
```

或直接執行 `啟動.bat`（會 `cd` 到專案根目錄後呼叫相同指令）。

Python 3.11（由 `__pycache__/` 推斷）。

## 專案結構

```
dashcam_player/                  # 含 __init__.py（regular package）
├── __init__.py                  # 套件標記（讓 dashcam_player 成為正式 package，從根目錄 pytest 可正常 import）
├── main.py                 # 入口點（init_log → QApplication → VideoPlayer）
├── core/
│   ├── controller.py       # Controller 層（薄 Orchestrator，委派 Manager）
│   ├── video_player.py     # 瘦 UI 主視窗（僅佈局與事件綁定）
│   ├── video_worker.py     # 播放背景執行緒（QThread）
│   ├── export_worker.py    # FFmpeg 匯出執行緒
│   ├── batch_capture.py    # 批次截圖背景執行緒
│   ├── gps_manager.py      # GPSManager（GPS 載入/解析/查詢，封裝於獨立 QObject）
│   └── managers/           # 四個 Manager（業務邏輯從 Controller 抽出）
│       ├── __init__.py     # 匯出 PlaybackManager / CaptureManager / ExportManager / GPSFacade
│       ├── playback_manager.py  # 播放控制 + GPS throttle
│       ├── capture_manager.py   # 單張/批次截圖 + 驗證
│       ├── export_manager.py    # FFmpeg 匯出 + 重試
│       └── gps_facade.py        # GPS 外觀模式（封裝 GPSManager）
├── models/
│   ├── video_info.py       # VideoInfo dataclass
│   ├── capture_settings.py # CaptureSettings 組合式 dataclass（Screenshot/Export/GPS + replace()）
│   ├── gps_roi.py          # GPSROISetting、GPSROIDetectionResult dataclass
│   └── resolution.py       # Resolution enum（解析度選項）
├── ui/
│   ├── styles.py           # 全域 UI 樣式常數
│   ├── map_view.py         # QWebEngineView + Leaflet.js 地圖元件
│   ├── playback_controls.py # PlaybackControls widget（重構後啟用）
│   └── dialogs/            # 對話框
│       ├── capture_settings.py
│       ├── gps_settings.py
│       ├── gps_roi_adjust.py   # GPS ROI 調整對話框（可拖曳/縮放選取框）
│       └── output_settings.py
├── utils/
│   ├── ffmpeg.py           # FFmpeg/FFprobe 工具
│   ├── cv_utils.py         # OpenCV 工具（硬體偵測、解析度、QImage 轉換）
│   ├── time_utils.py       # 時間格式/解析
│   ├── gps_parser.py       # GPS 資料提取（GPX/NMEA/SRT/內嵌 metadata/data track/OCR fallback）
│   ├── gps_ocr.py          # Tesseract GPS 辨識（從畫面文字提取座標，fallback 方案）
│   ├── gps_roi_detector.py # GPS ROI 自動偵測引擎（OpenCV 影像處理定位文字）
│   ├── gps_roi_templates.py # 內建 GPS 位置模板（左上/右上/左下/右下）
│   ├── osm_map.py          # OpenStreetMap 靜態縮圖 + Leaflet HTML 生成
│   └── log.py              # logging 模組封裝（RotatingFileHandler）
├── config/
│   └── settings.py         # QSettings 持久化
├── tests/
│   ├── conftest.py          # 共用 fixtures（video_worker、frame_1080p/480p/small）
│   ├── test_models.py       # dataclass 單元測試（含 replace()）
│   ├── test_utils.py        # 時間工具 + get_keyframe_positions + OSM 快取清理測試
│   ├── test_gps_parser.py   # GPS 解析/插值/GPX 匯出 + detector/extractor 策略測試
│   ├── test_gps_roi.py      # GPS ROI 測試（dataclass、templates、union ROI、bbox 篩選、corner 判斷）
│   ├── test_cv_utils.py     # OpenCV 工具測試（fourcc/解析度/overlay/imwrite/open_video_capture）
│   ├── test_video_worker.py # VideoWorker 指令佇列、keyframe 偵測、RELEASE_CAP 測試 (12 項)
│   ├── test_controller.py   # Controller 驗證方法 + 信號 relay 鏈測試 (23 項)
│   └── test_performance.py  # 效能基準（1080p 解碼 FPS / JPEG 寫入），RUN_PERF_TESTS=1 才執行
└── requirements.txt         # 實際位於 dashcam_player/（非根目錄）
```

## 測試

```bash
pytest                              # 從根目錄（自動經由 dashcam_player/__init__.py 解析 package）
pytest dashcam_player/tests/        # 或直接指定路徑
python -m pytest dashcam_player/tests/ -v   # 完整 verbose
```

## Lint / Type Check

```bash
ruff check dashcam_player/                        # 0 errors expected
python -m mypy dashcam_player/ --ignore-missing-imports   # 0 errors expected
```

- **ruff**（`pyproject.toml`）— lint + import sort，忽略 F821/B008/E501
- **mypy**（`pyproject.toml`）— 型別檢查，PyQt5 stub 噪音經 per-module `disable_error_code` 抑制（`union-attr`/`attr-defined`/`arg-type` 等）
- **CI**（`.github/workflows/ci.yml`）— GitHub Actions 執行 ruff + mypy + pytest

## 架構重點

- **Controller 層**（`core/controller.py`, 336 行）— 薄 Orchestrator，所有業務邏輯委派至 4 個 Manager；`VideoPlayer` 僅負責 UI 佈局與事件綁定
- **Manager 架構**（`core/managers/`）— 四個職責分明的 Manager：
  - `PlaybackManager`（148 行）— 播放控制（play/pause/seek/step/speed）+ GPS throttle
  - `CaptureManager`（181 行）— 單張截圖 + 批次截圖 + validate_batch_range / check_batch_conflict
  - `ExportManager`（135 行）— FFmpeg 匯出 + stream-copy 重試
  - `GPSFacade`（132 行）— GPS 查詢/疊加/ROI 對話（封裝 GPSManager）
- **Controller signals**（`controller.py`）— 定義 12 個 signal：`video_loaded(object)`、`load_failed(str)`、`error_occurred(str,str)`、`batch_progress(int,int)`、`batch_finished(int,int)`、`status_message(str)`、`export_finished(bool,str)`、`gps_position_updated(float,float)`、`gps_ready(str,bool,int)`、`frame_ready(object,int)`、`position_updated(float,int)`、`play_finished()`
- **Controller relay**（`controller.py`）— Manager 信號經 Controller relay 至 VideoPlayer，UI 不直接連接 Worker 或 Manager。`_on_frame_ready`、`_on_position_changed`、`_on_play_finished`、`_on_gps_loaded` 為 relay handlers。`error_occurred(title,message)` 由 `CaptureManager.error_occurred`（批次截圖失敗、單張儲存失敗）與 `VideoWorker.error_occurred`（seek 失敗等）relay，UI 統一彈 QMessageBox；`load_failed` 僅保留給「影片載入」失敗
- **Controller methods**（`controller.py`）— `skip(seconds)`（相對 seek）、`seek_to(frame_idx)`（seek 別名）、`set_playback_speed(speed)`（→ `PlaybackManager.set_playback_speed()`，儲存 `_playback_speed` 倍率、換影片時自動重套）、`validate_batch_range()`（驗證標記範圍）、`check_batch_conflict(out_dir, prefix, ext)`（回傳衝突檔案列表）、`validate_export_times(start_time, end_time)`（驗證匯出時間範圍）
- **`Controller.settings` property**（`controller.py`）— 回傳 `AppSettings` 實例（`self._app_settings`），用於視窗幾何、上次開啟目錄等持久化。`Controller.capture_settings` 為 `CaptureSettings` 唯一權威來源
- **`capture_single` 簽章**（`controller.py`）— 委派至 `CaptureManager.capture_single()`，**不接收 frame 參數**，Controller 內部從 `current_frame` property（→ `PlaybackManager._current_frame`）取得目前顯示的 frame，frame 為 None 時回傳 None。View 不持有 frame 副本
- **`Controller.current_frame` property**（`controller.py`）— 唯讀，回傳目前顯示的 frame（numpy ndarray 或 None）。View 端不持有 frame 副本，經由此 property 存取
- **`video_loaded` signal**（`controller.py`）— `pyqtSignal(object)`，emit `VideoInfo` 實例（非 `str`）
- **`CaptureSettings`**（`models/capture_settings.py`）— 組合式 dataclass：`ScreenshotSettings` / `ExportSettings` / `GpsDisplaySettings` + `__getattr__`/`__setattr__` 委派 + `replace()` instance method（取代 `dataclasses.replace()`，因自訂 `__init__(**kwargs)` 不相容）
- **`VideoPlayer.settings` property**（`video_player.py`）— `@property` 委託至 `self.ctrl.capture_settings`，Controller 為唯一權威來源。所有設定寫入經由 `Controller.update_capture_settings()` + `settings.replace()`，UI 不長期持有參照。**不可將 `@property` 插入 `__init__` 內部，否則會截斷 `__init__` 導致 `_init_ui()` 等不執行**
- **`VideoWorker(QThread)`** — 背景解碼，所有指令（play/pause/seek/step/stop/release_cap）經由 `queue.Queue[_Command]` 統一投遞，`run()` loop 單一消費，消除雙狀態變數競爭
  - **`RELEASE_CAP` + `_ensure_cap()`**（A9）：批次截圖前釋放主解碼器，播放時自動重建並 seek 回原位置
  - **Keyframe-aware seek**（A2）：`ffprobe` 載入關鍵影格時間表，`_nearest_keyframe_idx()` 跳最近 keyframe 再向前解碼，避免長 GOP 影片 seek 卡頓。**keyframe 表由 `open()` 啟動的 daemon thread 背景預載**（`get_keyframe_positions` 加 `-skip_frame nokey` 只輸出關鍵幀），表格就緒前 `_do_seek` 直接交給 cv2 內部 seek（不阻塞）；影片切換時以 `_path` 比對丟棄過期結果
  - **SEEK 維持播放狀態**：拖動進度條後繼續播放（不靜默暫停），單步（STEP_FWD/BWD）仍暫停
  - **`_target_fps` 換檔重置**：`run()` 載入新影片時重設 `_target_fps = 原始 fps`（1.0x），實際倍率由 `PlaybackManager._playback_speed` 於 `video_info_ready` 時重套，避免換影片後速度錯亂
  - **播放中「最新幀」丟棄**（`video_player.py`）：播放時 `_on_frame_ready` 只保留最新幀，由 15ms 單發 `QTimer` 統一刷新，避免 UI 跟不上時信號佇列堆積造成拖影/延遲；暫停/單步/seek 仍即時顯示
  - **`try/finally` cap 釋放**：`run()` 迴圈包在 `try/finally` 中，確保 `cap.release()` 在任何異常或提前 return 時都會執行
  - **⚠️ `queue.get()` 阻塞陷阱**：`run()` loop 內**不可**用 `queue.get(timeout=...)` 檢查指令，播放時佇列為空會每幀阻塞，導致幀率降至 ~20fps。須用 `queue.get_nowait()`（非阻塞）
- **`BatchCaptureWorker(QThread)`** — 完全背景化批次截圖，內部自行管理 VideoCapture，`run()` 使用 `try/finally` 確保 `cap.release()`
- **`ExportWorker(QThread)`** — 背景執行 FFmpeg subprocess
- **`GPSManager(QObject)`**（`gps_manager.py`）— 獨立 GPS 管理器，負責載入、解析、插值與資源管理。Controller 經由 `GPSFacade` 存取所有 GPS 功能，不直接操作 `GPSParser`
- **`GPSFacade`**（`core/managers/gps_facade.py`）— GPS 外觀模式，封裝 GPSManager 的查詢、疊加、匯出功能。提供 `has_roi_setting`/`is_loaded`/`get_track_count`/`has_manager` public properties 與 `manager` property。**信號生命週期由 `set_gps_manager()` 統一管理**（disconnect 舊 manager 的 `gps_loaded`/`gps_status_message`/`gps_roi_setup_needed` 再 connect 新的），Controller 不自行操作 GPS 信號。`gps_status_message` 經 `_on_gps_status_message` 轉發至 `status_message`
- **`AppSettings`**（`config/settings.py`）— 使用 QSettings 持久化視窗幾何、上次開啟目錄、擷取設定
- **滾輪縮放**（`video_player.py`）— 播放/暫停時皆可以滑鼠滾輪縮放 1.0x-5.0x，縮放中心定位於滑鼠位置；crop rect 自動 clamp 至影像邊界（邊緣貼邊）；開啟新影片時自動重置（播放切換不重置）；觸控板只報 pixelDelta 時自動改用 pixelDelta
- **硬體加速策略**（`cv_utils.py` A6）— `detect_hw_backends()` 偵測 CUDA/OpenCL；`configure_hw_acceleration()` 僅在 CUDA 時設 `VIDEO_ACCELERATION_ANY` + `CAP_PROP_HW_DEVICE=0`，OpenCL-only 不設 flag（避免誤套 D3D11/MFX 後端）
- **地圖 panTo 優化**（`osm_map.py` A5）— `updatePosition` JS 僅在新位置超出 `map.getBounds()` 時才 `map.panTo`，否則只更新 `marker.setLatLng`，避免播放時地圖持續晃動

## 已知陷阱

- **`_current_idx` 執行緒安全**：`VideoWorker` 內 `run()` 為唯一寫入端；Controller 的 `_current_idx` 為獨立副本，經由 signal 取得最新值
- **`position_changed` signal**：emit `(float, int)`，主執行緒直接使用傳入的 `frame_idx`
- **`worker.close()`**：僅設定 `_stop = True`，不釋放 `cap`；`run()` 迴圈結束後自行釋放
- **⚠️ QThread `run()` 不可因 EOF `break`**：`VideoWorker.run()` 播放到尾端（`cap.read()` 失敗）時**不可** `break` 跳出 while 迴圈，否則 worker 執行緒終止，後續所有 play/seek 指令進了 queue 卻無人消費，UI 完全卡死。正確做法是 `playing = False; continue`，保持迴圈存活等待下一個指令。同理，`BatchCaptureWorker.run()` 也用 `try/finally` 確保 `cap.release()` 而非提前 return
- **批次截圖**：由 `BatchCaptureWorker` 在背景執行緒順序讀取，完全脫離主執行緒
- **JPEG 品質**：由 `quality_spin`（QSpinBox, 1-100, 預設 95）控制
- **解析度等比例縮放**：`cv_utils.apply_resolution()` 以 `min(target/src)` 計算 scale
- **縮放 crop 後需 `.copy()`**：`_display_frame` 中 zoom crop 的 numpy slice 是 view（非連續記憶體），傳入 `QImage` 會噴 `TypeError`；需加 `.copy()` 保證連續性
- **硬體解碼**：`cv_utils.detect_hw_backends()` 偵測 CUDA/OpenCL；`configure_hw_acceleration()` 僅 CUDA 時設 `VIDEO_ACCELERATION_ANY` + `HW_DEVICE=0`；OpenCL-only 不設 flag（避免 MFX/D3D11 誤套）
- **`import cv2`**：`controller.py` 的 `capture_single()` 需要 `cv2`，不可漏加 import
- **signal 順序**：`video_worker.py` 中 `video_info_ready` 必須先於 `duration_ready` emit — 否則 `_on_duration_ready` 檢查 `self.video_info` 時仍為 `None`，`video_loaded` signal 永不觸發
- **`duration` 儲存**：`_on_duration_ready` 需將傳入的 `sec` 寫入 `self.video_info.duration`，否則總時長永遠為 0
- **shutdown**：務必呼叫 `_cancel_export()`（而非 `stop_export()`）以清除 `_export_params` 來阻止 retry worker 洩漏
- **stop / wait**：所有 stop 方法在 `wait()` 前須 `try: disconnect()` 避免 callback 在 wait 期間觸發
- **Logging**：使用標準 `logging` 模組搭配 `RotatingFileHandler`，日誌寫入 `tempfile.gettempdir() / dashcam_debug.log`

## GPS 相關已知陷阱

- **`_overlay_gps_text` 就地修改 frame**：`overlay_gps_text()` 內部已做 `frame.copy()`，不會修改原始 frame。Controller 與 BatchCaptureWorker 皆安全使用
- **`_parse_nmea` GPGGA 時間**：GPGGA 與 GPRMC 句子皆含 HHMMSS.SS 時間欄位，解析器從兩者提取時間以正確插值
- **`_parse_gpx` 無 time 的點**：GPX 中缺少 `<time>` 的追蹤點直接跳過，不加入軌跡列表（避免產生負值相對時間或 time=0 假點）
- **`_parse_gpx` 型別標註**：`_parse_gpx` 實際回傳 `Tuple[List[GPSPoint], Optional[float]]`，非 `List[GPSPoint]`
- **`_parse_iso_time`**：`_parse_gpx` 內部使用此輔助函數解析 XML 時間字串，支援 `Z` 與 `±HH:MM` 時區格式；解析失敗則跳過該點
- **SRT `\r\n` 相容**：`_parse_srt` 在 regex 比對前先 `content.replace("\r\n", "\n").replace("\r", "\n")` 標準化換行，否則 regex `(?=\n\n)` 對 Windows 換行永遠不匹配
- **GPS 節流計數器**：`_gps_throttle_counter` 在 seek/step/skip 時需重置為 0，否則地圖標記會延遲更新；另設 `_gps_manual_seek` flag，手動操作時跳過節流器、立即更新地圖
- **OSM 預設快取**：`osm_map.py` 將 `_TILE_CACHE_DIR` 預設為 `tempfile.gettempdir()/dashcam_osm_cache`，減少重複 HTTP 請求。`init_osm_cache()` 由 `main.py` 顯式呼叫，非 import-time 自動執行
- **OSM 快取上限**：`cleanup_cache()` 依序套用三層上限——過期（`_MAX_CACHE_AGE_DAYS`）、檔案數（`_MAX_CACHE_FILES`）、總大小（`_MAX_CACHE_SIZE_BYTES`，200MB），皆由舊至新刪除；`_read_cache()` 需容忍併發清理造成的 OSError（回傳 None）
- **OSM 快取鎖**：`osm_map.py` 使用 `_CACHE_LOCK = threading.Lock()` 保護 `_STATIC_MAP_MEMORY_CACHE` 存取，確保批次截圖與主執行緒並行時的執行緒安全
- **批次截圖 OSM 縮圖**：`BatchCaptureWorker` 的 GPS 疊加也包含 OSM 靜態縮圖（與單張截圖一致），依賴 `get_static_map()` HTTP 請求
- **`QSettings` 布林值**：`show_gps_map` / `skip_gps_overlay` 等布林設定從 QSettings 讀回時需手動轉換（字串 → bool）
- **OCR fallback**：`GPSParser._has_ocr_gps()` 在外部檔案與內嵌 metadata 都失敗時，嘗試用 Tesseract 從畫面辨識 GPS 文字（`gps_ocr.py`）。OCR 偵測取樣 3 幀（0s/3s/6s），提取時每 1 秒取樣。`_extraction_attempted` flag 確保空結果被快取，避免重複掃描
- **skip_ocr 優化**：`GPSParser(..., skip_ocr=True)` 在 `open_video` 中跳過 OCR 偵測，避免每次開啟影片都載入 Tesseract（需載入 tessdata）。OCR 僅在 `extract_gps_tracks()` 被明確呼叫且其他 GPS 來源都失敗時才執行。Controller 的 `open_video()` 傳入 `skip_ocr=True`
- **`_tracks_loaded` 快取**：`GPSManager.extract_tracks_sync()` 檢查 `_tracks_loaded` flag，已載入時直接回傳 `_tracks`，避免重複 OCR 全片掃描
- **OCR 準確度（實測）**：`gps_ocr.py.ocr_frame()` 的 ROI 裁切**先對外擴充邊距**（寬 5% / 高 15%），緊貼文字的 ROI 會裁到文字導致辨識失敗。`_recognize()` 多策略依序嘗試：**CLAHE 灰階直送**（Tesseract 內部自適應二值化，實測最準）→ Otsu 二值化 → Otsu 放大 2x/3x。⚠️ **不可對中大型文字預先放大**——實測 2x INTER_CUBIC 放大會把 `24.338535` 誤判成 `24.53585355`（3→5）；放大僅作為小字的**最後**備援
- **ROI 失敗全畫面備援**：`_has_ocr_gps()` 中已知 ROI 辨識失敗時自動退回全畫面 OCR；若全畫面成功會把 `parser._ocr_roi` 設為 `None`，後續全片提取也走全畫面
- **`_ocr_roi` 傳遞**：`GPSParser._ocr_roi` 記錄偵測階段使用的 ROI（已知設定或自動偵測結果），`GPSTrackExtractor._extract_from_ocr()` 將其傳入 `GPSOCR.extract_tracks(..., roi=...)`——**全片軌跡提取必須帶 ROI**，否則設定的偵測範圍無效
- **GPS 訊號過濾**：`filter_gps_drift(points, max_distance_m)`（`gps_parser.py`）丟棄「與前一點距離超過門檻且前後兩點彼此接近」的飄移點（跳離軌跡又跳回原位），連續大跳躍（真實快速移動）保留；端點一律保留。由 `AppSettings.load_gps_filter()` 讀取 `capture/gps_filter_enabled` / `gps_filter_max_distance`，在 `GPSParser.extract_gps_tracks()` 提取完成時套用
- **地圖 CORS**：`map_view.py` 設定 `QWebEngineSettings.LocalContentCanAccessRemoteUrls = True`，允許 `setHtml()` 載入的 HTML 存取 OSM 遠端圖磚
- **地圖載入時序**：`MapView._page_ready` flag 確保 `updatePosition()` 僅在頁面載入完成後呼叫，避免 `ReferenceError`
- **GPS 背景執行緒**：`open_video()` 中先呼叫 `worker.open(path)`（立即解碼），再啟動 `_GPSLoadThread(path)` 平行偵測 GPS。`gps_ready` 訊號在背景完成後觸發，`_on_video_loaded` 先設 GPS 為「偵測中…」，等 `_on_gps_ready` 才更新選單/地圖/狀態列
- **GPS 過期結果保護**：`gps_loaded` signal 帶 `video_path`，`_on_gps_loaded` 比對 `path != self._video_path`，快速切換影片時舊執行緒的結果會被丟棄
- **GPS ROI 系統**：使用 ROI（感興趣區域）設定取代原有「4角落×6預處理」策略，大幅提升 OCR 效能。ROI 設定綁定「解析度 + 檔案格式」（如 `1920x1080_MP4`），儲存於 QSettings `gps_roi/{key}` 命名空間
- **GPS ROI 偵測流程**：(1)查已知 ROI → (2)自動偵測（OpenCV 影像處理） → (3)手動微調 UI。自動偵測失敗時透過 `gps_manager.gps_roi_setup_needed` signal 觸發手動調整對話框
- **GPS ROI 設定儲存**：`AppSettings.load_gps_roi(w, h, fmt)` / `save_gps_roi(setting)` / `get_all_gps_roi_keys()`，使用 `gps_roi/{key}` 命名空間
- **GPS ROI 調整對話框**：`GPSROIAdjustDialog` 顯示影片畫面 + 可拖曳/縮放的 ROI 選取框，支援自動偵測按鈕與手動微調
- **GPS ROI 選取框 clamp**：`_ResizableRectItem` **必須**以 `parent=image_item` 建立（child 自動加入 scene），否則 `parentItem()` 為 `None`，clamp 範圍退化成 `QRectF(0,0,1,1)`——拖曳會把方框壓扁成 1x1、縮放永遠失敗。⚠️ 帶 parent 的 item **不可**再呼叫 `scene.addItem()`（會噴 `QGraphicsScene::addItem: item has already been added to this scene`）。繪製模式（`_toggle_draw_mode`）進入時隱藏舊框、繪製完成時從 scene 移除舊框再替換

## Unicode / 中文路徑相容性

- **`open_video_capture()`**（`utils/cv_utils.py`）— 所有 `cv2.VideoCapture` 的統一開啟入口（video_worker / batch_capture / gps_parser / gps_ocr / gps_roi_detector / gps_roi_adjust）。opencv-python >= 4.8 的 FFmpeg 後端**原生支援 UTF-8 路徑**（已實測驗證），故優先直接以原始路徑開啟，僅在失敗時退回 `win32_short_path`。回傳未開啟的 cap 表示失敗，呼叫端檢查 `isOpened()`
- **`win32_short_path()`**（`utils/ffmpeg.py`）— 8.3 短檔名轉換，現僅作為 `open_video_capture()` 的 fallback。若 8.3 短檔名在磁碟上被停用則轉換失敗（會 log warning），但不再影響開啟——原始路徑已被 opencv 原生支援
- **`imwrite_safe()`**（`utils/cv_utils.py`）— 使用 `cv2.imencode` + Python `open()` 寫入圖片，繞過 MinGW OpenCV `cv2.imwrite` 的 ANSI API 限制，天生支援中文路徑，已取代 `win32_short_path` 用於 `capture_single()` 與 `BatchCaptureWorker`
- **`utils/export_worker.py`** FFmpeg stderr 解碼先用系統 locale encoding（CP950 for 繁體中文），fallback UTF-8
- **`utils/time_utils.py`** 時間輸入已處理全形冒號 `：`（U+FF1A）
- **輸出資料夾 fallback**：`os.path.abspath("captures")`（非硬編碼 `./captures`）

## 封裝成 .exe

使用 **PyInstaller**（v6.15.0）+ **Python 3.11**。

```bash
pip install pyinstaller
pyinstaller --onefile --name DashcamPlayer --console ^
    --add-data "C:\ffmpeg\bin\ffmpeg.exe;." ^
    --add-data "C:\ffmpeg\bin\ffprobe.exe;." ^
    --exclude-module pytest ^
    dashcam_player\main.py
```

- `--onefile`：單一 exe 檔，可直接複製到任何位置執行
- `--console`：保留終端機視窗（方便看 log）
- `--add-data`：將 FFmpeg/FFprobe 封裝進 exe（啟動時解壓至暫存目錄）
- `find_ffmpeg()` 會優先檢查 `sys._MEIPASS`（PyInstaller 解壓路徑）
- 若不需 FFmpeg 可拔除 `--add-data` 行

### CI 自動封裝（.github/workflows/ci.yml `build-exe` job）

推 `v*` tag 時，GitHub Actions 在 `windows-latest` 自動建置並上傳至 Release：

- 下載 FFmpeg essentials build（gyan.dev）→ `--add-data` 封裝進 exe
- 使用 Python 3.11 + PyInstaller，指令與上方手動流程相同
- 產出 `DashcamPlayer.exe` 同時上傳 artifact 與 GitHub Release
- 需 `contents: write` permission（`softprops/action-gh-release`）

推 tag 範例：`git tag v1.0.0 && git push origin v1.0.0`

## 無用／不存在

- 沒有 `README`
- 不要搜尋 `_output_prefix`（已清理）
- `numpy` 用於 `cv_utils.py`（型別標註 + OpenCV 操作）與 `gps_ocr.py`（影像預處理），其餘檔案不可新增 `import numpy`
- `dashcam_player/__init__.py` 為空殼（僅 docstring），無 re-export。`core/__init__.py` 才有 re-export VideoWorker / ExportWorker / BatchCaptureWorker / Controller + 四個 Manager（但 `controller.py` 直接 import 個別模組而非經由 `__init__` 的 re-export）
