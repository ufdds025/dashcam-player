# 效能改善與修正計畫書

> 審查日期：2026-07-07
> 適用版本：dashcam_player (重構後版本)
> 審查範圍：全專案模組

---

## 1. 架構現況

### 1.1 模組依賴圖

```
main.py
 └── VideoPlayer (UI / QMainWindow)
      └── Controller (業務邏輯集中管理)
           ├── VideoWorker (QThread — 背景解碼)
           ├── GPSManager (QObject — GPS 載入/查詢)
           ├── BatchCaptureWorker (QThread — 批次截圖)
           └── ExportWorker (QThread — FFmpeg 匯出)

utils/
 ├── cv_utils.py       (OpenCV 工具)
 ├── gps_parser.py     (GPS 解析核心)
 ├── gps_ocr.py        (EasyOCR 辨識)
 ├── osm_map.py        (OSM 地圖快取)
 ├── ffmpeg.py         (FFmpeg/FFprobe 工具)
 └── time_utils.py     (時間格式)

models/
 ├── video_info.py     (VideoInfo dataclass)
 ├── capture_settings.py
 └── resolution.py     (Resolution Enum)
```

### 1.2 執行緒模型

| 執行緒 | 類型 | 責任 |
|--------|------|------|
| Main Thread | QApplication | UI 事件迴圈、signal/slot 分發 |
| VideoWorker | QThread | OpenCV 解碼、frame emit |
| _GPSLoadThread | QThread (in GPSManager) | GPS 偵測/解析 |
| BatchCaptureWorker | QThread | 批次讀取/截圖 |
| ExportWorker | QThread | FFmpeg subprocess |
| osm_map cleanup | threading.Thread (daemon) | 30天快取清理 |

### 1.3 關鍵共享狀態

| 變數 | 位置 | 保護方式 | 風險 |
|------|------|---------|------|
| `_current_idx` | VideoWorker | `threading.Lock` | 低 |
| `_seek_frame` | VideoWorker | `threading.Lock` | 低 |
| `_play` | VideoWorker | 無 lock | 低 |
| `_stop` | VideoWorker | 無 lock（GIL 隱含保護） | 低 |
| `_gps_throttle_counter` | Controller | 無 lock（僅主執行緒） | 低 |
| `_gps_manual_seek` | Controller | 無 lock（僅主執行緒） | 低 |
| `_export_params` | Controller | 無 lock（僅主執行緒） | 低 |

---

## 2. 問題分級

| 等級 | 定義 | 數量 |
|------|------|------|
| 🔴 高 | 正確性錯誤或嚴重效能問題（>30% 影響） | 4 |
| 🟡 中 | 執行緒安全、資源管理或中等效能問題 | 7 |
| 🟢 低 | 程式碼品質、維護性、輕量優化 | 7 |

---

## 3. 🔴 高優先修正

### 3.1 Bug: `self.ctrl.gps_parser` 不存在

**檔案**: `dashcam_player/core/video_player.py`
**位置**: 第 510 行
**嚴重性**: 正確性錯誤 — AttributeError
**原因**: 重構後 GPS 功能已移至 `GPSManager`，`Controller` 無 `gps_parser` 屬性

**現況程式碼**:
```python
# video_player.py 第 508-512 行（約）
def _on_video_loaded(self, info):
    # ... 初始化 UI ...
    if not self.ctrl.gps_parser:    # ← Bug: gps_parser 不存在
        self._gps_no_data_action.setEnabled(False)
```

**修正方案**: 移除此檢查，改用信號機制。`_gps_no_data_action` 應由 `gps_ready` signal 統一管理。

```python
def _on_video_loaded(self, info):
    # ... 初始化 UI ...
    pass  # GPS 狀態由 _on_gps_ready 統一處理

def _on_gps_ready(self, path, has_gps, track_count):
    if video_path != self._video_path:
        return  # 忽略過期結果
    self._gps_no_data_action.setEnabled(has_gps)
```

**實作步驟**:
1. 移除 `video_player.py` 中 `_on_video_loaded` 內的 `if not self.ctrl.gps_parser:` 區塊
2. 確認 `_on_gps_ready` 中已有 `self._gps_no_data_action.setEnabled(has_gps)`
3. 搜尋全專案確認無其他 `gps_parser` 殘留參照

**驗證**: 開啟有 GPS 影片後嘗試截圖，確認無 `AttributeError`

---

### 3.2 高效能: 雙 Buffer Ping-Pong 顯示模式

**檔案**: `dashcam_player/core/video_worker.py`
**嚴重性**: 記憶體複製過多，每幀 2 次 `frame.copy()`
**影響**: 30fps 1080p 影片浪費 ~186MB/s，4K 可達 ~336MB/s

#### 現況問題

```python
# video_worker.py run() 中
frame = self.cap.read()          # [1] OpenCV 緩衝區 A
frame_copy = frame.copy()         # [2] 複製到緩衝區 B — 浪費
frame_ready.emit(frame_copy, idx) # [3] 複製到 QImage 又 copy() — 浪費
```

#### 修正方案: Ping-Pong Buffer

```python
class VideoWorker(QThread):
    def __init__(self):
        super().__init__()
        # 兩個緩衝區，輪流使用
        self._buffers = [None, None]
        self._buf_idx = 0        # 當前寫入 buffer
        self._ready_idx = None   # 已完成解碼的 buffer 索引（供消費者讀取）
        self._buf_lock = QMutex()  # 保護 _ready_idx
        self._dropped = 0        # 統計掉的幀數

    def run(self):
        # ... 初始化 cap ...
        while not self._stop:
            # 讀取幀
            ret, frame = self.cap.read()
            if not ret:
                self.load_finished.emit()
                break

            # 放入當前 buffer（覆蓋舊資料）
            self._buffers[self._buf_idx] = frame
            self._buf_idx = 1 - self._buf_idx  # 交換

            # emit buffer 索引（不改記憶體內容）
            with QMutexLocker(self._buf_lock):
                self._ready_idx = 1 - self._buf_idx
            self.frame_ready.emit(id(self._buffers[1 - self._buf_idx]), frame_idx)
            # ...
```

**消費者端（VideoPlayer._on_frame_ready）**:
```python
def _on_frame_ready(self, frame, idx):
    # frame 即為 buffer 參照，不複製
    self._display_frame(frame)
```

**QImage 端修正**:
```python
# cv_utils.py
def frame_to_qimage(frame_bgr: np.ndarray) -> QImage:
    h, w = frame_bgr.shape[:2]
    # 不再呼叫 .copy() — 由呼叫端確保 frame 在 emit 完成前不被修改
    return QImage(frame_bgr.data, w, h, w * 3, QImage.Format_BGR888)
```

**實作步驟**:
1. 在 `VideoWorker.__init__` 加入 `self._buffers = [None, None]`, `self._buf_lock = QMutex()`, `self._ready_idx = None`
2. 修改 `run()` 內的 frame 發射，改為 emit buffer 索引
3. 修改 `frame_ready` signal 移除 `.copy()`
4. 修改 `cv_utils.frame_to_qimage()` 移除 `.copy()`
5. 修改 `VideoPlayer._on_frame_ready()` 直接使用傳入的 frame
6. 加入 `frame.timestamp` 或確保 `frame` 在顯示完成前不被覆寫的機制（可加 QMutex 保護消費者端）
7. 加入 `dropped frames` 統計並可選 log

**替代方案（更簡單）**: 若不想修改 video_worker，考虑在 `frame_to_qimage()` 前檢查 frame 是否連續，不連續才 copy。但這只是局部優化，無法減少 worker 內的 copy。

**驗證**: 使用 4K 影片播放，觀察任务管理器的記憶體使用曲線，確認無持續線性成長

---

### 3.3 高效能: GPS 合併單次 ffprobe 呼叫

**檔案**: `dashcam_player/utils/gps_parser.py`
**嚴重性**: 每次 GPS 偵測執行 2 次獨立 ffprobe subprocess
**影響**: GPS 載入時間從 20-30s 降至 10-15s

#### 現況問題

```python
# gps_parser.py — 兩個獨立呼叫點
class GPSParser:
    def _has_embedded_gps(self):
        data = self._probe_file()      # ← 第一次 ffprobe
        # 解析...

    def _extract_from_data_track(self):
        data = self._probe_file()      # ← 第二次 ffprobe（重複）
        # 解析...
```

#### 修正方案: 統一探測 + 快取

```python
class GPSParser:
    def _probe_file(self):
        if self._probe_cache is not None:
            return self._probe_cache
        # ... 現有 ffprobe 邏輯 ...
        self._probe_cache = data
        return data

    def _invalidate_probe_cache(self):
        self._probe_cache = None

    def _has_embedded_gps(self):
        data = self._probe_file()  # 使用快取
        # 解析...

    def _extract_from_data_track(self):
        data = self._probe_file()  # 使用同一份資料
        # 解析...
```

**實作步驟**:
1. 在 `GPSParser.__init__` 加入 `self._probe_cache = None`
2. 重構 `_probe_file()` 為帶快取的單次執行
3. 在 `_extract_gpx()` / `_extract_nmea()` / `_extract_srt()` 完成後呼叫 `_invalidate_probe_cache()`（外部檔案不需要 ffprobe）
4. 在 `open_video()` 重建 `GPSParser` 時自然重置

**驗證**: 以含嵌入式 GPS 的影片測試，計算 `_probe_file()` 被呼叫次數（除錯 log），確認僅 1 次

---

### 3.4 高效能: 批次截圖 OSM 磁碟快取

**檔案**: `dashcam_player/core/controller.py` 的 `capture_single()`
**嚴重性**: 每次單張截圖都 HTTP fetch OSM tile
**影響**: 重複截圖時網路延遲高，離線無法運作

#### 現況問題

```python
# controller.py capture_single()
osm_bytes = get_static_map(...)  # 每次呼叫都網路請求
```

`osm_map.py` 已有模組級 LRU 記憶體快取（256 筆），但 **單張截圖路徑未使用**。`BatchCaptureWorker` 有自己的 `_last_osm_bytes` 短期快取，但無跨截圖的持久化。

#### 修正方案

`capture_single()` 應直接使用 `get_static_map()`（已實作磁碟快取），無需改變。檢查 `osm_map.py` 的 `get_static_map()` 是否已實作磁碟快取：

```python
# osm_map.py — get_static_map() 現已有磁碟快取（7天效期）
def get_static_map(lat, lon, zoom=15, size=(300, 200)):
    # 記憶體 LRU 先行
    # 磁碟 cache 次之
    # 否則 HTTP 請求
```

**確認**: 檢查 `osm_map.py` 第 104-127 行磁碟快取邏輯是否存在。若存在，則 `capture_single()` 無需修改。

**實作步驟**:
1. 確認 `osm_map.py` 的 `get_static_map()` 磁碟快取實作完整
2. 確認 `capture_single()` 確實呼叫 `get_static_map()`（而非繞過快取）
3. 若缺少，則將 `osm_map.py` 的 `_get_disk_cache()` 邏輯應用於 `get_static_map()`

---

## 4. 🟡 中優先修正

### 4.1 執行緒安全: VideoWorker `_current_idx` 讀取加鎖

**檔案**: `dashcam_player/core/video_worker.py`
**位置**: `step_forward()`, `step_backward()`
**問題**:
```python
def step_forward(self):
    target = self._current_idx + 1  # 無 lock 保護
    with self._lock:
        self._seek_frame = target
```

**修正**: 讀取 `_current_idx` 時加 lock
```python
def step_forward(self):
    with self._lock:
        target = self._current_idx + 1
        if target < self._total_frames:
            self._seek_frame = target
```

### 4.2 執行緒安全: VideoWorker 加入 `wait()` 機制

**檔案**: `dashcam_player/core/video_worker.py`
**問題**: `open()` 啟動新執行緒但舊執行緒可能未完全結束

**修正**: 在 `open()` 中確保前一個執行緒已結束
```python
def open(self, path: str) -> None:
    # 若執行緒仍在運行，等待結束
    if self.isRunning():
        self._stop = True
        self.wait(3000)  # 最多等 3 秒
        if self.isRunning():
            self.terminate()  # 強制終止（最後手段）
    # 重設狀態...
    self.start()
```

### 4.3 GPS: 子程序 Timeout 與取消機制

**檔案**: `dashcam_player/utils/gps_parser.py`
**問題**: `subprocess.run()` 呼叫無中斷機制

**修正**: 使用 `subprocess.run(timeout=...)` 並在 `_GPSLoadThread` 中檢查 `_stop`
```python
# _GPSLoadThread.run() 中
def run(self):
    for check_point in self._check_points:
        if self._stop:
            return
        # ... 對應的解析邏輯 ...

    # ffprobe subprocess 加入 timeout
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=15
        )
    except subprocess.TimeoutExpired:
        return  # GPS 偵測失敗，不阻礙影片播放
```

**實作**: 在 `_GPSLoadThread` 類別中加入 `timeout` 參數，並在 3 個檢查點之間插入 `_stop` 檢查。

### 4.4 執行緒安全: 文件化執行緒契約

**檔案**: `dashcam_player/core/video_worker.py`（頂部 docstring）
**問題**: `_play` 旗標無 lock 保護但被跨執行緒讀寫

**修正**: 加上 docstring 明確說明
```python
"""
Thread safety:
- _current_idx, _seek_frame: protected by _lock
- _stop: written only by main thread (close()), read by worker (run())
- _play: main-thread-only write, worker-thread-only read (no concurrent write)
"""
```

### 4.5 記憶體: 減少 GPS 軌跡複本

**檔案**: `dashcam_player/core/gps_manager.py`
**問題**: 最多 3 份 GPS 軌跡複本

| 位置 | 內容 |
|------|------|
| `GPSParser._tracks` | 原始軌跡 |
| `GPSManager._tracks` | `list(parser.extract_gps_tracks())` 複本 |
| `BatchCaptureWorker._gps_data` | 又是另一份 |

**修正**: `extract_tracks_sync()` 回傳 `parser._tracks` 的切片視圖而非完整 copy，或直接共享參考
```python
# gps_manager.py extract_tracks_sync()
def extract_tracks_sync(self):
    if self._tracks is not None:
        return self._tracks  # 直接回傳參考，不複製
    tracks = self._parser.extract_gps_tracks()
    self._tracks = tracks
    return tracks
```

### 4.6 VideoInfo probe_info 快取清理

**檔案**: `dashcam_player/models/video_info.py`
**問題**: ffprobe 快取永久佔記憶體

**修正**: 加入清理方法
```python
class VideoInfo:
    def clear_probe_cache(self):
        self.probe_info = None
```

在 `Controller.open_video()` 開頭呼叫前一個 `VideoInfo` 的 `clear_probe_cache()`（若存在的話）。

### 4.7 Resolution Enum 統一使用

**檔案**: `dashcam_player/models/resolution.py`, `dashcam_player/ui/dialogs/output_settings.py`
**問題**: `"原始"` 作為 magic string 散佈

**修正**: 在 `Resolution` 加入明確方法
```python
class Resolution(Enum):
    ORIGINAL = ("原始", "")
    # ...

    @property
    def is_original(self) -> bool:
        return self == Resolution.ORIGINAL
```
並在 `cv_utils.apply_resolution()` 使用 `res.is_original` 而非 `res_text == "原始"`。

---

## 5. 🟢 低優先修正

### 5.1 清理備份檔案

**刪除**:
- `dashcam_player/core/video_player.py.old`
- `dashcam_player/video_player.py.old`
- `dashcam_player/ui/dashcam_map_qveo_m65.html`（含絕對路徑）

### 5.2 `shutdown` 順序強化

**檔案**: `dashcam_player/core/controller.py`
**問題**: `close()` + `wait()` + `release_cap()` 三步驟在超時時可能狀態不一致

**修正**:
```python
def shutdown(self):
    # 1. 停止所有 worker
    self.worker.close()
    if not self.worker.wait(3000):
        self.worker.terminate()
    # 2. 確保 cap 已釋放（在執行緒结束後）
    self.worker.release_cap()
    # 3. 其餘 shutdown...
```

### 5.3 EXIF GPS 寫入前清理

**檔案**: `dashcam_player/utils/cv_utils.py` `write_exif_gps()`
**問題**: `piexif.insert()` 不清除舊 EXIF

**修正**:
```python
def write_exif_gps(image_path, gps_coord):
    try:
        piexif.remove(image_path)  # 先清除舊 EXIF
        # ... insert GPS ...
    except Exception:
        pass
```

### 5.4 Zoom Reset 後狀態列更新

**檔案**: `dashcam_player/core/video_player.py`
**問題**: `_toggle_play` 設 `_zoom_factor = 1.0` 後呼叫 `_update_zoom_status()` 但立即被 `_update_status_bar()` 覆蓋

**修正**:
```python
def _toggle_play(self):
    playing = self.ctrl.toggle_play()
    if playing:
        self._zoom_factor = 1.0
        # 不要立即更新狀態列，確保下次 statusBar.showMessage() 不被覆蓋
    self.controls.btn_play.setText("⏸" if playing else "▶")
```

---

## 6. 實作優先順序

```
第一梯隊（立即修復）:
├── 3.1 移除 self.ctrl.gps_parser bug
└── 3.3 GPS 合併 ffprobe

第二梯隊（效能顯著改善）:
├── 3.2 雙 Buffer Ping-Pong
├── 4.3 GPS subprocess timeout
└── 4.5 GPS 軌跡記憶體共享

第三梯隊（穩健性強化）:
├── 4.1 VideoWorker _current_idx 讀取加鎖
├── 4.2 VideoWorker wait() 機制
├── 4.4 執行緒契約文件化
└── 4.6 probe_info 清理

第四梯隊（程式碼品質）:
├── 3.4 OSM 快取確認
├── 4.7 Resolution Enum 統一
├── 5.1 清理備份檔案
├── 5.2 shutdown 順序
├── 5.3 EXIF 清理
└── 5.4 Zoom 狀態列
```

---

## 7. 驗證矩陣

| 修正項目 | 驗證方法 | 成功標準 |
|---------|---------|---------|
| 3.1 gps_parser bug | 開啟任意影片，截圖 | 無 AttributeError |
| 3.2 雙 Buffer | 4K 30fps 播放，觀察記憶體 | 記憶體不持續成長 |
| 3.3 合併 ffprobe | 調試 log 計數 `_probe_file` | 僅 1 次呼叫 |
| 3.4 OSM 快取 | 同一位置截圖 2 次（離線） | 第二次無網路請求 |
| 4.1 讀取加鎖 | 快速連續 step_forward | 無假值或崩潰 |
| 4.2 wait() 機制 | 快速切換影片 10 次 | 無多個執行緒同時運行 |
| 4.3 timeout | GPS 載入中途按 Esc 取消 | 2s 內回應 |
| 4.5 GPS 記憶體 | 長途軌跡（10000+ 點）監控 | 記憶體用量↓ 30% |
| 5.1 清理備份 | 搜尋 `*.old` / `*.html` 含絕對路徑 | 無殘留 |
| 5.3 EXIF 清理 | 同一檔案寫入 GPS 2 次，讀回 | 僅 1 組 GPS IFD |

---

## 8. 附錄：相關檔案行數統計

| 檔案 | 行數 | 備註 |
|------|------|------|
| core/controller.py | ~601 | 業務邏輯集中點 |
| core/video_player.py | ~1037 | UI 主視窗 |
| core/video_worker.py | ~213 | 背景解碼 |
| core/gps_manager.py | ~185 | GPS 管理 |
| utils/gps_parser.py | ~794 | GPS 解析核心 |
| utils/cv_utils.py | ~166 | OpenCV 工具 |
| utils/osm_map.py | ~341 | OSM 快取 |
| models/video_info.py | ~55 | 影片資訊 dataclass |
| models/capture_settings.py | ~68 | 截圖設定 dataclass |
| models/resolution.py | ~34 | 解析度列舉 |

---

*本文件為內部審查記錄，每次重大修正後更新「驗證矩陣」與「實作狀態」。*