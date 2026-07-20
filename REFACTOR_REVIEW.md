# REFACTOR 設計審查紀錄

產生日期：2026-07-04（最近更新：2026-07-19）
測試狀態：64 passed

---

## 目錄

1. [背景](#1-背景)
2. [原始設計方案](#2-原始設計方案)
3. [首次分析（含錯誤結論）](#3-首次分析含錯誤結論)
4. [重新檢驗與修正](#4-重新檢驗與修正)
5. [最終工作事項](#5-最終工作事項)
6. [測試現狀](#6-測試現狀)
7. [附錄：程式碼結構](#7-附錄程式碼結構)

---

## 1. 背景

專案「行車紀錄器影片播放與截圖工具」為多模組 PyQt5 + OpenCV 應用程式。

### 架構

```
dashcam_player/
├── main.py               # 入口點
├── core/
│   ├── controller.py     # Controller（連接 UI 與 Worker）
│   ├── video_player.py   # 瘦 UI 主視窗（1248 行，重構前為 1531 行）
│   ├── video_worker.py   # 播放背景執行緒
│   ├── export_worker.py  # FFmpeg 匯出
│   └── batch_capture.py  # 批次截圖
├── models/
│   ├── video_info.py
│   ├── capture_settings.py
│   └── resolution.py     # Resolution Enum（重構新增）
├── ui/
│   ├── styles.py
│   ├── map_view.py
│   ├── playback_controls.py  # PlaybackControls widget（重構新增但未啟用）
│   └── dialogs/
│       ├── capture_settings.py
│       ├── gps_settings.py
│       └── output_settings.py
├── utils/
│   ├── ffmpeg.py / cv_utils.py / time_utils.py
│   ├── gps_parser.py      # 含 interpolate_gps() 純函數
│   └── osm_map.py / log.py
├── config/
│   └── settings.py
└── tests/
    ├── test_gps_parser.py  # 20 tests
    ├── test_models.py      # 5 tests
    └── test_utils.py       # 6 tests
```

### 已知陷阱（AGENTS.md 節錄）

- `_current_idx` 執行緒安全受 `_lock` 保護
- `position_changed` emit `(float, int)`
- `worker.close()` 僅設 `_stop = True`，run 結束後自行釋放 cap
- BatchCaptureWorker 完全背景化，脫離主執行緒
- JPEG 品質由 quality_spin (1-100, 預設 95) 控制
- 縮放 crop 後需 `.copy()` 保證連續記憶體
- signal 順序：`video_info_ready` 必須先於其他 signal
- shutdown 需呼叫 `_cancel_export()`（非 `stop_export()`）
- 所有 stop 方法在 `wait()` 前須 `disconnect()`
- Unicode 中文路徑相容：`win32_short_path()` / `imwrite_safe()`
- QSettings 布林值需手動轉換字串→bool

---

## 2. 原始設計方案

### Q1: State Ownership Strategy

**決策：Controller 為 SSOT，View 唯讀**

- SSOT（Single Source of Truth）由 Controller 持有
- View 不直接修改 Worker 狀態
- View 不可互相修改 Controller 狀態
- View 可暫存 UI-only 狀態（縮放、預覽模式）
- 例外：`_current_frame`（numpy array）僅存於 View

### Q2: Preview Toolbar Widget

**決策：方案 A — PlaybackControlGroup**

- 完全取代底部面板與 QGroupBox
- 保留一份 widget，控制項固定顯示
- 切換 preview 時僅 show/hide 四組 QGroupBox
- 三欄：播放控制 | 標記與時間 | 動作按鈕

### Q3: Timeout & Retry

**決策：不重試，合併 P2 改動**

- ExportWorker TIMEOUT_SEC = 3600（1 小時）
- timeout 時 emit `finished(False, "逾時")`，不做 retry
- Resolution Enum 從 P2 合併到本次改動
- `capture_single()` 改收 `CaptureSettings` 一次整改

### Q4: Test Strategy

**決策：重構前 Integration Test → 重構後 Unit Test**

- 先寫整合測試鎖定行為 boundary
- 重構完成後補單元測試驗證重構正確性
- 寫測試時即可發現不易測試的設計問題

---

## 3. 首次分析（含錯誤結論）

### 初始結論（已證實錯誤）

| Step | 項目 | 初始結論 | 實際狀態 |
|------|------|---------|---------|
| 4 | GPS 插值共用 | BUG + 未完成 | ✅ 全數完成 |
| 5 | SSOT 集中 | 多處重複 | ✅ 全數完成 |
| 6 | Resolution Enum | 完全未開始 | ✅ 全數完成 |
| 7 | PlaybackControls | 雙重 widget | ✅ 已啟用完成 |

### 錯誤原因

1. **`__pycache__` 快取污染**：首次檢查 `GPSParser.get_gps_bounds` 回傳 `False`，因 `gps_parser.py` 舊版 compiled cache 殘留。清除後重新 import 確認三方法皆為 class method。

2. **未注意到 `video_player.py` 已大幅瘦身**：初始假設基於 `video_player.py.old`（1531 行），但當前版本僅 1248 行。`_total_frames`、`_fps`、`_duration` 已從 View 移除，`_capture_start/end_frame` 已改透過 Controller property 讀寫。

3. **漏看 `batch_capture.py:78`**：`_get_gps_at_frame()` 已使用 `interpolate_gps()`，無重複邏輯。

4. **未檢查 `models/resolution.py`**：該檔案已存在，Resolution Enum 已全面導入。

---

## 4. 重新檢驗與修正

### Step 4: GPS 插值共用 — ✅ 已全數完成

```
gps_parser.py:667-692    interpolate_gps(seconds, points)         # module-level 純函數
gps_parser.py:572-574    GPSParser.get_gps_at_time()             # 委派 interpolate_gps()
batch_capture.py:73-78   BatchCaptureWorker._get_gps_at_frame()  # 使用 interpolate_gps()
gps_parser.py:576-591    GPSParser.get_gps_bounds()              # 正常 class method
gps_parser.py:597-628    GPSParser._probe_file()                 # 正常 class method
gps_parser.py:630-664    GPSParser.to_gpx()                      # 正常 class method
```

### Step 5: SSOT 集中 — ✅ 已全數完成

**Controller 持有：**
- `video_info: VideoInfo`（含 fps / frame_count / duration）
- `_capture_start_frame / _capture_end_frame`（透過 property 讀寫）
- `_current_idx: int`（播放位置）
- `_playing: bool`

**Controller 提供：**
- `@property total_frames` → `video_info.frame_count`
- `@property fps` → `video_info.fps`
- `@property duration` → `video_info.duration`
- `@property capture_start/end_frame` → `_capture_start/end_frame` (rw)
- `get_current_frame_index()` → `_current_idx`

**View 僅存 UI-only 狀態：**
- `_current_frame: numpy.ndarray` — 顯示用 frame
- `_preview_mode: bool`
- `_zoom_factor / _zoom_rel_x / _zoom_rel_y`
- `_has_gps: bool`

### Step 6: Resolution Enum — ✅ 已全數完成

```python
# models/resolution.py
class Resolution(Enum):
    ORIGINAL = "原始"
    HD720    = "1280x720"
    HD1080   = "1920x1080"
    QHD      = "2560x1440"
    UHD4K    = "3840x2160"

    @classmethod
    def from_value(cls, value: str) -> Resolution: ...  # 含舊設定 fallback
    @property
    def scale_str(self) -> str: ...  # 傳給 apply_resolution()
    @classmethod
    def values(cls) -> list[str]: ...  # combo box 用
```

已使用於：
- `capture_settings.py:15` — `resolution: Resolution`
- `controller.py:284` — `capture_single(..., resolution: Resolution, ...)`
- `batch_capture.py:50` — `resolution: Resolution = Resolution.ORIGINAL`
- `video_player.py:146` — `Resolution.values()`
- `video_player.py:664` — `Resolution.from_value()`
- `video_player.py:725` — `Resolution.from_value()`
- `video_player.py:909` — `Resolution.from_value()`
- `config/settings.py:67` — `from_dict()` 處理舊字串

### Step 7: PlaybackControls — ✅ 已啟用完成

- `PlaybackControls` 實例化為 `self.controls`，始終可見
- 移除 `_create_preview_toolbar()` + 8 組 `_pv_*` widgets（~115 行）
- 移除兩套舊控制項：`ctrl_layout`、`left_group`、`mid_group`、`time_layout`
- 移除 8 處 `hasattr` guard，改用 `self.controls.*` 直取
- 簡化 `_toggle_preview_mode()`：僅 show/hide `_full_bottom`（設定面板）
- 簡化 `_on_speed_change()`：移除雙 combo 同步邏輯
- 更新註解 `"controller 不儲存 UI 狀態"`

---

## 5. 最終工作事項

### 無待完成工作 — Step 7 已全數完成

### 無須處理

```
✗ Step 4 — gps_parser indent bug        → 不存在
✗ Step 5 — View 重複變數               → 已移除
✗ Step 6 — Resolution 字串型別          → 已為 Enum
✗ P2 合併 — 已納入本次改動
✗ ExportWorker 重試                    → 不重試（決議已定）
```

---

## 6. 測試現狀

```
dashcam_player\tests\
├── conftest.py             — 共用 fixtures（video_worker, frame_*）
├── test_gps_parser.py      — 26 passed (NMEA, GPX, SRT, interpolate, detector, extractor)
├── test_models.py          —  6 passed (VideoInfo, CaptureSettings roundtrip, ProbeInfo)
├── test_utils.py           —  7 passed (format_time, parse_time_input, get_keyframe_positions)
├── test_cv_utils.py        — 11 passed (fourcc, resolution, overlay, imwrite)
└── test_video_worker.py    — 12 passed (command queue, keyframe, release_cap)
─────────────────────────────────
Total: 64 passed in 0.84s
```

無測試失敗。ruff + mypy 通過。

---

## 7. 附錄：程式碼結構

### 關鍵檔案行數

| 檔案 | 行數 | 說明 |
|------|------|------|
| `core/video_player.py` | 1078 | 瘦 UI 主視窗 |
| `core/controller.py` | 593 | Controller 層（含 relay signals + 播放控制） |
| `core/video_worker.py` | 336 | 播放背景執行緒 |
| `core/batch_capture.py` | 196 | 批次截圖 Worker |
| `core/export_worker.py` | 68 | FFmpeg 匯出 Worker |
| `core/gps_manager.py` | 178 | GPS 管理器（含背景載入執行緒） |
| `utils/gps_parser.py` | 801 | GPS 解析 + interpolate_gps |
| `utils/gps_ocr.py` | 279 | EasyOCR GPS 辨識 |
| `utils/cv_utils.py` | 206 | OpenCV 工具 |
| `utils/osm_map.py` | 351 | OSM 快取 + Leaflet HTML |
| `models/resolution.py` | 35 | Resolution Enum |
| `models/capture_settings.py` | 68 | CaptureSettings dataclass |
| `ui/playback_controls.py` | 118 | PlaybackControls widget（已啟用） |
| `config/settings.py` | 73 | QSettings 持久化 |
| `dashcam_player/__init__.py` | 4 | 套件標記（空殼 docstring），讓測試與啟動可用 `dashcam_player.*` import |

### 測試命令

```bash
pytest                              # 從專案根目錄（透過 dashcam_player/__init__.py 解析為 regular package）
pytest dashcam_player/tests/ -v     # 詳細輸出
python -m pytest dashcam_player/tests/ -v   # 等同上，無論 __init__.py 與否皆可
```
