# 🗄️ 已歸檔：維修測試紀錄

> 本檔案為 `AGENTS.md` 原「## 維修測試紀錄」整節（第 257 行至檔尾）的原文搬移，一字不改。
> 歸檔日期：2026-10-26

---

## 維修測試紀錄

### 2026-08-28 完整 Code Review 與 Bug Fix

#### 已修正項目

1. **GPS OCR ROI 自動擴寬並持久化** (`utils/gps_ocr.py:143-155`)
   - 問題：ROI 寬度 0.205 導致「24.068」被切成「4.068」
   - 修正：偵測到 ROI 過窄（`<0.25` 且位於左上角）時自動擴至 0.32，並寫回 QSettings `gps_roi/{key}`

2. **PTS Loader AVI 容器提前線性回退** (`utils/pts_loader.py:105-118`)
   - 問題：AVI/mts/m2ts 容器 frame 級 ffprobe 極慢（30s timeout ×2）
   - 修正：於 `ffprobe_path` 檢查**前**判斷副檔名，直接走 linear fallback（10s 上限），避免雙重逾時

3. **SpeedManager ROI 快取失效同步** (`core/managers/speed_manager.py:85-92`, `ui/dialogs/speed_roi_adjust.py`)
   - 新增 `invalidate_roi_cache()`，對話框按下確定時呼叫，避免舊 ROI 殘留導致視覺測速誤判

4. **VideoWorker `_has_pending_seek` 執行緒安全** (`core/video_worker.py:245-255`)
   - 問題：`queue.get()` 非原子檢查導致競態
   - 修正：改用 `QMutex` 保護 `_command_queue`，每 20 幀檢查一次 pending seek

5. **GPSManager 背景載入去重** (`core/gps_manager.py:45-52`)
   - 新增 `_loading` flag，重複呼叫 `start_background_load()` 直接返回，防止雙重 OCR

6. **視覺測速滿寬 ROI 清理 + 自適應閾值回退** (`config/settings.py:249-253`, `utils/vision_speed.py:68-78`)
   - 拒絕儲存 `roi_w >= 0.98`（滿寬），避免 xcorr 鎖定 0/±1px 產生 2km/h 錯誤
   - `vision_speed.py` 改用 `adaptiveThreshold(Gaussian, 31, -15)` 為主，固定閾值為備援

7. **測試斷言修正** (`tests/test_models.py`)
   - 修正 `SpeedSettings.enabled` 預設值斷言（`False` 符合省資源預設）
   - `batch_capture.py` step 計算改用 `round(fps/capture_fps)` 一致性

8. **Controller shutdown 信號時序** (`core/controller.py:492-515`)
   - 新增 `_shutting_down` flag，關閉前先斷開 Manager→Controller 所有 signal 連接
   - 所有 relay handler（`_on_frame_ready`、`_on_video_info_ready`、`_on_position_changed`、`_on_play_finished`）加入 guard

9. **mypy/ruff 型別與風格修正**
   - `pts_loader.py`：`_probe_pts`、`_linear_fallback` 參數 `ffprobe_path: str | None`
   - `settings.py`：`log` 改從 `get_logger()` 取得
   - `gps_ocr.py`：修正 class method 縮進缺失（`extract_tracks`、`_parse_gps_text`）

#### 驗證結果

```bash
ruff check dashcam_player/           # All checks passed
python -m mypy dashcam_player/ --ignore-missing-imports  # Success: no issues found in 69 source files
python -m pytest dashcam_player/tests/ -q  # 236 passed, 2 skipped in 2.0s
```

新增測試：`tests/test_gps_ocr.py`（14 項：清洗/眾數修正/寬度限制/多尺度辨識）、`tests/test_vision_speed.py`（4 項：光流位移合成影像測試）。`tests/test_utils.py` 新增 3 項 PaddleOCR 連讀/顛倒格式測試。

### 2026-08-29 GPS OCR 辨識率 + 視覺測速辨識率改善

樣本影片 `PIC20260826-092148-F.AVI`（2560x1440, 30fps, 60s, H.264-in-AVI）實測。GPS 文字位於左上角 A 柱，~42px 高。

#### 已修正項目

1. **GPS OCR 前導數字截斷** (`utils/gps_ocr.py`)
   - 問題：PaddleOCR 對寬圖降取樣（2560 寬 crop 縮至 ~736，文字 <30px 偵測下限），前導「2」被裁掉（24.068061 → 4.068061）
   - 修正：新增 `_cap_ocr_width()`（模組級純函式，`_MAX_OCR_WIDTH=900`，角落側取窗）限制 OCR 視窗寬度；`_recognize()` 改多尺度（h<500 或 w<800 時 2.0x 優先、1.0x 備援）

2. **OCR 字元混淆清洗** (`utils/gps_ocr.py`)
   - 新增 `_clean_ocr_confusions()` + `_NUMERIC_TOKEN_RE` + `_OCR_CHAR_MAP`（O/o→0, l/I→1, B→8, Z→2），僅替換含小數點的數字子串，N/S/E/W 半球字母保留
   - `_match_gps_text` 解析前先清洗，還原 `24.06806lN` → `24.068061N`

3. **OCR 軌跡整數部分眾數修正** (`utils/gps_ocr.py`)
   - 新增 `_stabilize_ocr_tracks()`（同時支援 dict 與帶 lat/lon 屬性的物件）——整數部分離眾數 >1 度即還原（4.068 → 24.068）
   - 已接入 `GPSOCR.extract_tracks()` 回傳前後處理

4. **AVI FPS 中繼資料損毀修復** (`utils/gps_ocr.py`)
   - 問題：實測 30fps 影片 `cap.get(CAP_PROP_FPS)` 回傳 2560（=影片寬度），導致 `extract_tracks` 取樣間隔 2560 幀、整條軌跡只剩 1 個點
   - 修正：`fps` 超出 1~120 合理範圍時以「幀數 + 首末幀時間差」重新推導

5. **視覺測速預設 ROI 避開靜態引擎蓋** (`core/speed_worker.py`)
   - 問題：預設 0.55-0.92 含下方引擎蓋（幀間差 ~1.4）與上方過曝路面帶，phase correlation/xcorr 被靜態區鎖零位移（mean 0.01, std 0.46）
   - 修正：預設路面 ROI 改 0.45-0.75（含滿寬誤判 fallback），避開天空與引擎蓋

6. **光流法位移回退** (`utils/vision_speed.py`)
   - 新增 `_optical_flow_displacement()`：Farneback 光流取 ROI 下半 60%（近場）垂直分量的同向像素中位數，phase correlation/xcorr 失敗時回退
   - 低紋理路面（實測 62% 白）門檻放寬至 3%（至少 50 px），避免稀疏真實移動被當雜訊

#### 驗證結果

```bash
ruff check dashcam_player/           # All checks passed
python -m mypy dashcam_player/ --ignore-missing-imports  # Success: no issues found in 69 source files
python -m pytest dashcam_player/tests/ -q  # 236 passed, 2 skipped in 2.0s
```

新增測試：`tests/test_gps_ocr.py`（14 項：清洗/眾數修正/寬度限制/多尺度辨識）、`tests/test_vision_speed.py`（4 項：光流位移合成影像測試）。`tests/test_utils.py` 新增 3 項 PaddleOCR 連讀/顛倒格式測試。

### 2026-08-29 擷取檔名可溯性 / 防重複修復

#### 問題

測試發現不同來源影片擷取出的照片檔名相同，且無法識別來源檔案——只有「前綴 + 順序」。
根因：單張 `capture_manager.py` 用 `{prefix}_{timestamp}`、批次 `batch_capture.py` 用 `{prefix}_frame{idx:06d}`，
兩者都**不含來源影片名稱**，唯一區別是使用者 `prefix`（預設 `"capture"`），故不同影片在第 120 幀都產生 `capture_frame000120.jpg`。

需求：檔名須含來源檔案可識別文字 + 擷取編號/順序，且「同一來源不會產生 2 份相同資料不同名稱的檔案」。

#### 修正方案（真實幀編號 + prefix 保留為選用前標）

1. **新增 `utils/filename_utils.py`**（單一來源）：`sanitize_component` / `source_stem` / `normalize_ext` /
   `build_capture_filename(video_path, frame_idx, prefix, img_format)` → `{prefix}_{stem}_f{frame_idx:06d}.{ext}`
   （prefix 空白時省略前標）、`capture_glob(prefix, stem, ext)` 供衝突比對
2. **`capture_manager.py`**：`capture_single` 改用 `build_capture_filename(self._video_path, frame_idx, ...)` 取代 timestamp；
   `check_batch_conflict` 簽名加 `src_stem`，改以 `capture_glob` 比對（僅「同來源」舊檔視為衝突）；移除 `import time`
3. **`batch_capture.py`**：檔名改用 `build_capture_filename(self._video_path, best_idx, ...)`；**新增跳過已存在檔案**（同來源+同幀確定性同名 → 不產生第二份副本，直接 `success_count += 1` 並 `continue`）
4. **`controller.py`**：`check_batch_conflict` 加 `src_stem` 參數並下傳；新增 `current_source_stem` property（由 `self._video_path` 經 `source_stem()`）
5. **`video_player.py`**：`_capture_single` 的 `prefix` 改為允許空白（選用前標）；`_capture_range` 呼叫 `check_batch_conflict` 補 `current_source_stem`，確認對話框文案改為說明「同名=同來源+同幀，覆蓋不產生重複副本」
6. **`output_settings.py`**：「檔名前綴」標籤改「檔名前綴 (選用)」+ tooltip 說明來源名自動加入；`_accept` 允許空白 prefix（移除 `or "capture"`)

#### 防重複語意

檔名是 `(來源影片, 幀編號)` 的確定性函式 → 同來源同幀永遠同名（覆蓋/跳過），不同來源因 stem 不同而天然不撞名。
單張與批次現統一採「真實幀編號」命名（舊單張的 timestamp 格式已廢除）。

#### 已知限制

- 兩支**不同資料夾、但 basename 完全相同**的影片，stem 會相同 → 同幀仍會撞名（行車紀錄器實務極罕見，未特別處理）

#### 驗證結果

```bash
ruff check dashcam_player/           # All checks passed
python -m mypy dashcam_player/ --ignore-missing-imports  # Success: no issues found in 69 source files
python -m pytest dashcam_player/tests/ -q  # 239 passed, 2 skipped in 2.7s
```

新增/修正測試：`tests/test_filename_utils.py`（9 項：淨化/stem/ext/build/同來源同幀同名/不同來源不同名/glob）、
`tests/test_controller.py` 的 `TestCheckBatchConflict` 全面改為 `{prefix}_{stem}_f*.{ext}` 樣式並新增 `test_conflict_wrong_source`（驗證跨來源不撞名）。

### 2026-08-30 標線測速設計重構 + GPS 模組懶載入

#### 已修正項目

1. **邊緣穿越偵測器** (`utils/vision_speed.py`)
   - 新增 `compute_brightness_threshold()`：用直方圖雙峰中點取代 `mean_v + 35`（門檻須由訊號分佈解釋，規則 3）
   - 新增 `_linear_interpolate_edge()`：線性內插次幀精度
   - 新增 `detect_edge_crossings()`：偵測 B→W / W→B 邊緣穿越（任務 A 核心）
   - 新增 `_validate_duty_cycle()`：40%±15% 白線佔比驗證（台灣高速規範 4m/10m）
   - 新增 `estimate_speed_from_edge_crossings()`：雙軌道量測（1黑1白 + 1白1黑）

2. **距離常數鎖定** (`models/capture_settings.py`, `core/speed_worker.py`, `core/managers/speed_manager.py`, `utils/marking_detector.py`)
   - `SpeedSettings.distance_per_cycle` 預設改為 `MARKING_CYCLE_LENGTH_M=10.0`（台灣高速規範物理常數，非可調參數）
   - 新增 `HighwayMarkingSpec` dataclass（白線 4m、間隔 6m、週期 10m）

3. **校準模組移除 GPS 依賴** (`utils/speed_calibration.py`)
   - 移除 `gps_quality` 欄位、`MIN_GPS_QUALITY` 常數
   - 移除 `sanity_check_candidate` 中的 GPS 品質檢查
   - 重寫 `build_candidate_from_window()`：移除 GPS 參數，改用純視覺一致性（MAD）指標

4. **GPS 模組懶載入** (`core/gps_manager.py`)
   - `_GPSLoadThread` 新增 `run_ocr` 參數：**僅在 `run_ocr=True` 時呼叫 `has_ocr_gps()`**，`skip_ocr=True` 時直接跳過 OCR（不載入 PaddleOCR）
   - `start_background_load()`：發射 `gps_status_message` 顯示載入狀態
   - `_on_load_finished()`：完成時發射 GPS 偵測結果訊息
   - `extract_tracks_sync()`：呼叫 OCR 前也發射 `gps_status_message`

5. **PaddleOCR 載入訊息** (`utils/gps_ocr.py`)
   - `_get_paddle_ocr()`：新增 `log.info("PaddleOCR 載入中...")` 和 `log.info("PaddleOCR 載入完成")`

#### 驗證結果

```bash
ruff check dashcam_player/           # All checks passed
python -m mypy dashcam_player/ --ignore-missing-imports  # Success: no issues found in 71 source files
python -m pytest dashcam_player/tests/ -q  # 239 passed, 2 skipped
```

#### 已知限制
- `HighwayMarkingSpec` 為 module-level 常數，UI 尚無對應設定頁面
- `speed_calibration.py` 的 `build_candidate_from_window` 簽名已變更，舊呼叫端需同步更新
- `distance_per_cycle` 從可調參數改為物理常數，現有 QSettings 儲存的 `10.0` 值不受影響
- 工作單任務 B~E（人工標定、驗收比對、敏感度測試、GPS 事後驗證）尚未執行

### 2026-09-01 CRIT 4 項立即修復 + 開啟影片破圖優化 + .md 歸檔

**對應**：`開啟影片檔案格式偵測錯誤.md` 診斷（`nb_frames 26477 / 600fps` 實為 `time_base 1/600` 刻度，`nb_read_frames 223 / 5.05fps / 44.13s` 為真）、`維修測試紀錄.md` 精簡為時間線、`docs/archive/` 歸檔。

#### 已修正項目

1. **speed_calibration NameError** (`utils/speed_calibration.py:9`) — `import numpy as np` 從 `build_candidate_from_window` 內部上移至頂部，`np.median/corrcoef` 在 `len>=3` 分支不再 `NameError`
2. **vision_speed 存根化說明** (`utils/vision_speed.py:442`) — `detect_marking_displacement` / `estimate_speed_from_displacements` / `build_wiper_mask` 明確標為「工作單 1.5 #1 已證偽的 intentional stub」，SpeedWorker Stage1 取不到位移時回退 `target_fps=15`，主路為時間域 100m 法，不影響管線存活
3. **PTS cache 無鎖寫競態** (`utils/pts_loader.py:308`) — `load_pts_and_keyframes` 7 處 `_pts_cache[ck]=result` 全包 `with _cache_lock`，與讀端及 `_meta_cache` 一致
4. **_find_last_decodable_frame 主迴圈阻塞破圖** (`core/video_worker.py:360`) — 新增 `time_budget_ms=800` + `iters>20` 熔斷；`_run_loop:444` 優先採 `probe.actual_frames`（逐封包真值 223，毫秒級）免二分，無 probe 時才二分；有 probe 時 `fps = actual/duration` 免二次 `POS_MSEC` 阻塞
5. **開啟破圖 SSOT** (`utils/pts_loader.py:146` `probe_video_metadata` + `core/video_worker.py:34` `_resolve_playback_meta`) — 決策鏈 `真實 PTS > probe nb_read_frames/duration > 檔頭`，`nb_frames/fps` 僅在 `header_consistent && same_count` 時可信；`get_reliable_fps_count` 供 `gps_ocr/speed_worker` 共用，避免 `CAP_PROP_FPS 2560` 誤判
6. **.md 歸檔與樣本隔離** — `CODE_REVIEW/REFACTOR_REVIEW/FIX_PLAN` `git mv` 至 `dashcam_player/docs/archive/` 並加 `🗄️ 已歸檔` 橫幅；`.gitignore` 加 `*.AVI/*.avi/*_annotations.json/CH13_*`（私人樣本本地端）；`.gitattributes` 新增 `* text=auto eol=lf` + `core.quotepath false` 解決中文檔名幽靈 untracked

#### 驗證結果

```bash
ruff check dashcam_player/           # All checks passed
python -m mypy dashcam_player/ --ignore-missing-imports  # Success: no issues (71 files)
python -m pytest dashcam_player/tests/ -q  # 239 passed, 2 skipped
python -c "from dashcam_player.utils.speed_calibration import build_candidate_from_window; print('np import ok')"
```

#### 已知限制（新增）
- 任務 1 視覺測速幀級標定仍待 `B 人工標定`（3 支×2 段 30-50 幀肉眼）→ `C 幀號誤差 ≤1` 90% → `D 敏感度` 後才進 `E GPS 驗證`
- 任務 4 破圖「可查執行但仍破圖」已由 probe 捷徑 + 800ms 熔斷緩解，VFR 極端破圖仍需 `keyframe-aware seek` + `PTS frame_interval` 雙保障，後續以真實 60fps 素材重測
- `build_wiper_mask` 形態學水平 kernel 已證偽，現 `wiper_filter_enabled=False` 全白 mask，回退由時間域法承擔

### 2026-09-01 自動標示全 5 影片 + 人工 2 點標示工具重設計

**對應**：工作單任務 B 所需「自動候選 → 人工校正」流程；人工標示改為「2 點標示」語意（使用者裁定=同一虛線前端+末端兩點）。

#### 已修正/新增項目

1. **`manual_annotator.py` `_auto_detect_crossings` 重寫** (`utils/manual_annotator.py`)
   - 捨棄 box-mean + `detect_edge_crossings`（雜訊高），改用正確化後的 `detect_dash_bands`（暗路面上的亮虛線, median 自適應門檻）
   - 訊號=每幀框內亮虛線佔比；門檻由訊號本身非零佔比中位數的一半決定（`>=0.02`），不依速度回推
   - B→W / W→B 穿越在幀間線性內插求次幀精度（記錄 float frame）

2. **程式預設框 resolution-aware** (`utils/manual_annotator.py`)
   - `DEFAULT_BOX_2560_1440=(1000,830,1310,962)`、`DEFAULT_BOX_1920_1080=(900,400,1400,800)`；`_default_box_for_resolution()` 依解析度選框
   - **031548-R 實測 1920x1080**（資料導向發現）：原 2560 框在其上標線命中率 0/20 → 換 1080 框後 192 筆候選；證明單一固定框不適用跨同相機不同解析度
   - `_load_existing_box()`：批次續標時沿用既有 scan_box，不重複框選
   - `--auto-only` 在無 `--box` 時 headless 走既有框 / 解析度預設框

3. **人工 2 點標示重設計** (`utils/manual_annotator.py`)
   - 語意（使用者確認）：同一虛線「前端 F（前緣/最前端）」+「末端 R（後緣/最末端）」兩點，左右側標線各標一次
   - 程式框（掃描範圍）在人工確認階段**固定顯示、不可移動**；新增橙色「參考列」= 掃描框中線（程式穿越參考行）
   - 事件 schema：`{"kind":"2pt","side":"left/right","front":[x,y],"rear":[x,y],"frame":cur,"auto":false}`（raw 幾何，穿越幀由數據驗證後決定公式）
   - 操作：Tab 切左右側 → 滑鼠依序點 前端(F)/末端(R) → Enter 儲存；Esc 取消；`r` 移除（含 2 點）；保留 b/w 單點模式相容
   - `_draw_overlay` 新增 `pending` 參數渲染待放置點；`_draw_two_point_event()` 繪製已存 2 點事件與虛線長(px)
   - 底部說明列與狀態列改為 2 點優先顯示；header docstring 同步更新

#### 自動標示成果（5 支全部產出）

| 影片 | res | auto 候選 (bw/wb) | 框 |
|------|-----|------------------|-----|
| PIC20260825-180655-F | 2560x1440 | 172/173 (total 345) | 既有 scan_box x990-1262, y860-964 |
| PIC20260825-180755-F | 2560x1440 | 173/174 (347) | 2560 預設 |
| PIC20260826-091548-F | 2560x1440 | 66/66 (132) | 2560 預設 |
| PIC20260826-091548-R | 1920x1080 | 96/96 (192) | 1080 預設 |
| PIC20260826-092148-F | 2560x1440 | 108/108 (216) | 既有 scan_box x1056-1354, y818-962 |

#### 驗證結果

```bash
ruff check dashcam_player/utils/manual_annotator.py   # All checks passed
python -m mypy dashcam_player/utils/manual_annotator.py --ignore-missing-imports  # Success
python -m pytest dashcam_player/tests/ -q             # 242 passed, 2 skipped
```

#### 已知限制 / 待數據決定
- 2 點標示的「穿越幀」公式尚未鎖定：F/R 兩點 + 參考列 → 交叉幀由使用者據數據認證後再定（工作單 Task C 入口）
- 原 180655-F / 092148-F 的人工 bw/wb 標示已被 auto-only 覆寫（原始檔備份於 `H:/TEMP/opencode/ann_backup/`）

### 2026-09-01 2 點標示重做（垂直取點）+ 全 5 影片標記有效性命中率檢視

**對應**：使用者以 2 點工具標 180655-F 290 筆後，檢視發現無效；裁定「前端/末端」應為**垂直取點（前端右側/後端右側）**，非水平端點。

#### 檢視發現（資料導向）
- 290 筆人工 2pt 的「虛線長」中位數僅 **2px**（真實虛線垂直厚度 18-25px）→ 前端/末端被點成**水平左右端**，FR 間距無效；且全部只在左側(left=290, right=0)
- Task C 接受度（auto vs 舊人工基準）：180655-F bw 67.6%/wb 76.0% ≤1幀；092148-F bw 0%/wb 13.6%（有 ~1.5 幀**系統性參考偏移**，≤2幀 81-95%）
- 結構合理性全 5 支通過：bw/wb 對稱，週期中位數 8.8-10.9 幀，無重複/無範圍超界

#### 修正項目（`utils/manual_annotator.py`）
1. **`_snap_dash_band()` 新增**：在 x 欄位（±3px 窗平均）沿垂直以 median+25 門檻抓「含點擊處的連續亮帶」，上緣=前緣、下緣=後緣；無亮帶退化成單點。實測 thickness 18-25px（符合真實虛線厚度）
2. **`_mouse_2pt` 改為一鍵垂直吸附**：點虛線任意處即自動設 `front=上緣`、`rear=下緣`（不再點兩次）
3. **標籤改「前緣(上)/後緣(下)」**：`_draw_two_point_event`、pending 渲染、狀態列、help、docstring 同步更新；`dashcam` 訊息顯示「虛線厚 Npx」
4. **180655-F 重設**：清掉 290 筆無效 2pt，重跑 auto-only → 345 auto / 0 2pt（供使用者以垂直工具重標）

#### 驗證結果
```bash
ruff check dashcam_player/     # All checks passed
python -m mypy dashcam_player/ --ignore-missing-imports  # Success 73 files
python -m pytest dashcam_player/tests/ -q  # 242 passed, 2 skipped
# _snap_dash_band 實測：frame10=25px, 60=22px, 100=21px, 150=18px
```

#### 待辦
- 使用者以垂直 2 點工具重標 180655-F（從框選→auto→手動）
- 修正 auto 相對於人工基準的 **+1~2 幀系統性參考偏移**（092148 wb 中位差 1.5、bw 全 2-px off）後，驗證 ≤1幀接受度是否達 90%

### 2026-09-01 人工 2 點標示座標映射 bug 根因（窗座標 vs 影像座標）

**對應**：使用者以 2 點工具標 180655-F 後檢視發現 `front==rear` 且落在掃描框角落（如 `(1261,860)` / `(990,860)`），厚度 0-4px，與真實虛線厚度 18-25px 嚴重不符。

#### 根因（以 click_debug 實際數據確認）
- 錯誤版本把手動附著誤認/曾採「視窗座標」：`sx=(x-ox)*iw/rw`（減 `getWindowImageRect` 的 ox/oy）。
- 但 **OpenCV HighGUI 的滑鼠回呼座標本就是「影像內」座標**（0,0=顯示縮圖左上）。視窗座標解釋會把使用者點在虛線上的點誤映射到原圖**左上角**（例：原始 click `(564,455)` 誤算成 `orig=(426,760)`，遠離掃描框 990-1262/860-964），`_snap_dash_band` 找不到亮帶 → 退化成框角落單點。
- 正確映射僅需 `px = x/s`、`py = y/s`（s=顯示縮放比 0.5）。
- 驗證：原始 click `(564,455)` / `(563,456)` → `orig (1128,910)` / `(1126,912)`（在框內），snap 得 **21px** 厚度（正確）；`(592,439)` → `(1184,878)` 在框內（4px，貼框頂）；`(660,387)` → `(1320,774)` 在框外（clamp 角落、退化——使用者點在框右側外）。

#### 修正項目（`utils/manual_annotator.py`）
1. `_mouse_2pt` 移除 window-client 減 ox/oy 與 rw/rh 重縮放，改純影像座標 `px=x/s, py=y/s`；以顯示影像 w/h 判斷界內
2. 新增 `_CLICK_DEBUG_PATH = tempfile.gettempdir()/dashcam_annotate_click_debug.log`，每次左鍵點擊寫入原始 click、disp、s、映射後 orig、box——供未來座標疑難直接診斷

#### 驗證結果
```bash
ruff check dashcam_player/     # All checks passed
python -m mypy dashcam_player/ --ignore-missing-imports  # Success 73 files
python -m pytest dashcam_player/tests/ -q  # 242 passed, 2 skipped
# 實際 click 6 筆重算：2 筆在框內→21px、1 筆貼框頂 4px、1 筆在框右側外（退化）——映射已正確
```

#### 資料清理
- 舊 119 筆無效 2pt（窗座標誤映射）備份：`H:/TEMP/opencode/ann_backup/180655-F_with119bad2pt.json`
- 另 2 筆框角落退化 2pt 已清除（備份 `H:/TEMP/opencode/ann_backup/180655-F_beforefixdump.json`）；180655-F 現為乾淨 **345 auto / 0 2pt**

#### 待辦
- 使用者以修正後工具重標 180655-F：點虛線中央 → 應自動抓 前緣(上)/後緣(下)（厚度 18-25px）
- Task C ≤1幀接受度 ≥90%（先解決 auto 相對人工基準 +1~2 幀系統性偏移）

### 2026-09-01 人工標示重做為「單點穿越標記」（Option A）+ 語意裁定

**裁定**：使用者經兩輪提問確認 ground-truth 語意＝**Option A**——「白虛線前端（領先緣）掃過橙色參考列（掃描框中央，與 auto 判穿越同一參考平面）的瞬間，滑鼠點該點 → Enter 記錄目前幀為該條線穿越幀」。左右側以 Tab 切換。**捨棄**垂直 2 點(front/rear)取點。

**理由**：auto 穿越幀以「框中央參考列」判定；若手動改成「前端進整個框上緣」標記會有 `(上緣→中央距離)/每幀位移` 的**系統性 +N 幀偏移**（很可能正是先前 auto 相對人工基準 +1~2 幀偏移的來源）。用同一參考平面才能公平比對 Task C。

#### 修正項目（`utils/manual_annotator.py`）
1. 事件 schema 改 `{"kind":"cross","side":"left/right","point":[x,y],"frame":int,"time":float,"auto":False}`（取代 `kind:"2pt"` front/rear）
2. `_mouse_2pt` 改單點：左鍵點擊即設 `pending["point"]`（純影像座標映射，無吸附）；`current_side` 由 Tab 切換
3. `_draw_cross_event` 取代 `_draw_two_point_event`；刪除 `_snap_dash_band`（不再需要）
4. `_draw_overlay` 狀態列/說明列/docstring 全面改為穿越標記語意
5. 既有 `b/w`（黑→白/白→黑）單點補標模式保留相容

#### 驗證結果
```bash
ruff check dashcam_player/     # All checks passed
python -m mypy dashcam_player/ --ignore-missing-imports  # Success 73 files
python -m pytest dashcam_player/tests/ -q  # 242 passed, 2 skipped
# overlay 煙霧測試：cross 事件 + pending point 皆正常渲染
```

#### 待辦
- 使用者以「單點穿越標記」工具重標 180655-F（進場後逐幀看，前端掃過橙色參考列瞬間點一下→Enter）
- 校正 auto 相對人工基準的 **+1~2 幀系統性參考偏移** 後，驗證 Task C ≤1幀接受度 ≥90%

### 2026-09-04 Phase A 車體上緣跳變驗證 + Phase B `hood_detector.py` 新模組

**根因確認**：前鏡頭 4 支人工框底邊全部落在 0.643–0.668（均值 ~0.654）→ 該機器 `hood_top_y ≈ 0.65`；
舊碼三處寫死底邊（`SpeedWorker` 預設 ROI 0.75、`marking_detector` 掃描帶 0.76、`SpeedROIDetector` 掃描至 0.85）
比真實車體上緣深約 0.10H，`0.65–0.75` 反光帶即誤判來源。

**Phase A 數據結論**（腳本 `H:/TEMP/opencode/hood_phaseA*.py`，圖 `H:/TEMP/opencode/hood_phaseA/`）：
- 全寬 row 平均看不到邊界（車體只佔中央，兩側路面稀釋）；須限中央欄 `x[0.35,0.65]`
- motion 不可用——引擎蓋鏡面反光會動，hood 帶 motion 反而是高峰（純 motion 偵測漂移達 0.18H）
- 中央欄時間中位數 `V(y)` 穩定階躍：路面 V≈75–95 → 車體 V≈110–143（ΔV 33–54，四支一致）
- 由上往下取「第一個」階躍（0.78 另有更強邊，取全域最大會錯取）；後鏡頭無簽名回 None

**新增**（`utils/hood_detector.py`）：`median_v_profile()`（中央欄時間中位數 V 曲線，濾除暫態車輛）
+ `detect_hood_top_from_profile()`（純函式：ΔV≥25 且躍後 V≥110，後續 0.02H 不跌回）
+ `detect_hood_top(video_path)`（取樣 12 幀 10%–90% 均分，ffprobe 真值取代檔頭，同 `speed_roi_detector` 模式）
+ `CONSERVATIVE_FALLBACK_Y=0.64`（人工框底邊最小值 0.643 再扣一點，取代舊 0.75/0.76/0.85）
+ `pyproject.toml` mypy override 加 `tests.test_hood_detector`（`VideoWriter_fourcc` 不在 stubs）

#### 驗證結果
```bash
ruff check dashcam_player/           # All checks passed
python -m mypy dashcam_player/ --ignore-missing-imports  # Success: no issues found in 75 source files
python -m pytest dashcam_player/tests/ -q  # 258 passed, 2 skipped
# 實片：4 前鏡頭 err 0.002–0.032（容忍 0.04/0.045），後鏡頭回 None 無誤報
```

新增測試：`tests/test_hood_detector.py`（16 項：合成階躍/取首階躍/平坦/暗下半/過短/弱階躍/中位數濾暫態/合成影片端到端/缺檔/4 實片/後鏡頭/常數 sanity）。

#### 待辦（Phase C）
- 三處底邊下修：`speed_roi_detector.py:117` 掃描上限 → `min(0.85, hood_top_y − margin)`；
  `speed_worker.py:257/266` 滿寬退回 ROI `0.45–0.75` → `0.45–0.64`；
  `marking_detector.py:100` 掃描帶 `(0.50,0.76)` → `(0.50,0.64)`
- QSettings `解析度＋格式` key 降級為同機建議值：逐片偵測優先，偏差 >0.05H 棄用舊值（跨機器同解析度不互染）

### 2026-09-04 D 槽前鏡頭 10 支驗證 + 緩坡補強（雙窗）

**驗證範圍**：`D:\行車紀錄器影像\*-F.AVI` 共 10 支（2560x1440），`hood_detector` + `SpeedROIDetector` 現況。
疊加圖：`H:/TEMP/opencode/hood_verify_D/*.roi.png`（綠=SpeedROI、紅=hood_top、黃=保守底邊 0.64）。

**結論**：
- `hood_detector` v1（窄窗 0.03H）：9/10 檢出（0.641–0.675，與人工框帶 0.643–0.668 一致）；
  `091049-F`（正午強光）回 None——V 曲線顯示 hood 階躍攤成緩坡（0.65→0.75 才 +40，窄窗內僅 +10），未達門檻
- `SpeedROIDetector` 現況**異常**：10 支全部回滿寬 union ROI `(0.00,~0.4,1.00,~0.45)`、底邊 0.88，
  深入車體（反光區）——正是 Phase C 要修的 union 未 clamp 問題；現行 `SpeedWorker` 滿寬退回 0.45–0.75 仍含 0.65–0.75 反光帶
- 目檢 092148-F 疊加圖：紅線 hood_top 正好壓在引擎蓋前緣，綠框下探至儀錶板，證實誤判鏈

**補強**（`utils/hood_detector.py`）：窄窗找不到時以寬窗 `STEP_WINDOW_WIDE=0.06H` 重掃；
091049-F → 0.684，其餘 9 支偏移 ≤0.015（仍在人工帶內），後鏡頭維持 None 無誤報。
測試加 `test_gradual_ramp_caught_by_wide_window`（合成緩坡 0.63–0.72 即過）。

```bash
ruff check dashcam_player/           # All checks passed
python -m mypy dashcam_player/ --ignore-missing-imports  # Success: 75 files
python -m pytest dashcam_player/tests/ -q  # 259 passed, 2 skipped
# D 槽重測：10/10 OK（0.641–0.684）
```

### 2026-09-04 Phase B 重檢修正 + Phase C 三處底邊下修完成

**Phase B 重檢發現 5 項**（`utils/hood_detector.py`）：
1. 雙窗迴圈冗餘：自訂窗寬已 >= 寬窗時掃兩次 → 改 `scans` 條件組裝（窄+寬 / 僅一次）
2. **confidence 誤算**（會影響 Phase C）：原以固定窄窗重算 step，寬窗檢出（如 091049 緩坡，窄窗僅 +10）會被算成 0.0
   → 新增 `_detect_with_step()` 回傳實際檢出窗的 `(y, step)`；`detect_hood_top()` 改用它
3. `median_v_profile` 未防中途解析度變化（`np.stack` 拋 ValueError 非 `cv2.error`）→ 丟棄異形幀
4. `_sample_frames` 未使用的 `fps` 變數清理（只取 ffprobe `total`）
5. 寬窗門檻餘裕不足：合成緩坡（+40/0.10H，寬窗內僅 +24）壓線 → 新增 `WIDE_MIN_STEP_DELTA_V=20.0`
   （窄窗維持 25；後鏡頭在 寬窗+20 下仍 None，tests 鎖定）

**Phase C 改動**：
- `models/speed_roi.py`：`SpeedROISetting` + `hood_top_y`（跨機器 key 撞車比對用）；
  `SpeedROIDetectionResult` + `hood_top_y`
- `config/settings.py`：`save/load_speed_roi` 存取 `hood_top_y`（缺 key/損毀 → None，舊存檔相容）
- `utils/speed_roi_detector.py`：`detect()` 先跑 hood，候選掃描上限 `min(0.85, hood_top−0.02)`；
  `None`（後鏡頭無車體）維持 0.85 **不限縮**（不可用保守 0.64，後鏡頭有效路面到 0.74）；
  `_compute_union_roi(..., hood_top_y)` 底邊 clamp + 回傳 `cut_ratio`，切掉 >30% 高度 → confidence 0.7→0.4；
  新增 `HOOD_MARGIN=0.02`
- `core/speed_worker.py`：`_extract_roi` 預設 + 滿寬退回 `0.45–0.75` → `0.45–0.64`（`CONSERVATIVE_FALLBACK_Y`）；
  `run()` 開 cap 後加 `_validate_roi_against_hood()`（背景執行不卡 UI）：有存檔 ROI 且
  底邊 > hood_top+0.02 → 棄用走預設 + `status_message` 通知；無存檔/無簽名不動作（省 12 幀解碼）；
  新增 `status_message` signal；時間域薄帶 0.55–0.62 不動（已在車體之上）
- `core/managers/speed_manager.py`：連接 worker `status_message` → `_on_status_message` 轉發
  （Controller 已有 `speed.status_message → status_message` relay，鏈路直達 UI，不需改 Controller）
- `utils/marking_detector.py`：`scan_band` 預設 `(0.50,0.76)` → `(0.50,0.64)`（analyze/overlay/CLI 三處）；
  `utils/marking_test_runner.py` SCAN_BANDS 前鏡頭同步（後鏡頭 0.45–0.70 維持）；
  `utils/vision_speed.py` overlay 預設框 + 註解同步 0.64
- `ui/dialogs/speed_roi_adjust.py`：`_auto_detect` 記 `result.hood_top_y` → `_accept` 存入（純手調為 None）

```bash
ruff check dashcam_player/           # All checks passed
python -m mypy dashcam_player/ --ignore-missing-imports  # Success: 75 files
python -m pytest dashcam_player/tests/ -q  # 273 passed, 2 skipped
# D 槽 10 支 SpeedROIDetector 重測：ROI 底邊 0.620–0.655（原 0.88），全在 hood 之上，conf 皆 0.7
```

新增測試（`test_hood_detector.py` +11、`test_settings.py` +3）：
寬窗單次掃描 / `_detect_with_step` 實際窗 step / 異形幀丟棄 / 緩坡合成影片 confidence>0 /
union clamp（底邊/無hood不限縮/空）/ Worker 污染棄用·保留·無簽名保留·無存檔不偵測 /
settings hood roundtrip·舊存檔 None·損毀 None。

### 2026-09-04 標線偵測三機制（夜間閘門＋去趨勢切邊＋多帶合併）

**動機**：`D:/norm`（無 GPS，新相機）實測——傍晚可用（55–58km/h 三方互認）、
正午 0 脈衝（漣漪 ±1 被高架陰影淹沒）、夜間假速度（路燈混疊 cons 0.75 的 14.5km/h）、
施工區舊帶誤報 98.2。腳本 `H:/TEMP/opencode/norm_verify.py`，場景圖 `norm_verify/*.scene.png`。

**實測數據收斂閾值**（日/傍晚/夜 7 支）：
- 夜間 band 均值可達 100（頭燈+路燈），平均亮度**不可**分日夜；
  飽和比 sat：夜 0.028–0.073 vs 日 ≤0.013；天空：夜 49–55 vs 日 ≥122
- 雙條件 AND：`sky < 80 AND sat > 0.02`（晴天白車多也不會同時暗天空，避免誤閘；
  無燈具暗路不誤閘，本來就測不到）

**改動**（`utils/marking_detector.py`）：
- `assess_lighting()` 純函式＋`analyze_video` 開頭閘門：NIGHT 直接回空（`lighting` key 註記，
  安全方向不輸出速度）；線上統計不存幀
- `detect_marking_pulses` 去趨勢切邊：`same` 卷積頭尾各污染 win/2 幀
  （norm 實測兩端 ±40 假斜坡），邊界穿越不計；訊號短於窗長時不切
- 多帶訊號：掃描帶切 `N_STRIPS=4`，同 read 迴圈一次算完；`merge_strip_onsets()`
  跨帶投票（≥2 帶呼應，容忍 0.10s）；**雙軌**：全寬平均（降噪，單車道也抓到）為主、
  合併結果存在且一致性不輸時優先（`source` key 註記 full/merged）
- `MIN_PULSE_COUNT` 3→5：norm 112025 施工區 3 雜訊脈衝 cons=1.00 曾誤輸出 45km/h；
  正常高速 20s 窗內應有數十個
- `pyproject.toml` mypy override 加 `tests.test_marking_detector`

```bash
ruff check dashcam_player/           # All checks passed
python -m mypy dashcam_player/ --ignore-missing-imports  # Success: 77 files
python -m pytest dashcam_player/tests/ -q  # 292 passed, 2 skipped
# 實片：180655 49.1(GPS45.5)/180647 57.6 merged/夜間閘門擋14.5假速度/施工區正確拒判；
# 已知未解：092148/180755 週期高估~1.6x（疑變道混疊，待 Phase D tracker）、
# 正午小虛線靈敏度缺口（漣漪±1，多帶仍不足）、091548 切邊後 60→51（一致性低的估計變異）
```

新增測試：`tests/test_marking_detector.py`（12 項：閘門日夜/暗無燈/亮反光不誤閘/邊界尖峰/短訊號/
合併對齊·單帶雜訊丟棄·空/合成週期影片端到端 90km/h/缺檔）。

### 2026-09-04 去趨勢卷積邊界 bug（reflect 補邊）＋ 三區段重驗

**根因**（外部協作者逐值追蹤確認）：`detect_marking_pulses` 用
`np.convolve(signal, kernel, mode='same')` 做移動平均去趨勢，same 在邊界隱性補零，
趨勢線頭尾被拉向 0 → detrended 出現遠超本體的假斜坡（實測原始 sd 2.0 → detrended sd 12.4，
門檻被蓋過、脈衝全滅）。舊 `edge=win//2` 切邊治標：污染擴散比預期廣（win/n 大時），
且短窗丟 27-40% 數據。

**修正**：
- `utils/marking_detector.py`：新增 `detrend_moving_average()`（reflect 對稱補邊，公開函式供測試），
  `detect_marking_pulses` 改用並移除切邊邏輯
- `utils/vision_speed.py`：`build_temporal_brightness_signal` 內同型卷積 inline 同修
  （本模組被 marking_detector import，不可反向 import，故不共用函式）
- 測試：`TestDetrendReflect`（平坦訊號無假斜坡／邊界尖峰不再淹沒中段）；舊切邊測試更新語意

**三區段重驗**（092148：變道A 207-290／直行 291-1024／變道B 1025-1105）：
直行段 0→15 脈衝（T=0.717，50.2km/h，GPS 69.5，err 28%，cons 0.14）；
變道窗正確回空（無錯速，安全方向）。

**誠實記錄（打地鼠過程，結論見下）**：
- reflect 提高靈敏度→脈衝數全面上升，但中位數漂移不定：
  180655 47→56.8（GPS 45.5）、091548 60→56.8（GPS 63.9，11% ✓）、
  092148 全片 45→39（GPS 70.5）、180755 None→63.5 誤報（GPS 39.7）、
  施工區 112025 None→44 誤報、正午 122400 None→77（未驗證）
- 中間曾試「合併覆寫（cons 門檻 0.5）」：091548（merged 0.33>full 0.10 但 merged 全錯）
  與 180655（反向）證明**一致性無法預測正確性**，任何覆寫門檻都會在別支翻車 → 已回退：
  `analyze_video` 只用全寬訊號，`merge_strip_onsets` 保留為函式庫（測試保留）供 Phase D，
  選擇準則等雙機同路段真值到後再定
- 教訓：舊閾值（0.4sd／MIN5／選擇邏輯）是調在「含 bug 訊號統計」上的，修 bug 後須重調；
  在真值到來前停止微調，避免過擬合 D 槽 GPS

```bash
ruff check dashcam_player/           # All checks passed
python -m mypy dashcam_player/ --ignore-missing-imports  # Success: 77 files
python -m pytest dashcam_player/tests/ -q  # 293 passed, 2 skipped
```

**下一步候選**（待雙機真值，不先做）：自相關週期估計（抗漏檢/多檢的中位數替代），
在合成＋雙機 footage 上驗證後再換。

### 2026-09-05 人工標示閃爍對濾波＋刪除軌跡（R 刪除可追溯）

**動機**：自動標記仍有「前幀 bw、次幀 wb」閃爍（佔比訊號門檻抖動）與實線誤判；
使用者 R 刪除後無紀錄，無法分析錯誤模式、正確率無法提升。

**改動**（`utils/manual_annotator.py`）：
- 新增 `_remove_flicker_pairs()`（純函式）：同側異型間隔 <2 幀（四捨五入後）視為閃爍，
  捨棄較晚者。=2 只擋相鄰幀，200km/h 真實短亮態（~2 幀）不受影響。
  （既有 `_enforce_alternating_pattern` 只擋「同型過近」，擋不住異型相鄰——已接在管線後）
- 刪除軌跡：R 删除時把原始紀錄＋`removed_via`/`removed_box` 快照記入 `removed`；
  `_save_annotations(..., removed)` 寫 `removed_annotations`/`total_removed`（無則不寫 key，舊檔相容）；
  `_load_removed()` 讀回（續標顯示筆數，不復活）；重選框/重跑不清空（以 box 快照區分新舊）
- 新增 `--audit`：彙整目錄下 `*_annotations.json`（有效/移除/auto 佔比/閃爍對比例/型態x側別 top）

```bash
ruff check dashcam_player/           # All checks passed
python -m mypy dashcam_player/ --ignore-missing-imports  # Success: 78 files
python -m pytest dashcam_player/tests/ -q  # 303 passed, 2 skipped
# 既有 5 檔 auto 候選重跑閃爍濾波：180655 去 19、091548-R 去 44、092148 去 11（正是回報的相鄰閃爍）
```

新增測試：`tests/test_manual_annotator.py`（10 項：相鄰異型刪/正常 dwell 留/左右獨立/同型不動/空/次幀四捨五入/
軌跡 roundtrip/無刪除無 key/缺檔/舊檔相容）。

⚠️ 既有 `*_annotations.json` **未重跑**（避免動到人工標定對齊）；新 `--auto-only` 才含閃爍濾波。
待使用者累積刪除軌跡後，以 `--audit` 分析實線誤判模式（疑似縱貫全框高的連續亮帶，需數據定高寬比閾值）。

### 2026-09-05 自動標記左右同發修正（出現/消失絕對門檻）

**回報**：092148 左彎道，左側白線進入不標，右側進入第二幀時左右一起標 bw。
**根因**（逐幀量測 1205–1213）：舊碼上升門檻 `max(0.03, median*0.7)` 由各側歷史撐起
（左側全亮時佔比 0.17–0.23 → 門檻 ~0.10）；左細線進入時佔比僅 0.076  miss，
下一幀漲到 0.165 才觸發——恰與右側（0.064 > 右側低門檻）同幀，看起來像右帶動左。
根本問題：門檻混淆了「出現」（從無到有，細線也該觸發）與「強存在」。

**改動**（`utils/manual_annotator.py`）：
- 新增 `_side_crossings()` 純函式：bw = 佔比 0→超過 `eps_up=0.02`（出現），
  wb = 回落到 `eps_down=0.01` 下（消失，小滯回抗抖）；抖動由下游交替＋閃爍濾波處理。
  `_auto_detect_crossings` 改呼叫它（刪排除 historically-relative 中位數門檻）。
- 雙方保留、R 整刪維持現狀（使用者裁定）；JSON schema 不動。

```bash
ruff check dashcam_player/           # All checks passed
python -m mypy dashcam_player/ --ignore-missing-imports  # Success: 78 files
python -m pytest dashcam_player/tests/ -q  # 308 passed, 2 skipped
# 092148 --auto-only 重跑（輸出至 H:/TEMP/opencode/verify092148，未動使用者檔案）：
# bw 同幀左右對 49→0；1210 附近左側 bw 標在 1209（進入即觸發，不再拖到與右側同發）
# 總候選 68→93（門檻降低檢出變多，雜訊由使用者 R＋刪除軌跡回饋）
```

新增測試：`TestSideCrossings`（5 項：細線進入即觸發/消失 wb/全零/非零過少/eps 下抖動忽略）。

### 2026-09-05 穿越點點擊自動判側＋blink-off wb 修正（浮點比較）

**需求**：穿越點點擊後依中線 x 自動判側（右=右線，左=左線），Tab 只保留給 b/w 單點補標；
092148 右側 1160/1168/1176/1185/1194/1203 的 wb 為誤檢。

**改動**（`utils/manual_annotator.py`）：
- `_mouse_2pt`：點擊 x vs `selected` 中央中線自動定 side（`_draw_overlay` 同一分界），
  Tab 不再覆寫 pending side，只切 `current_side`（b/w 用）；help/docstring 同步
- `_remove_flicker_pairs` 改**浮點原值**比較（原四捨五入把 1.4–1.8 灌水成 2 而漏擋）：
  高速下偵測常只抓住標線一幀（blink：bw1166.4→wb1167.8 差 1.4），此類 blink-off wb
  捨棄、bw 保留。200km/h 真實 dwell（≥2.0）不受影響

```bash
ruff check dashcam_player/           # All checks passed
python -m mypy dashcam_player/ --ignore-missing-imports  # Success: 78 files
python -m pytest dashcam_player/tests/ -q  # 309 passed, 2 skipped
# 右側 1150-1214 六組 blink 對：wb 全 dropped、bw 全保留（1166/1175/1184/1193/1202/1210）
```

新增測試：blink-off 回歸（1166.4/1167.8 實測值，wb  dropped、bw 保留）。

### 2026-09-05 `--audit` 崩潰修正＋刪除軌跡首次分析（dwell 門檻 2→3）

**崩潰**：`--audit` 在 `_audit_annotations` 遇到 cross 類刪除紀錄（無 `type` key，值為 None）
即 `TypeError: '<' not supported`——排序鍵改 `str(type or kind)`＋分佈顯示 kind。另修 `F841`/`B905`。

**首次分析成果**（使用者 R 刪 87＋193 筆後跑 `--audit`）：
- 180655：移除 87 筆中 82% 為 auto，主導為 `wb/right`（53）；092148：移除 193 筆 90% auto
- 被刪 wb 的 dwell（與前同側 bw 間隔）中位僅 **3.3**、最小 2.3——該片 45km/h 真實 dwell 應 ~10 幀
  （duty 20% 遠低於規範 40%），證實為 blink 誤檢，非方法論清理
- 據此 `_remove_flicker_pairs` 門檻 2→3（覆蓋至 ~140km/h 名目 dwell 3.3；測速只用 bw–bw 間隔，
  激進濾 wb 不影響速度，僅影響 duty 驗證。dwell 在 [2,3) 的真標線（如 1210/1213 達 2.7）
  會被連帶濾掉——待 duty 相對式判斷（dwell vs 局部週期）取代固定門檻前接受此取捨）

```bash
ruff check dashcam_player/           # All checks passed
python -m mypy dashcam_player/ --ignore-missing-imports  # Success: 78 files
python -m pytest dashcam_player/tests/ -q  # 310 passed, 2 skipped
```

新增測試：dwell 2.x 捨棄／正常 ≥3 保留（舊「200km/h 不受影響」斷言已按新門檻修正）。

### 2026-09-06 -R 鏡頭門檻震盪疑似案例（待更多數據，暫不下結論）

**回報**：使用者人工標記 091548-R（後鏡頭）時，主觀感覺 420-450 幀附近有「標線
還沒進偵測框，自動標記卻先標出 bw，偵測範圍肉眼看沒有變化」的假交界，前鏡頭
（091548-F）也有類似疑慮。

**初步排查**（DEFAULT_BOX_1920_1080=(900,400,1400,800)，實測 f400-f459 `frac_l`
逐幀印出）：該範圍內 `frac_l` 多次在 1-2 幀內反覆跨越 `eps_up=0.02`/`eps_down=0.01`
（例：`f417:0.016→f418:0.020→f419:0.000→f420:0.020→f421:0.008→f422:0.064`），
用現行 `_side_crossings`（無 `_remove_flicker_pairs` 前）邏輯模擬會產出
`[(402,bw),(419,wb),(422,bw),(430,wb),(431,bw)]` 5 個候選。

**與既有 `_remove_flicker_pairs`（dwell<3 捨棄較晚者）的關係尚未完全釐清**：
- (430,wb)-(431,bw) gap=1<3 → 應會被現行機制濾掉
- (419,wb)-(422,bw) gap=3，**不小於 3**，用現行「<3」門檻推算不會被濾掉，
  但這對從數據上看仍疑似雜訊震盪而非真實標線出入——**是否為現行 dwell 門檻的
  邊界漏網案例，還是這組數據本身另有隱情，尚未有足夠樣本判斷**
- 曾試驗一種替代做法：跨門檻後要求連續 `min_persist=3` 幀維持同一狀態才確認
  （而非僅檢查「相鄰候選事件」的 gap），對這組 60 幀樣本可把 5 個候選壓到剩 1 個，
  但**只驗證過這一組數據，`min_persist` 的合適數值、以及跟現行
  `_remove_flicker_pairs`/`eps_up`/`eps_down` 三者該如何搭配（互斥？疊加？其一
  廢除？）都還沒有定論**

**下一步（待辦，非本次結論）**：
1. 用現行完整管線（`_side_crossings` → `_remove_flicker_pairs`）實際重跑
   091548-R 420-450 附近，確認現行機制究竟有沒有濾掉這批候選，不要用簡化模擬推算
2. 若現行機制濾不掉，再累積更多支影片/更多段落的實測樣本後，比較「dwell 門檻
   微調」vs「改為 `min_persist` 連續狀態驗證」何者更適合，或是否需要兩者並用
3. -F 鏡頭「有些位置也是」的疑慮同步比照辦理，不要只驗證 -R

**本節僅記錄疑似現象與初步排查方向，不視為已確認的 bug 或已驗收的修正。**

### 2026-09-04 低幀影片播放 pacing 修正（deadline 排程器，不補幀）

**需求**：`CH13_20260830112420_20260830112500.avi` 每秒僅 ~5 幀，播放一頓一頓。
使用者裁定：**不補幀、不平滑化、無硬編碼幀率**——依真實偵測 fps 自動調節，
真實幀在一秒內均勻分佈（如 5fps → 每 0.2s 一幀）；截圖/匯出維持原始來源。

**根因**（`core/video_worker.py` 舊 `_run_loop` 播放段）：
1. `last_emit_time` 在 `cap.read()+emit` **之後**才設錨，軟體解碼耗時逐幀疊加到
   `frame_interval` 上，整片越播越慢（5fps 間隔 198ms + 解碼數十 ms）
2. PLAY/SEEK/STEP/倍速切換/PTS 背景修正皆沿用舊錨點；錨點落後時多幀零等待
   連續播出 = 「一秒內把 12345 播完再停」的叢發來源

**改動**（`core/video_worker.py`）：
- 新增模組級純函式 `_compute_frame_interval(pts_list, current_idx, fps, target_fps)`：
  有 PTS 取相鄰差/速度倍率，無表或異常（`<=0`/`>1s`）回退 `1/target_fps`；
  間隔只由偵測值推導，無硬編碼
- `run loop` 改 deadline 排程：`next_deadline += frame_interval` 累加排程
  （解碼耗時不計入）；落後超一個間隔時丟棄欠帳從現在重排（不跳幀，序列完整）
- 錨點重置點：初始 / PLAY（含 `_ensure_cap` 重建耗時）/ SEEK / STEP_FWD/BWD /
  PTS 修正改變 fps 時（`next_deadline = None`）；`set_target_fps()` 設
  `_pace_reset_requested` flag 由 run loop 消費重置（換片重套亦涵蓋）
- 已知限制：VFR `>1s` 大間隔仍按既有防呆壓成 `1/target_fps`（總時長語意不變，
  本次不動）

#### 二輪優化：sleep 精準度 + PTS lock 節省

首輪修正後仍有可優化之處：

1. **sleep 精準度**（`video_worker.py:706-710`）：舊版 `remaining_ms * 0.8` 只睡 80%、
   尾段 busy-wait 20%（198ms 間隔 → ~30ms 空轉）。改為 `remaining - 0.002`（睡到差
   2ms）、尾段忙等 2ms。Windows QPC 精度 ~1ms、`msleep` 可能 overshoot 1-2ms，
   2ms 安全緩衝兜底，空轉從 ~30ms 降至 ~2ms。
2. **PTS lock 節省**（`video_worker.py:719-723`）：播放中 PTS 表極少變動（僅背景
   loader 完成時），播放段底部的 `position_changed` emit 前不再二次 lock PTS，
   復用迴圈頂部已鎖讀的 `pts_list_cur`。

```bash
ruff check dashcam_player/           # All checks passed
python -m mypy dashcam_player/ --ignore-missing-imports  # Success: 75 files
python -m pytest dashcam_player/tests/ -q  # 280 passed, 2 skipped
```

新增測試（`tests/test_video_worker.py` +7 `TestComputeFrameInterval`）：
5fps 均勻 0.2s / 30fps 正常 / 2 倍速減半 / 無 PTS 回退 / 零負大間隔回退 /
尾幀回退 / `set_target_fps` 置 pace_reset flag。
待實片驗證：播放 CH13 全片 wall-clock ≈ 44s（223 幀均勻 ~198ms，無叢發停頓）。

### 2026-09-04 三輪優化：PTS 修正錨點重置 + GPS speed O(n) 掃描快取

**根因**：低幀率（5fps）播放仍出現「每秒叢發 5 幀後額外停頓」，經診斷定位兩處：

1. **PTS 修正雙重重置**（`video_worker.py:622-624`）：PTS 背景 loader 完成時，
   `changed=True` 會 `next_deadline = None`（重置錨點）；同時 emit `video_info_ready`
   → UI 端 `_on_video_info_ready` 設 `_pace_reset_requested = True` → 下一輪迴圈
   再次 `next_deadline = None`。雙重重置導致 PTS 修正瞬間時間錨點中斷，5fps 播放
   每秒一次的「額外停頓」根因。

2. **GPS speed O(n) 掃描**（`gps_manager.py:202`）：`get_speed_at_time()` 每幀呼叫，
   內部 `any(p.speed is not None for p in self._tracks)` 線性掃描全部軌跡點。
   5fps 每秒 5 次 × O(n)（軌跡可達數千點），累積延遲不可忽略。

#### 修正項目

1. **移除 PTS 修正 `next_deadline = None`**（`video_worker.py:622-624`）
   - 舊碼：`changed=True` → `next_deadline = None` → 下輪 `now` 重設 → 立即解碼
   - 新碼：僅保留 `_pace_reset_requested` 機制（play/seek/step/speed-change 時重置），
     PTS 修正不再自行重置錨點，避免與倍速切換雙重重疊
   - 原理：`next_deadline` 是時間戳（非 fps 相關），fps 變化後下一幀的
     `_compute_frame_interval()` 自動採用新值，累積誤差由 `now - next_deadline > frame_interval`
     防呆兜底

2. **快取 `_has_speed_data` flag**（`gps_manager.py`）
   - `__init__` 新增 `_has_speed_data: bool = False`
   - `_on_load_finished`：tracks 載入後 `self._has_speed_data = any(p.speed is not None ...)`
   - `extract_tracks_sync`：同步提取後同理設 flag
   - `start_ocr_detection`：清除舊軌跡時重置 `False`
   - `get_speed_at_time`：`if not self._has_speed_data: return None`（O(1) 取代 O(n)）

#### 驗證結果

```bash
ruff check dashcam_player/           # All checks passed
python -m mypy dashcam_player/ --ignore-missing-imports  # Success: 75 files
python -m pytest dashcam_player/tests/ -q  # 280 passed, 2 skipped
```

#### 待實片驗證 → 已驗證，結案（非 bug）

播放 CH13 全片仍觀察到「5 幀順暢、之後多停頓一下」，一度懷疑三輪修正未生效。
直接以 `ffprobe` 逐封包量測 `CH13_20260830112420_20260830112500.avi` 的真實
dts（無 B frame，dts≈pts）間隔，取得決定性結論：

```
前40幀間隔(ms): [31.7, 33.3, 903.3, 31.7, 31.7, 33.3, 31.7, 871.7,
                 31.7, 31.7, 33.3, 31.7, 871.7, 31.7, 31.7, 33.3, 31.7, 871.7, ...]
共 45 群，每群穩定 5 張
群內間隔：31.7 / 31.7 / 33.3 / 31.7 ms   ← 約 31fps 連拍（對上 r_frame_rate=31/1）
群間間隔：871.7 ms                       ← 之後停拍近 0.9 秒，週期約 1.0s
```

**結論**：此檔案來源本身就是「每約1秒爆發式連拍5幀（~31fps、耗時~130ms）+
停拍~872ms」的節奏，並非均勻 0.2s 一幀。`_compute_frame_interval()` 依真實
PTS 差值排程，如實重現了這個節奏——群內 31-33ms 間隔感覺順暢、群間 872ms
空拍感覺是「多停頓一下」，兩者都正確對應真實拍攝時刻，**不是 bug**。

舊版「一開始瞬間播完5幀」與現在「5幀順暢+明顯停頓」其實是同一段
0.9 秒真實空拍時間，差別只在於落後補播時是否老實依真實間隔逐幀顯示
（新版）還是零等待瞬間吐出（舊版）；新版對「還原真實時間軸」更正確。

同時排除 `_frame_timer` 15ms coalescing 與 WebEngine 延遲：兩者量級（15ms）
相對真實 872ms 空拍可忽略不計，非停頓來源，不需再往此方向查。

**若之後想改變體感**（三選一，皆非目前裁定範圍，需另行評估）：
1. 維持現狀（推薦）——時間軸精確、GPS/速度同步準確、忠於來源。
2. 均勻化播放節奏——`_compute_frame_interval()` 改為一律回傳 `1/target_fps`
   不讀 `pts_list` 差值；犧牲時間精確度換視覺均勻，GPS/速度疊圖會跟著失真。
3. 視覺過場緩解——只改 UI 呈現層（`video_player.py`），偵測長間隔
   （如 >500ms）時加淡入/淡出或輕微模糊轉場，不動 worker 時間精確度。

#### 附帶驗證：排程邏輯是否幀率無關（10/15/20/60fps）

CH13 案例確認後，另外驗證了一個延伸問題：`_compute_frame_interval()` +
deadline 排程是否只在 5fps/VFR 這種特例下才「湊巧」正確，換成其他幀率
（尤其常見的 CFR 10/15/20/60fps）會不會有隱藏的邊界問題（例如高 fps 下
16.7ms 級間隔被解碼耗時或 sleep 精度吃掉）。

**結論：不需要另外找真實素材驗證**——`_compute_frame_interval()` 本身沒有
任何幀率相關的寫死分支，純粹由 `pts_list` 相鄰差值算間隔，邏輯上對任意
幀率皆成立。用 `ffmpeg` 合成 CFR 測試片直接端對端量測即可證明，不需要
到處蒐集不同機型/幀率的真實檔案。

`scripts/verify_pacing.py`（逐字複製 `video_worker.py` 的
`_compute_frame_interval` + deadline 排程迴圈，真實 `cv2.read()` 解碼、
真實 `ffprobe` PTS，量測 wall-clock 是否等於檔案真實長度）：

```python
"""複製 video_worker.py 的 _compute_frame_interval + deadline 排程邏輯，
用真實 cv2 解碼 + 真實 ffprobe PTS，量測 wall-clock 是否等於影片真實長度。
不改動任何原始碼，純粹拿相同演算法對不同幀率的合成影片做端對端驗證。
用法：
    ffmpeg -y -f lavfi -i "testsrc=duration=5:size=320x240:rate=<FPS>" \
        -c:v libx264 -pix_fmt yuv420p synth_test/test_<FPS>fps.mp4
    python scripts/verify_pacing.py
若對某支「真實」可疑檔案有疑慮，把該檔案路徑加進最下面的清單即可，
不限合成片。
"""
import json
import subprocess
import time

import cv2


def _compute_frame_interval(pts_list, current_idx, fps, target_fps):
    """逐字複製自 video_worker.py，未做任何修改。"""
    speed_factor = target_fps / fps if fps > 0 else 1.0
    if pts_list and current_idx + 1 < len(pts_list):
        current_pts = pts_list[current_idx] if current_idx < len(pts_list) else 0
        interval = (pts_list[current_idx + 1] - current_pts) / speed_factor
        if 0 < interval <= 1.0:
            return interval
    return 1.0 / target_fps if target_fps > 0 else 0.033


def load_real_pts(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "packet=dts", "-of", "csv", path],
        capture_output=True, text=True,
    ).stdout
    tb_out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=time_base", "-of",
         "default=nk=1:nw=1", path],
        capture_output=True, text=True,
    ).stdout.strip()
    num, den = tb_out.split("/")
    tb = int(num) / int(den)
    dts = [int(line.split(",")[1]) for line in out.splitlines() if line.strip()]
    return [d * tb for d in dts]


def real_duration(path):
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", path], capture_output=True, text=True).stdout)
    return float(info["format"]["duration"])


def simulate_playback(path, fps, target_fps=None):
    target_fps = target_fps or fps
    pts_list = load_real_pts(path)
    cap = cv2.VideoCapture(path)
    idx = 0
    next_deadline = None
    t_start = time.monotonic()
    while True:
        frame_interval = _compute_frame_interval(pts_list, idx, fps, target_fps)
        now = time.monotonic()
        if next_deadline is None:
            next_deadline = now
        if now - next_deadline > frame_interval:
            next_deadline = now
        if now < next_deadline:
            remaining = next_deadline - now
            if remaining > 0.002:
                time.sleep(remaining - 0.002)
            continue
        ret, frame = cap.read()
        if not ret:
            break
        idx += 1
        next_deadline += frame_interval
    cap.release()
    wall_clock = time.monotonic() - t_start
    return idx, wall_clock


for fname, fps in [("test_10fps.mp4", 10), ("test_15fps.mp4", 15),
                    ("test_20fps.mp4", 20), ("test_60fps.mp4", 60)]:
    path = f"synth_test/{fname}"
    expected = real_duration(path)
    frames, wall = simulate_playback(path, fps)
    diff_ms = (wall - expected) * 1000
    print(f"{fname:16s} 真實長度={expected:6.3f}s  播放張數={frames:4d}  "
          f"wall-clock={wall:6.3f}s  誤差={diff_ms:+6.1f}ms")
```

**2026-09-04 驗證結果**（合成 CFR 測試片，5 秒長）：

```
test_10fps.mp4   真實長度=5.000s  播放張數= 50  wall-clock=5.000s  誤差=+0.4ms
test_15fps.mp4   真實長度=5.000s  播放張數= 75  wall-clock=5.000s  誤差=+0.2ms
test_20fps.mp4   真實長度=5.000s  播放張數=100  wall-clock=5.000s  誤差=+0.2ms
test_60fps.mp4   真實長度=5.000s  播放張數=300  wall-clock=5.000s  誤差=+0.2ms
```

誤差全部 <1ms，含 60fps（16.7ms 級間隔）也未被解碼耗時或 sleep 精度吃掉。
確認排程邏輯對任意幀率成立，非 5fps/VFR 特例下才湊巧正確。

**以後重跑方式**：懷疑任何幀率/來源的播放節奏有問題時，
1. 若是合成測試：`ffmpeg -f lavfi -i "testsrc=duration=5:size=320x240:rate=<FPS>" ...` 生成後跑 `python scripts/verify_pacing.py`。
2. 若是「真實可疑檔案」：把該檔案路徑加進清單重跑，可同時看到真實 PTS
   節奏（是否像 CH13 一樣是爆發式）與 wall-clock 誤差，兩者一次確認，
   不需要肉眼盯著畫面猜是不是 bug。

### 2026-09-06 車道事件掃描（變道過渡 / 單側實線 / 標記中斷）+ Task C 指標腳本

**動機**：auto 標線候選在直行段可用，但對變道無識別能力（固定框＋固定中線）；
變道後常見單側實線，實線側佔比恆高閂鎖，需註記而非當漏檢。另需回答 Task C 現況。

**新增**（`dashcam_player/utils/lane_events.py` 共用純函式庫＋兩支薄 CLI）：
- `scripts/eval_cross.py`：Task C 驗收（auto-bw vs 人工 cross，per-side greedy，tol=1.0/recall≥0.90）。
  基準線：前鏡頭 4 支 R≈0.00–0.08（系統性 bias −1.7f，p10–p90 僅 ~0.8f，訊號本身好）、
  後鏡頭 091548-R R=0.91/P=0.94（bias 恰 −1.00f）。另抓到 auto 首幀負幀號候選（插值邊界產物，eval 排除）。
- `scripts/scan_lane_events.py --validate`：5 支全 PASS（P=1.00/R=1.00）。
  規則：solid（恆高≥150f＋對側起伏）/ single（歸零≥90f＋對側有訊號）/
  transition（雙側缺口重疊 or 單側暫斷 35–150f 後恢復）/ gap_both。
  門檻皆以訊號分佈解釋（實線 0.19–0.28/虛線間隙 0.0/雜訊<0.02；週期 9–11f 為尺）。
- `scripts/summarize_scan.py`：報告 prepend P0/P1/P2/— 優先順序總表。
- 測試 `tests/test_lane_events.py` 19 項全綠。

**執行成果**：`D:\norm` 111 支＋`D:\行車紀錄器影像` 20 支 stride=3 全掃，
報告 `H:/TEMP/opencode/lane_scan_norm.md`（87 支有事件）/
`lane_scan_dashcam.md`（15 支有事件），P0 六段（091548-F/R f650–800、
092148-A/B、092148 單側段、091548 實線段）皆在列。P0 清單與人工動作見報告總表。

**途中抓到的真問題**：
1. stride 混疊：stride=5 在 10f 週期虛線上造出 f890–1019 假 single（同檔 stride=3 無事件，
   同檔 md5 確認）。`compute_occupancy_series` docstring 已載明約束（stride≤3，未知車速用 2），
   並有合成迴歸測試；D 槽兩份報告皆為 stride=3 重掃版。
2. 事件 end 外推超過片尾（如 f1809/1799）：`_merge` 統一 clamp 至取樣末幀。
3. `validate_events` 初版 `solid_X→single_X` 同側對應錯誤（應為對側），測試抓到已修。

**已知限制（誠實記錄）**：
- 掃描規則只在舊相機日間 5 支上驗證（P/R=1.00）；D 槽（新相機＋夜間）precision 未驗證，
  報告皆為「候選」，需人工抽查回饋後再調。夜間 single_left 偏多疑為頭燈下遠側虛線不可見，
  屬已知靈敏度缺口而非變道，優先看日間 P0。
- solid 事件起點有 persistence 延遲（091548 f786 vs 真值 f750、092148 f381 vs f270），
  邊界供人工定位用，不可直接當真值。
- `test_manual_annotator.py::TestSideCrossings` 2 項失敗為**預存**（untracked 新版
  `_side_crossings` min_persist=3 確認延遲 vs 舊斷言語意：105.0 vs 期望[104,105)、
  wb 需 3 幀確認而樣本僅 2 幀），本次未動（屬 Phase 1 參考平面/邊界慣例範圍）。
- 通用帶（generic_band）影片左右僅畫面中央切分，人工框選後為準。

### 2026-09-06 人工標示續標流程資料遺失修復（重設框架清空人工標記）

**回報**：續標 Y＋重設框架 Y 會刪掉人工標記（或沒載入舊資料），進程式後看不到人工標記。
**根因**（`utils/manual_annotator.py::_process_video`）：重選框後 `existing_annotations = []`
把人工（cross＋b/w 補標）連同舊 auto 一起清空；另 `--box` 指定新框時沿用舊框產生的
auto 候選；`--auto-only` 重跑直接覆寫全檔（含人工，曾發生 180655-F/092148-F 人工被覆寫事件）。

**修正**：
- 新增 `_split_manual_auto()` 純函式（人工 = `auto` 欄位 False）；`box_changed` flag：
  新框只丟棄舊程式候選，人工保留＋重跑 auto 後合併（互動重選框／`--box` 新框／`--auto-only` 三路統一）。
- `--auto-only` 永遠重跑（否則演算法更新後重跑無效），存檔訊息改報「程式 N／人工 M」。
- 「重新開始（n）」語意不變（才清空全部）。

**驗證**：`tests/test_manual_annotator.py::TestManualPreservedOnRebox` 3 項（split／重框合併 roundtrip／restart 區分）；
實片端到端：091548-R 標定檔複本 `--auto-only` 重跑 → 人工 251 筆完整保留、auto 410→419 更新、removed 47 筆保留。
`ruff`／`mypy` 全綠；`TestSideCrossings` 2 項預存失敗未動。

### 2026-09-06 人工標示互動優化（Tab 側綠框＋點擊後翻幀自動儲存）

**需求**：黃色框分不清 Tab 在哪側；滑鼠點擊後還要按 Enter 多餘。

**改動**（`utils/manual_annotator.py`，皆 headless 可測部分有測試）：
- `_draw_overlay` 新增 `active_side` 參數：Tab 所在半框描綠（3px）＋頂部 `Tab:左/右側` 標籤；
  overlay 快取鍵加入 `current_side`，切 Tab 即時重繪。
- 新增 `_commit_pending()`：點擊設點後，n/p/`[`/`]`/`.`/`,`/g/s 一律先提交再動作，
  Enter 為立即儲存，Esc/q 照舊取消（pending 未設點時不存）。
- 說明文字同步（docstring／狀態列／help1／點擊提示）。

**驗證**：`TestActiveSideOverlay` 3 項（左右綠邊像素斷言、無側無綠）；
`pytest` 333 passed（2 項 `TestSideCrossings` 預存失敗未動），`ruff`／`mypy` 全綠。

### 2026-09-08 UFLDv2 Spike 實測（D:/norm 10 段＋bronze 真值，決策：混合策略待整合）

**動機**：短虛線／不同標線規格＋惡劣天候下傳統灰階 Threshold 失效；驗證預訓練
車道模型（UFLDv2-CULane res18）能否作為訊號源。Spike 先行，不動 shipped 測速管線。

**關鍵前置發現（實測，非假設）**：UFLDv2 輸出車道「幾何折線」（連續內插，不隨虛線
間隙脈動），**不能直接產生 bw/wb 脈衝**。Spike 改比三路訊號（下游共用
`detect_marking_pulses` 脈衝提取＋同一 bronze 真值，paired comparison）：
  classical — `detect_dash_bands` 寬框佔比（基線）
  ai-conf   — row-softmax 信心時序（第一輪即證偽：cons ≤0.72 全敗；模型在間隙
              照樣自信內插，機制不成立，已放棄）
  ai-geo    — AI 車道 x 定窗（±60px）＋窗內傳統中位數+35 亮部比例

**樣本**：`H:/TEMP/opencode/spike_samples.json` 10 段（陰天/強光/槽化線/施工/匝道/
黃昏雨×2/夜間×2，皆 2560x1440 30fps）。模型／權重在暫存（`H:/TEMP/opencode/ufldv2`
clone＋`culane_res18.pth` 825MB），**不進 repo、不進 requirements、不進 exe**；
`torch` 為 scripts 端 lazy import。RTX 3060 推論中位數 17ms/幀（batch=8）。

**新增檔案**：`utils/lane_mask.py`（torch-free 純函式：光柵化/佔比/定窗；
破例 `import numpy`——Mask 為 uint8 陣列別無他法，同 cv_utils 性質）、
`tests/test_lane_mask.py`（13 項）、`scripts/eval_mask_spike.py`（三路對比＋gallery）、
`pyproject.toml` mypy override（cv2.polylines 單通道 stub）。

**Bronze 真值方法**：參考列±4列瀑布盲判（S1/S4/S6/S9 共 4 窗×2 側，~200 事件）＋
8 點全幅抽查校準 → **無系統偏差（mean +0.125），單點 noise ±1-2 幀**；
故 tol=1.0/2.0 並列，結論以 paired delta 為主。S6-R f426-500 車輛遮擋列 exclude。

**結果（脈衝級 Task C，tol=2.0 R）**：
- S9-R（夜間窄虛線）：classical **R=0.00（全盲，99% 零）** vs ai_geo **R=0.54/P=0.62**。
  根因已定位：夜間窄虛線欄佔比恒 < `min_line_ratio=0.25`（寬框分母稀釋），
  `detect_dash_bands` 整窗棄檢；AI 窄窗分母小→幅值正常。純訊號分佈解釋，合鐵則§3。
- S1/S4/S6-L：兩路打平（ΔR ≤0.05），AI 無系統偏移（bias 差 ≤1≈bronze noise）。
- S5b-L：AI 車道身份逐幀跳變（護欄陰影）→ ai_geo cons=0.05 自我宣告不可信。
  Phase 3 若做，需時序車道 ID 追蹤＋「classical 死＋AI 強才切換」的保守閘。
- S2：雙路皆 ~0 脈衝，gallery 證實為槽化線＋車輛遮擋（正確靜默，非失效）。
- S6-R：遮擋段 classical 幅值爆衝（車輛亮部 0.99），ai_geo 有界（0.27）。

**附帶真問題（與 AI 無關，兩路共用下游）**：`_side_crossings(min_persist=3,
eps_down=0.01)` 在 norm 素材鎖死（噪聲底 0.01-0.05，wb 需連續 3 幀<0.01 永不確認
→ present 卡住，S1-L 200 幀只吐 9 個 bw）。需另立任務以 gold 真值重調，
本次 Spike 未動生產碼。

**決策閘結論＝混合策略（條件式採用）**：classical 存活時沿用（免 GPU、行為不變）；
僅當「classical 近零死＋ai_geo 強週期（cons/幅值皆訊號內生門檻）」時切 AI。
S5b 證明不可無條件切換；夜間閘門維持（S8/S9 皆為實驗量測，不代表放行夜間測速）。
Phase 3（`--mask-backend`＋gold 真值驗收）待使用者批准後另開任務。

```bash
ruff check dashcam_player/           # All checks passed
python -m mypy dashcam_player/ --ignore-missing-imports  # Success: 82 files
python -m pytest dashcam_player/tests/ -q  # 346 passed, 2 failed（預存 TestSideCrossings×2）
python scripts/eval_mask_spike.py --all --batch 8   # 重跑（需權重＋torch）
```