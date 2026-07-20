# Project GPS 功能規劃書

> **專案**: 行車記錄器影片播放與截圖工具
> **日期**: 2026-07-04
> **版本**: 1.2
> **最後更新**: 2026-07-04 — Phase 7 OCR GPS fallback + 地圖 CORS 修正

---

## 目錄

1. [功能概述](#功能概述)
2. [核心規格](#核心規格)
3. [技術架構](#技術架構)
4. [模組設計](#模組設計)
5. [UI 設計](#ui-設計)
6. [實作步驟](#實作步驟)
7. [測試計畫](#測試計畫)
8.  [相依 性與限制](#相依性與限制)
9. [未來擴展](#未來擴展)

---

## 功能概述

為行車記錄器影片播放工具新增 **GPS 軌跡處理** 功能，包含：

1.  **剪 模式保留 GPS**：影片擷取時保留原始 GPS 軌跡資料。 ✅
2.  **照片 疊加 GPS**：截取畫面時自動注入 GPS 座標與地圖縮圖。 ✅（單張 ✅ / 批次 ✅）
3.  **地圖同步顯示**：播放時同步顯示行車軌跡與當前位置。 ✅

---

## 核心規格

### 功能 1: 影片擷取保留 GPS ✅

| 項目 | 規格 | 狀態 |
|------|------|------|
| **觸發條件** | 使用者點擊「擷取影片」且勾選「使用原始編碼（stream copy）」 | ✅ |
| **技術方案** | FFmpeg `-map 0` + `-c copy` + `preserve_gps=True` | ✅ |
| **預設行為** | 複製所有串流（視訊、音訊、GPS、字幕等） | ✅ |
| **錯誤處理** | `-map 0` 失敗時 fallback 為 libx264 重新編碼 | ✅ |
| **測試驗證** | `build_export_cmd(preserve_gps=True)` 含 `-map 0` | ✅ |

### 功能 2: 照片疊加 GPS ✅

| 項目 | 規格 | 狀態 |
|------|------|------|
| **觸發條件** | 單張截取或批次截取 | ✅ |
| **GPS 來源** | `GPSParser._get_current_gps()` 即時提取 | ✅ |
| **疊加內容** | 座標文字 (WGS84) + OpenStreetMap 靜態縮圖 160×160 | ✅ |
| **縮圖尺寸** | 160×160 像素（右下角，半透明 blend） | ✅ |
| **座標位置** | 畫面右下角（縮圖上方） | ✅ |
| **自動跳過條件** | 使用者勾選「影片已有 GPS 浮水印」 | ✅ |
| **EXIF 寫入** | 寫入 GPS 座標至 JPEG EXIF（PNG 跳過） | ✅ |

### 功能 3: 地圖同步顯示 ✅

| 項目 | 規格 | 狀態 |
|------|------|------|
| **顯示條件** | 使用者開啟「顯示 GPS 軌跡」選項 | ✅ |
| **地圖來源** | OpenStreetMap (Leaflet.js) — 預設 | ✅ |
| **備援方案** | Google Maps (需 API Key) | ✅ |
| **佈局** | QSplitter 左右分割 (60% / 40%) | ✅ |
| **軌跡顯示** | Polyline + 起點（綠）/ 終點（紅）/ 當前位置（藍） | ✅ |
| **同步方式** | `position_changed` → `get_gps_at_time` → `updatePosition()` | ✅ |
| **效能優化** | 預載所有 GPS 點 + throttling（每 15 幀更新一次） | ✅ |

---

## 技術架構

### 技術選型

| 元件 | 技術 | 說明 |
|------|------|------|
| GPS 解析 | FFprobe (metadata tags / data track) + 外部 GPX/NMEA/SRT + EasyOCR fallback | 從影片或外部檔案提取 GPS，OCR 作為最後手段 |
| 地圖渲染 | Leaflet.js + OpenStreetMap | 免費、無需 API Key、支援離線 |
| Web 容器 | QWebEngineView (PyQt5) | 內嵌瀏覽器顯示地圖 |
| 圖片處理 | OpenCV (`cv2.imdecode`) + piexif (僅 JPEG) | 疊加縮圖與寫入 EXIF |
| OSM 縮圖 | `requests` → OpenStreetMap Static API | 免費圖資，含 7 天快取 |

### 架構圖

```
┌─────────────────────────────────────────────────────┐
│                     VideoPlayer                      │
│  (QMainWindow)                                       │
└─────────────────────────────────────────────────────┘
                         │
         ┌───────────────┼───────────────┐
         ▼               ▼               ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────────┐
│   Controller │  │  GPS Parser  │  │    MapView       │
│ (事件中樞)   │  │ (提取+插值)  │  │ QWebEngineView   │
└──────────────┘  └──────────────┘  │ + Leaflet.js     │
       │                 │          │ + OSM Tiles      │
       │         ┌───────┴───────┐ └──────────────────┘
       │         ▼              ▼
       │  ┌──────────┐  ┌──────────┐
       │  │ FFmpeg   │  │ OSM      │
       │  │ -map 0   │  │ Static   │
       │  │ probe    │  │ API      │
       │  └──────────┘  └──────────┘
       │                 │
       │         ┌───────┴───────┐
       │         ▼              ▼
       │  ┌──────────────┐  ┌──────────────┐
       │  │ cv2.imdecode │  │  piexif      │
       │  │ (OSM縮圖疊加) │  │ (JPEG EXIF)  │
       │  └──────────────┘  └──────────────┘
       │
┌──────┴───────────────────────────────────────────────┐
│                 Output Files                         │
│  - trimmed_video.mp4 (含 GPS track)                  │
│  - capture_001.jpg (含 EXIF GPS + 文字+縮圖疊加)     │
└─────────────────────────────────────────────────────┘

> 注意：`MapBridge` (QWebChannel) 已移除。Python→JS 透過 `runJavaScript()` 單向呼叫，
> JS 無需回呼 Python。詳見 `ui/map_view.py` 與 Phase 5 修復。
```

---

## 模組設計（實作狀態）

### 1. `utils/gps_parser.py` - GPS 資料提取 ✅

```python
@dataclass
class GPSPoint:
    lat: float
    lon: float
    time: float          # 相對於影片開始的秒數
    altitude: float = None
    speed: float = None


class GPSParser:
    """從影片檔案提取 GPS 軌跡資料"""

    def __init__(self, video_path: str): ...
    def has_gps_data(self) -> bool: ...
    def extract_gps_tracks(self) -> List[GPSPoint]: ...

    # 外部檔案解析
    @staticmethod
    def _parse_gpx(path) -> List[GPSPoint]: ...   # GPX 1.0/1.1, 時間自動相對化
    @staticmethod
    def _parse_nmea(path) -> List[GPSPoint]: ...   # NMEA 含時間提取
    @staticmethod
    def _parse_srt(path) -> List[GPSPoint]: ...    # SRT block 解析含時間戳

    # 內嵌 GPS 提取
    def _extract_gps_from_tags(self, data) -> List[GPSPoint]: ...  # metadata tags
    def _extract_from_data_track(self) -> List[GPSPoint]: ...      # data track NMEA
    @staticmethod
    def _parse_nmea_line(line) -> Optional[GPSPoint]: ...          # 單行 NMEA 解析
    @staticmethod
    def _parse_gps_tags(tags) -> Optional[Tuple[float, float]]: ... # tags 搜尋

    # OCR fallback (Phase 7)
    def _has_ocr_gps(self) -> bool: ...  # 取樣 1 幀測試 OCR
    def _extract_from_ocr(self) -> List[GPSPoint]: ...  # 完整 OCR 軌跡提取

    # 查詢
    def get_gps_at_time(self, seconds) -> Optional[Tuple[float, float]]: ...  # 線性插值
    def get_gps_bounds(self) -> Optional[Tuple[float, float, float, float]]: ...
    def to_gpx(self, output_path) -> bool: ...  # 匯出 GPX
```

> **偵測優先順序：**
> 1. 外部檔案（.gpx / .nmea / .srt）
> 2. 內嵌 metadata（ffprobe data track / GPS tags）
> 3. OCR fallback（EasyOCR 從畫面辨識 GPS 文字）
>
> **OCR fallback (Phase 7):** 當前兩種方式都失敗時，用 EasyOCR 取樣影片畫面辨識 GPS 座標。
> 支援格式如 `24.338460N 120.455859E 18Km/h`（行車記錄器畫面疊加文字）。
> 採用雙重策略：先裁切左上角 ROI，失敗時改用全畫面 OCR。

### 2. `utils/osm_map.py` - OpenStreetMap 縮圖 + Leaflet HTML ✅

```python
# 模組層函式（非類別）
def get_static_map(lat, lon, zoom=15, width=256, height=256, maptype="mapnik") -> Optional[bytes]: ...
def get_static_map_multi(points, width=400, height=300) -> Optional[bytes]: ...
def generate_map_html(tracks, center=None, zoom=15, provider="osm", google_maps_key="") -> str: ...
```

> **注意：** 實作採用模組層函式而非規劃中的 `OSMMapGenerator` 類別。
> `get_static_map()` 含 7 天快取機制。

### 3. `ui/map_view.py` - 地圖顯示元件 ✅

```python
class MapView(QWidget):
    """Leaflet 地圖顯示元件"""

    def load_tracks(self, tracks, provider="osm", google_maps_key=""): ...
    def update_position(self, lat, lon): ...          # runJavaScript() 呼叫
    def clear(self): ...
    def set_provider(self, provider, google_maps_key=""): ...
```

> **Phase 7 修正：**
> - 啟用 `QWebEngineSettings.LocalContentCanAccessRemoteUrls = True`，允許本地 HTML 存取 OSM 遠端圖磚
> - 使用 `setHtml()` 直接載入，移除暫存 HTML 檔案邏輯
> - 加入 `_page_ready` flag，確保 `updatePosition()` 僅在頁面載入完成後呼叫

### 4. `utils/gps_ocr.py` - OCR GPS 辨識 ✅ (Phase 7)

```python
class GPSOCR:
    """從影片畫面 OCR 辨識 GPS 座標"""

    def ocr_frame(self, frame) -> Optional[Tuple[float, float]]: ...
    def extract_tracks(self, video_path, interval_sec=1.0) -> List[dict]: ...
    def _crop_gps_region(self, frame) -> Optional[np.ndarray]: ...
    def _preprocess(self, roi) -> np.ndarray: ...
    @staticmethod
    def _parse_gps_text(text) -> Optional[Tuple[float, float]]: ...
    @staticmethod
    def _detect_cuda() -> bool: ...
```

> **OCR 策略：**
> - 先裁切左上角 ROI（依解析度比例 10%x45%）
> - 預處理：灰階 → 放大 2x → OTSU 二值化
> - ROI 辨識失敗時改用全畫面 OCR
> - 支援格式：`24.338460N 120.455859E`（含 N/S/E/W 方向）
> - 有 CUDA GPU 時自動啟用 GPU 加速

### 4. `core/controller.py` 修改 ✅

```python
# GPS 相關方法
def _get_current_gps(self, frame_idx) -> Optional[tuple]: ...
def _overlay_gps_text(self, frame, gps_coord) -> object: ...  # 含 OSM 縮圖疊加 (Phase 3)
def _write_exif_gps(self, image_path, gps_coord) -> None: ... # 僅 JPEG (Phase 5)

# GPS 查詢
def has_gps_data(self) -> bool: ...
def get_gps_tracks(self) -> list: ...
def get_gps_bounds(self): ...
def export_gpx(self, output_path) -> bool: ...
```

### 5. `core/batch_capture.py` - 批次截圖 GPS 支援 ✅

```python
class BatchCaptureWorker(QThread):
    def __init__(self, ..., gps_data=None, skip_overlay=False): ...
    def _get_gps_at_frame(self, frame_idx) -> Optional[tuple]: ...  # 插值查詢

# 模組層輔助函式（供 controller 與 worker 共用）
def _overlay_gps_frame(frame, gps_coord) -> object: ...
def _overlay_osm_thumbnail(frame, osm_bytes, thumb_size=160) -> object: ...
def _write_exif_gps(image_path, gps_coord) -> None: ...
```

> **關鍵修復 (Phase 2+3):** 批次截圖初始完全沒有 GPS；現已加入 GPS 文字疊加 + EXIF 寫入 + OSM 縮圖疊加。

---

## UI 設計

### 主視窗佈局 (左右分割 60/40)

```
┌──────────────────────────────────────────────────────────┐
│  選單列 (檔案 / 截取 / 導覽 / 標記 / GPS / 說明)          │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  ┌─────────────────────┐  ┌──────────────────────┐       │
│  │                     │  │                      │       │
│  │     影片播放區       │  │    GPS 地圖區        │       │
│  │     (60%)            │  │    (40%)             │       │
│  │                     │  │                      │       │
│  │   [video_label]     │  │   [QWebEngineView]   │       │
│  │                     │  │                      │       │
│  │                     │  │   Leaflet.js + OSM   │       │
│  │                     │  │   軌跡 + 當前位置     │       │
│  │                     │  │                      │       │
│  └─────────────────────┘  └──────────────────────┘       │
│                                                          │
├──────────────────────────────────────────────────────────┤
│  時間軸 [========================●===============]      │
├──────────────────┬───────────────┬──────────────────────┤
│   播放控制        │   截圖標記     │     輸出設定         │
│                  │               │                      │
│   ⏮ ⏪ ▶ ⏩ ⏭   │  開始/結束   │  fps/解析度/前綴    │
│                  │   清除        │                      │
│   速度: [1.0x]   │               │   影片擷取設定       │
│                  │               │   [ ] 使用原始編碼    │
│                  │               │   [ ] 保留音軌       │
│                  │               │                      │
│                  │               │   [擷取影片]         │
└──────────────────┴───────────────┴──────────────────────┘
```

### GPS 選單項目

```
GPS (新增選單)
├── 顯示 GPS 軌跡  [勾選框]
├── ─────────────
├── 影片已有 GPS 浮水印 (跳過疊加)  [勾選框]
├── ─────────────
└── 地圖來源
    ├── ● OpenStreetMap (預設)
    └── ○ Google Maps (需 API Key)
```

### 設定對話框

新增 `GPS 設定` 對話框：

```
┌─────────────────────────────────┐
│           GPS 設定               │
├─────────────────────────────────┤
│                                 │
│  [ ] 顯示 GPS 軌跡               │
│                                 │
│  [ ] 影片已有 GPS 浮水印          │
│      (跳過照片疊加)              │
│                                 │
│  地圖來源:                       │
│  ● OpenStreetMap               │
│  ○ Google Maps                  │
│    API Key: [________________当前無用的部分]        │
│                                 │
│  [確定]  [取消]                 │
└─────────────────────────────────┘
```

---

## 實作步驟（完成狀態）

### Phase 1: GPS 資料提取 ✅

1.  ~~安裝 `exiftool` / `GPSBabel`~~ → 改用 `ffprobe`（內建於 FFmpeg）+ 外部 GPX/NMEA/SRT 純文字解析
2.  ✅ 實作 `utils/gps_parser.py`
    - 偵測影片中的 GPS track（data track / metadata tags / 外部檔案）
    - 解析時間戳與座標（支援 GPX 1.0/1.1、NMEA $GPGGA/$GPRMC、SRT block）
    - 時間點線性插值查詢
3.  ✅ 擴展 `VideoInfo` model 加入 `has_gps` / `gps_track_count`
4.  ✅ 整合到 `Controller`：影片載入時自動提取 GPS

### Phase 2: 照片 GPS 疊加 ✅

1.  ✅ 實作 `utils/osm_map.py` - OSM 縮圖下載（`get_static_map`）+ 7 天快取
2.  ✅ 修改 `capture_single` / `batch_capture`
    - 讀取當前時間點 GPS
    - 下載 OSM 縮圖
    - 使用 OpenCV (`cv2.imdecode`) 疊加座標文字 + 160×160 OSM 縮圖（右下角半透明 blend）
    - 寫入 EXIF GPS（僅 JPEG，PNG 跳過）
3.  ✅ 加入「影片已有 GPS 浮水印」設定開關

> **Phase 2 修復紀錄 (2026-06-22):**
> - 批次截圖初始完全無 GPS 支援 → 已加入 `_get_gps_at_frame()` + `_overlay_gps_frame()` + `_write_exif_gps()`
> - OSM 縮圖初始未被呼叫 → 已整合至 `_overlay_gps_text()`

### Phase 3: 地圖顯示元件 ✅

1.  ✅ 建立 `ui/map_view.py` - QWebEngineView + Leaflet
2.  ✅ `map.html` 模板內嵌於 `generate_map_html()`（Leaflet + OSM Tiles）
3.  ❌ ~~QWebChannel 雙向溝通~~ → 實際 JS→Python 回呼無需求，已移除 `MapBridge`（Phase 5 清理）
4.  ✅ 整合到主視窗：QSplitter 左右分割 60/40

### Phase 4: 整合測試 ✅

1.  ✅ 所有模組整合完成
2.  ✅ `build_export_cmd(preserve_gps=True)` 含 `-map 0`
3.  ✅ 單張 ✅ + 批次 ✅ 照片 GPS 疊加
4.  ✅ 地圖同步顯示（Marker 跟隨播放 + throttling）
5.  ✅ 全部 17 項自動化測試通過

### Phase 5: Bug 修復 (2026-06-22 新增)

1.  ✅ `_extract_from_embedded()` 寫死假資料 → 真實 ffprobe 3 階段提取
2.  ✅ `_parse_gpx()` 絕對時間戳 → 相對影片時間（自動減 baseline）
3.  ✅ `_parse_nmea()` / `_parse_srt()` 時間永遠 0 → 正確提取
4.  ✅ 批次截圖無 GPS → 完整 GPS 支援
5.  ✅ GPS 設定對話框取消勾選時地圖不隱藏 → 正確 show/hide
6.  ✅ MapBridge 空殼 → 移除
7.  ✅ `setTracks()` JS 死碼 → 移除
8.  ✅ EXIF 寫入 PNG 可能失敗 → 僅 JPEG
9.  ✅ GPS 位置更新無 throttling → 每 15 幀更新一次

### Phase 6: GPS Bug 修復 (2026-06-22)

1.  ✅ `settings.py` — `show_gps_map` / `skip_gps_overlay` 從 QSettings 讀回時未從字串轉布林 → 加入轉換清單
2.  ✅ `controller.py` `capture_single()` — `_overlay_gps_text()` 就地修改 `self._current_frame`，重複截圖時文字重疊 → 傳入 `frame.copy()`
3.  ✅ `gps_parser.py` `_parse_nmea()` — GPGGA 句子未提取時間（field 1 含 HHMMSS.SS）→ 從 GPGGA/GNGGA 提取時間
4.  ✅ `gps_parser.py` `_parse_gpx()` — 無 `<time>` 的點產生負值相對時間 → 無 time 且已有其他定時點時跳過該點
5.  ✅ `controller.py` — GPS 節流計數器在 seek/step/skip 後未重置，地圖標記延遲更新 → 重置為 0

### Phase 7: OCR GPS Fallback + 地圖修正 (2026-07-04)

1.  ✅ `utils/gps_ocr.py` — 新增 EasyOCR GPS 辨識模組，從畫面文字提取座標
2.  ✅ `gps_parser.py` — `_detect_gps()` 加入 OCR 作為第三偵測選項（fallback）
3.  ✅ `gps_parser.py` — `_extract_from_ocr()` 每 1 秒取樣提取完整軌跡
4.  ✅ `map_view.py` — 啟用 `LocalContentCanAccessRemoteUrls` 修正 CORS 問題
5.  ✅ `map_view.py` — 改用 `setHtml()` + `_page_ready` flag 修正載入時序
6.  ✅ `osm_map.py` — 修正 attribution 單引號破壞 JS 字串

---

## 測試計畫（執行結果）

### 自動化測試（31 項，2026-07-04 全數通過）

| ID | 測試項目 | 預期結果 | 結果 |
|----|---------|---------|------|
| TC-01 | GPSPoint 資料類別 | `to_dict()` 正確序列化 | ✅ |
| TC-02 | `_parse_nmea_line` GPGGA | 解析緯度/經度/海拔 | ✅ |
| TC-03 | `_parse_nmea_line` GPRMC | 解析緯度/經度/速度 | ✅ |
| TC-04 | `_parse_gps_tags` (lat/lon) | 從 tags 提取座標 | ✅ |
| TC-05 | `_parse_gps_tags` (location) | 從 "lat,lon" 字串提取 | ✅ |
| TC-06 | GPX 相對時間 | 絕對時間戳轉為影片相對秒數 | ✅ |
| TC-07 | GPX 1.0 namespace | 正確解析舊版 GPX | ✅ |
| TC-08 | SRT 時間戳提取 | 從 subtitle timing 取得秒數 | ✅ |
| TC-09 | `get_gps_at_time` 插值 | 邊界、中間、clamp 皆正確 | ✅ |
| TC-10 | `get_gps_bounds` | 邊界框計算正確 | ✅ |
| TC-11 | NMEA 多點相對時間 | 多條 GPRMC 正確相對化 | ✅ |
| TC-12 | 單張疊加 `_overlay_gps_frame` | 文字成功寫入 frame | ✅ |
| TC-13 | EXIF PNG 跳過 | PNG 檔案不寫 EXIF | ✅ |
| TC-14 | Worker `_get_gps_at_frame` | 幀號→時間插值正確 | ✅ |
| TC-15 | OSM 縮圖安全跳過 | 空 bytes 不 crash | ✅ |
| TC-16 | Throttle 設定確認 | `_gps_throttle_interval` 預設 15 | ✅ |
| TC-17 | Controller / Worker 匯入 | 所有模組 import 正常 | ✅ |

### 手動測試案例（需實際影片與 PyQt5 環境）

| ID | 測試項目 | 預期結果 | 依賴 |
|----|---------|---------|------|
| MTC-01 | 載入含 GPS 的影片（內嵌 tags） | `has_gps_data() == True`，地圖顯示 | 實際 GPS 影片 |
| MTC-02 | 載入含 data track 的影片（NMEA） | 從 data track 提取 GPS 點 | 含 NMEA data track 的影片 |
| MTC-03 | 載入不含 GPS 的影片 | `has_gps_data() == False`，地圖隱藏 | 無 GPS 影片 |
| MTC-04 | 外部 .gpx 檔案與影片同名 | 自動載入外部 GPX | GPX 檔案 |
| MTC-05 | 外部 .nmea 檔案 | 自動載入 NMEA | NMEA 檔案 |
| MTC-06 | 外部 .srt 檔案 | 自動載入 SRT（含時間戳） | SRT 檔案 |
| MTC-07 | 影片擷取 (stream copy) | 輸出檔案包含 GPS track | `ffmpeg -map 0` |
| MTC-08 | 截取單張疊加 GPS | 圖片含 EXIF + 文字 + OSM 縮圖 | PyQt5 + 網路 |
| MTC-09 | 批次截取疊加 GPS | 所有圖片正確疊加 | PyQt5 + 網路 |
| MTC-10 | 跳過疊加（勾選浮水印） | 僅 EXIF，無視覺元素 | PyQt5 |
| MTC-11 | 開啟地圖顯示 | Leaflet 正確繪製軌跡 | PyQt5 + QWebEngine |
| MTC-12 | 播放影片同步地圖 | Marker 跟隨移動（throttled） | PyQt5 |
| MTC-13 | GPS 設定對話框開關 | 勾選/取消勾選正確顯示/隱藏地圖 | PyQt5 |
| MTC-14 | GPX 匯出 | 從 `to_gpx()` 匯出有效 GPX 檔案 | — |

---

## 相依性與限制

### 新增相依套件（已安裝）

```
# requirements.txt（GPS 相關）
piexif>=1.1.3           # EXIF 寫入（僅 JPEG）
requests>=2.25.0        # HTTP 請求（OSM tiles 下載）
easyocr>=1.7.0          # OCR GPS 辨識（fallback 方案）
opencv-python>=4.5.0    # 已安裝，OSM 縮圖解碼用（cv2.imdecode）
numpy>=1.21.0           # 已安裝，frame 疊加運算用
```

### 系統需求

- **FFmpeg 3.0+**: 需支援 `-map 0` 參數。GPS data track 提取依賴 `ffprobe -show_frames -select_streams d -show_data`
- **exiftool**: ❌ 不再依賴（已改用 ffprobe metadata tags 替代）
- **網路**: 首次使用時需下載 Leaflet.js CDN 與 OSM tiles；靜態縮圖有 7 天本地快取

### 已知限制（更新）

1.  **Google Maps 需 API Key**: 已實作 UI 開關，但預設為 OSM
2.  **OSM Static API 非官方**: 使用 `staticmap.openstreetmap.de` 第三方服務，可能不穩定
3.  **GPS 格式相容性**: 嵌入式 GPS 提取依賴 ffprobe data track 解析，不同廠牌行車記錄器的私有格式可能無法解析。當嵌入式提取失敗時，可放置同名 `.gpx` / `.nmea` / `.srt` 外部檔案替代
4.  **OCR 格式限制**: OCR fallback 僅支援座標格式如 `24.338460N 120.455859E`，其他格式（度分秒、中文標記等）需擴展 regex
5.  **OCR 取樣間隔**: 預設每 1 秒取樣一幀，快速行駛時可能遺漏轉彎處。可透過 `interval_sec` 參數調整
6.  **OCR 首次初始化慢**: EasyOCR 首次載入模型需數秒，後續呼叫較快
7.  **批次截圖 OSM 縮圖**: 以效能考量，批次模式下 **不** 疊加 OSM 縮圖（僅文字 + EXIF）。單張截圖才有完整縮圖疊加
8.  **EXIF 寫入僅 JPEG**: `piexif` 不支援 PNG EXIF，PNG 格式截圖跳過 EXIF 寫入

---

## 未來擴展

| 功能 | 說明 | 優先級 | 狀態 |
|------|------|--------|------|
| 更完整的嵌入式 GPS 提取 | Garmin GPMF、GoPro、DJI 等廠牌私有 data track 格式 | Medium | 🟡 已實作 ffprobe data track 通用提取 |
| OCR 格式擴展 | 支援更多行車記錄器 GPS 文字格式（度分秒、中文標記等） | Medium | 🟡 已支援基本座標格式 |
| Google Maps 整合 | 已實作 UI 切換 + API Key 設定，需使用者自備 Key | Medium | ✅ |
| 離線地圖快取 | OSM tiles 7 天快取已實作，但 Leaflet.js 仍需 CDN | Medium | 🟡 部分完成 |
| 速度資訊顯示 | 在地圖上顯示速度色彩（`GPSPoint.speed` 已提取） | Low | 🔲 |
| 多軌跡比較 | 同時顯示多條路徑 | Low | 🔲 |
| GPX 匯出/匯入 | `to_gpx()` 匯出已實作；匯入可透過外部檔案同名偵測 | Low | 🟡 匯出已完成 |
| NMEA 中 $GPGGA 時間 | 從 $GPGGA field 1 提取 HHMMSS.SS 時間 | Low | ✅ |

---

## 附錄

### GPS 資料格式參考

常見行車記錄器使用 **GPX** 或 **NMEA** 格式：

```xml
<!-- GPX 範例 -->
<trkpt lat="25.0330" lon="121.5654">
  <ele>50.0</ele>
  <time>2023-01-01T12:00:00Z</time>
</trkpt>
```

### EXIF GPS 寫入格式

```python
# WGS84 座標寫入格式
gps_info = {
    piexif.GPSIFD.GPSLatitudeRef: 'N',
    piexif.GPSIFD.GPSLatitude: ((25, 1), (2, 1), (0, 1)),
    piexif.GPSIFD.GPSLongitudeRef: 'E',
    piexif.GPSIFD.GPSLongitude: ((121, 1), (33, 1), (0, 1)),
}
```

### 參考資料

-   [FFmpeg Map 官方文件](https://ffmpeg.org/ffmpeg.html#Advanced-options)
-   [Leaflet.js 官方文件](https://leafletjs.com/)
-   [OpenStreetMap 圖資使用政策](https://operations.osmfoundation.org/policies/tiles/)
-   [PyQt5 QWebEngineView 文件](https://doc.qt.io/qt-5/qtwebengine-index.html)

---

**文件結束**
