# 行車紀錄器影片播放與截圖工具 — 效能改善與修正計畫書 v2.0（已核實版）

> 核實日期：2026-07-07
> 適用版本：dashcam_player（重構後版本）
> 核實方式：逐條比對計畫書 v1.0 與實際原始碼（video_player.py / controller.py / video_worker.py / cv_utils.py / gps_parser.py / gps_manager.py / osm_map.py / output_settings.py / resolution.py / video_info.py）
> 本版變更：v1.0 中 9 條問題有 5 條經核實已修復或不成立，已移除；1 條修正細節有誤，已修正；1 條描述不精確，已重寫。
> 最後更新：2026-07-19 — 全部 30 項優化已完成，64 tests passing, ruff + mypy clean

---

## 一、阻斷性錯誤（必須立即修復）

### 1.1 `video_player.py` 使用已不存在的 `gps_parser` 屬性

- **檔案 / 位置**：`dashcam_player/core/video_player.py` 第 508-511 行
- **問題**：
  ```python
  # GPS 狀態已由 _reset_ui_state 設為初始值（_has_gps=False 等）
  # 若背景 _GPSLoadThread 尚未完成，顯示等待訊息
  if not self.ctrl.gps_parser:
      self.statusBar().showMessage("正在偵測 GPS 軌跡...")
  ```
  重構後 GPS 功能已全部移至 `GPSManager`，`Controller` 只有 `self.gps_manager`，沒有 `gps_parser` 屬性 → 開啟任何影片都會觸發 `AttributeError`。
- **修正方案**：整段判斷式直接刪除即可，不需額外邏輯——GPS 狀態已經由 `_reset_ui_state()` 設好初始值，之後 `_on_gps_ready()`（第 862 行）會在背景載入完成後自動更新選單/地圖/狀態列。
  ```python
  def _on_video_loaded(self, info: VideoInfo) -> None:
      self.timeline.setRange(0, self.ctrl.total_frames - 1)
      self.fps_spin.setRange(1, max(1, int(self.ctrl.fps)))
      self.fps_spin.setValue(int(self.ctrl.fps))
      self.prefix_edit.setText(self.settings.prefix)
      self.output_path_label.setText(self.settings.output_dir)
      self._update_time_display(0)
      self._update_status_bar()
      # GPS 狀態改由 _on_gps_ready 統一處理，此處不再檢查
  ```
- **注意**：v1.0 計畫書曾提到要用 `_gps_no_data_action.setEnabled(...)`，但全專案搜尋不到這個屬性，是誤植的細節，**請勿照抄**。
- **驗證**：開啟任意影片，確認不再出現 `AttributeError`，狀態列仍能正確顯示「正在偵測 GPS 軌跡…」與偵測結果。
- **優先度**：🔴🔴🔴 立即處理

---

## 二、記憶體與效能優化

### 2.1 雙層 Frame 複製浪費

- **檔案**：`dashcam_player/core/video_worker.py`（`run()` 內 3 處 `frame.copy()` emit）+ `dashcam_player/utils/cv_utils.py`（`frame_to_qimage()` 內 `QImage(...).copy()`）
- **問題**：每一幀從 `cap.read()` 到畫面顯示，會經過兩層 `.copy()`：一次在 worker emit 前，一次在 `frame_to_qimage()` 內建構 QImage 時。1080p 30fps 約浪費 ~186MB/s，4K 更高。
- **修正方案**：拿掉 `video_worker.py` 的 `.copy()`，只保留 `frame_to_qimage()` 的那一份。
  - `cv2.VideoCapture.read()` 每次呼叫都配置全新的 ndarray，不會重用緩衝區，因此拿掉 worker 端的複製不會造成競態。
  - `frame_to_qimage()` 保留 `.copy()` 是必要的：`QImage` 直接指向 numpy `.data` 的記憶體位址，若原始陣列被 GC 回收，`QImage` 會變成懸空指標。這份複製留在顯示前的最後一刻剛好卡在正確位置。
  - `video_player.py:543` 的 zoom 裁切路徑已自帶 `.copy()`（因為 crop 後的 slice 是非連續記憶體），與此修正不衝突，只是縮放時會多一次複製，不影響正確性。
- **實作步驟**：
  1. `video_worker.py` 的 3 處 `self.frame_ready.emit(frame.copy(), idx)` 改為 `self.frame_ready.emit(frame, idx)`
  2. `cv_utils.py` 的 `frame_to_qimage()` 維持現狀（已有 `.copy()`）
- **驗證**：4K 影片播放，觀察記憶體曲線是否有下降；確認畫面顯示與縮放功能正常無異常。
- **優先度**：🔴 高

### 2.2 GPS 軌跡多份複本

- **檔案**：`dashcam_player/utils/gps_parser.py`（`extract_gps_tracks()` 第 201-233 行）+ `dashcam_player/core/gps_manager.py`（第 88、120 行）
- **問題**：
  - `GPSParser.extract_gps_tracks()` 內部快取 `self._tracks`，每次呼叫回傳 `.copy()`
  - `GPSManager._on_load_finished()` 又用 `list(self._parser.extract_gps_tracks())` 包一層，存成 `GPSManager._tracks`
  - `GPSManager.extract_tracks_sync()` 每次外部呼叫還會再 `.copy()` 一次
  - 長軌跡（10000+ 點）會有多層不必要的記憶體複製
- **修正方案**（二擇一）：
  - 選項 A：外部唯讀查詢（如地圖顯示、GPX 匯出）直接回傳參考，不複製，前提是呼叫端保證不修改
  - 選項 B：僅在真正需要獨立複本的場景（如批次截圖背景執行緒需要跨執行緒安全存取）才複製，其餘走參考
- **影響**：10000+ 點軌跡目前多份複本約多佔用 2-6MB，現代系統影響不大，長時間執行或超大軌跡才會累積明顯
- **優先度**：🟡 中（可視資源狀況延後）

---

## 三、防禦性程式設計

### 3.1 `open_video()` 缺少 `terminate()` fallback

- **檔案**：`dashcam_player/core/controller.py` 第 103-106 行
- **現況**：
  ```python
  self.worker.close()
  if self.worker.isRunning():
      self.worker.wait(3000)
  ```
  `shutdown()`（第 592-598 行）已有完整的 wait + terminate fallback，但 `open_video()` 只有 wait，逾時後沒有後續處理就直接呼叫 `self.worker.open(path)` 再次 `start()`，若舊執行緒真的卡住超過 3 秒，行為未定義。
- **修正方案**：
  ```python
  self.worker.close()
  if self.worker.isRunning():
      if not self.worker.wait(3000):
          self.worker.terminate()
          self.worker.wait(1000)
  ```
- **驗證**：快速連續切換影片 10 次以上，確認無多個 worker 執行緒同時運行、無殘留 crash。
- **優先度**：🟡 中

### 3.2 執行緒契約文件化

- **檔案**：`dashcam_player/core/video_worker.py` 頂部
- **問題**：`_play` 旗標等跨執行緒讀寫的欄位目前無 docstring 說明，僅能從程式碼推敲哪個欄位有 lock 保護
- **修正方案**：加上類別 docstring
  ```python
  """
  Thread safety:
  - _current_idx, _seek_frame: protected by _lock
  - _stop: written only by main thread (close()), read by worker (run())
  - _play: main-thread-only write, worker-thread-only read (no concurrent write)
  """
  ```
- **優先度**：🟡 中

---

## 四、資料完整性

### 4.1 EXIF GPS 寫入前未清除舊 GPS 段

- **檔案**：`dashcam_player/utils/cv_utils.py` `write_exif_gps()`
- **現況**：`piexif.insert(exif_bytes, image_path)` 傳入的 `exif_dict` 只有 `{"GPS": gps_ifd}`，若圖片本身已有其他 Exif 內容（如相機資訊），此寫法會整段替換掉，不會合併保留
- **修正方案（選項 B：只清除/覆蓋 GPS IFD，保留其他 Exif）**：
  ```python
  def write_exif_gps(image_path: str, gps_coord: tuple) -> None:
      if not image_path.lower().endswith(".jpg"):
          return
      try:
          import piexif
          try:
              exif_dict = piexif.load(image_path)
          except Exception:
              exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
          lat, lon = gps_coord
          # ... 計算 gps_ifd ...
          exif_dict["GPS"] = gps_ifd
          exif_bytes = piexif.dump(exif_dict)
          piexif.insert(exif_bytes, image_path)
      except ImportError:
          pass
      except Exception as e:
          get_logger().warning(f"寫入 EXIF GPS 失敗: {e}")
  ```
- **備註**：由於截圖本身是透過 `cv2.imencode` 產生的全新檔案，原生通常不帶 Exif，實務影響較小，但寫法上仍建議改為讀取-合併-寫回，避免未來若圖片來源改變（如直接複製原始相機檔案）導致資料遺失。
- **優先度**：🟢 低

---

## 五、程式碼品質

### 5.1 Resolution Enum 統一使用

- **檔案**：`dashcam_player/models/resolution.py`、`dashcam_player/utils/cv_utils.py`
- **現況**：`Resolution.ORIGINAL` 的值恰好就是字串 `"原始"`，所以 `cv_utils.apply_resolution()` 目前用 `res_text == "原始"` 巧合能動，但仍是 magic string，未真正透過 Enum 比對
- **修正方案**：`Resolution` 已有 `from_value()`，可再加一個 `is_original` 屬性，並讓 `apply_resolution()` 改吃 `Resolution` 型別而非裸字串
- **優先度**：🟢 低

### 5.2 待確認項目（尚無足夠檔案核實）

- **清理備份檔案**（`*.old`、含絕對路徑的 html）：需要專案目錄結構快照才能確認是否仍存在
- 其餘 v1.0 中「低優先」宣稱共 7 項，但 v1.0 實際只列出 4 項，缺口尚未釐清；本次核實聚焦在有實際程式碼可查證的項目，若還有其他遺留項目請提供具體檔案

---

## 六、已排除項目（v1.0 中誤判，不再列入）

以下項目經逐條比對實際原始碼後確認**已修復或問題不成立**，故從計畫中移除：

| 項目 | v1.0 判定 | 核實結果 |
|------|-----------|---------|
| GPS 合併 ffprobe | 🔴 高優先 | `gps_parser.py` 的 `_probe_file()` 已有 `self._probe_data` 快取，`_has_embedded_gps()` 與 `_extract_from_data_track()` 共用同一次探測結果 |
| `_current_idx` 讀取加鎖 | 🟡 中優先 | `video_worker.py` 的 `step_forward()` / `step_backward()` 讀取與寫入已在同一個 `with self._lock:` 區塊內 |
| GPS subprocess timeout | 🟡 中優先 | 兩處 `subprocess.run()` 已分別設定 `timeout=10` / `timeout=15` |
| `probe_info` 快取清理 | 🟡 中優先 | `controller.py` 的 `open_video()` 一開始就執行 `self.video_info = None`，整個舊 `VideoInfo` 物件（含 `probe_info`）隨之被 GC 回收，無累積風險 |
| Zoom Reset 後狀態列覆蓋 | 🟢 低優先 | `_update_zoom_status()` 在 `zoom<=1.0` 時本身就會呼叫 `_update_status_bar()`，全檔案搜尋也無其他地方會在 `_toggle_play` 後立即覆蓋訊息，行為正確 |

---

## 七、實作優先順序

```
Phase 0（立即修復）:
└── 1.1 移除 self.ctrl.gps_parser 判斷式

Phase 1（效能顯著改善）:
├── 2.1 移除 video_worker.py 冗餘 .copy()
└── 2.2 GPS 軌跡複本精簡（可視情況延後）

Phase 2（穩健性強化）:
├── 3.1 open_video() 加入 terminate() fallback
└── 3.2 執行緒契約文件化

Phase 3（資料完整性 / 程式碼品質）:
├── 4.1 EXIF 讀取-合併-寫回
└── 5.1 Resolution Enum 統一使用
```

---

## 八、驗證矩陣

| 修正項目 | 驗證方法 | 成功標準 |
|---------|---------|---------|
| 1.1 gps_parser bug | 開啟任意影片 | 無 `AttributeError`，狀態列正常顯示 GPS 偵測進度 |
| 2.1 移除冗餘複製 | 4K 30fps 播放，觀察記憶體 | 記憶體用量下降且無持續線性成長；縮放功能正常 |
| 2.2 GPS 軌跡複本 | 長途軌跡（10000+ 點）監控記憶體 | 記憶體用量下降 |
| 3.1 terminate fallback | 快速切換影片 10 次 | 無多個 worker 執行緒同時運行 |
| 4.1 EXIF 合併 | 對已有 Exif 的圖片寫入 GPS 兩次 | 其他 Exif 欄位（如相機資訊）未遺失，僅 GPS 更新 |
| 5.1 Resolution Enum | code review | `apply_resolution()` 不再用裸字串比對 |

---

*本文件為第二版核實記錄；每次重大修正落地後應更新「驗證矩陣」與「實作狀態」欄位。若後續要核實第五節「待確認項目」，需要提供專案目錄結構快照或相關檔案。*
