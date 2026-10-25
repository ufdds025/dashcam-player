> 🧊 2026-10-26 已凍結，見 [dashcam_player/docs/VISION_SPEED_POSTMORTEM.md](dashcam_player/docs/VISION_SPEED_POSTMORTEM.md)

# 軟體測速與 GPS 特徵比對校正 — 詳細實作計畫書

> **專案**：行車紀錄器影片播放與截圖工具  
> **日期**：2026-08-27  
> **版本**：2.2 — 套用專案檢索修訂（14 項疏漏：SpeedTrack 快取、三解碼器調度、ffmpeg/gps_parser 盤點補全等）  
> **作者**：OpenCode (Muse Spark) + 多 AI 討論彙整（Gemini/Grok/Claude/ChatGPT 綜述）  
> **狀態**：Plan Review v2.2（P0 九項門檻，P0.5 可行性閘門後進 P1a）  
> **前版**：2.1 / 2.0（2026-08-27）/ 1.0（2026-08-26）

> **審查結論（ChatGPT 綜述）**：架構成熟度 85-90% / 演算法可驗證性 70-75%。最大風險非 QThread/QSettings/FFmpeg，而是「4m+6m → 水平投影 → v=10/Δt」缺幾何保證。**本版以 P0 九項為有條件通過門檻**，P0.5 先驗證 vision-only 再進 P1a。

---

## 目錄

1. [背景與動機](#1-背景與動機)
2. [目標與非目標](#2-目標與非目標)
3. [現況架構盤點](#3-現況架構盤點)
4. [需求定義](#4-需求定義)
5. [總體架構設計](#5-總體架構設計)
6. [視覺測速管線 v2.2（Multi-strip + Harmonic + 可測試 Confidence）](#6-視覺測速管線-v22multi-strip--harmonic--可測試-confidence)
7. [GPS 特徵比對與校正 v2.2（品質閘門 + EMA + Outlier）](#7-gps-特徵比對與校正-v22品質閘門--ema--outlier)
8. [時間同步與 VFR 處理 v2.2（PTS 優先序 + Quality）](#8-時間同步與-vfr-處理-v22pts-優先序--quality)
9. [模組與介面設計](#9-模組與介面設計)
10. [資料模型與持久化 v2.2](#10-資料模型與持久化-v22)
11. [執行緒與生命週期 v2.2（Scheduler + 優先序 + 預算）](#11-執行緒與生命週期-v22scheduler--優先序--預算)
12. [UI 設計 v2.2](#12-ui-設計-v22)
13. [兩段式自適應取樣 v2.2（護網語意明確）](#13-兩段式自適應取樣-v22護網語意明確)
14. [錯誤處理與邊界 v2.2](#14-錯誤處理與邊界-v22)
15. [效能與資源 v2.2（預算制 + 驗證指標）](#15-效能與資源-v22預算制--驗證指標)
16. [測試計畫 v2.2（五類合成 + Negative + FHCR）](#16-測試計畫-v22五類合成--negative--fhcr)
17. [實作階段與時程 v2.2（含 P0.5 閘門）](#17-實作階段與時程-v22含-p05-閘門)
18. [風險與緩解](#18-風險與緩解)
19. [文件與規範異動](#19-文件與規範異動)
20. [開放問題決議](#20-開放問題決議)
21. [P0 門檻清單（有條件通過）](#21-p0-門檻清單有條件通過)
22. [附錄](#22-附錄)

---

## 1. 背景與動機

- 台灣《道路交通標誌標線號誌設置規則》第 182 條：快速/高速公路車道虛線 **線段 4m、間距 6m、週期 10m**（市區可能 3m+4m、隧道/施工區無標線）。此 10m 為本計畫唯一物理尺規。
- 現行已可透過 `utils/gps_parser.py:283 GPSParser` 取得 GPS（外部 .gpx/.nmea/.srt → 內嵌 data track/tags → OCR 崁入文字 fallback），但隧道/都會峽谷/無 GPS 影片時速度中斷。
- 多 AI 共識：無 GPU 純 CPU可行，但僅晴天白天直線段、需 ROI 降載與多週期平均。此計畫將其產品化為 **可關閉開關 + GPS 特徵比對自動校正**，並在 v2 補齊幾何/時基/harmonic 三大缺口。

> **重要更正（v2 起）**：不再表述為「利用 10m 週期直接測速」，而是「**利用標線時空週期作為弱幾何尺度，搭配多條帶一致性與週期假設檢驗估算速度**」。

## 2. 目標與非目標

### 目標

| # | 目標 | 驗收（v2.2 修訂） |
|---|------|----------------|
| G1 | 軟體測速可勾選啟閉，預設關閉，零 GPS 影片亦可近似估速 | 關閉時零 CPU/零執行緒；未校正機型純視覺標 `未校正/低信心`，不偽裝可用；停等紅燈無 GPS 時顯示 `--` 而非 0 |
| G2 | 有 GPS/崁入速度字時自動校正與加權融合 | 同段校正後中位誤差優於未校正；融合權重由來源品質分數決定，非固定優先序；FHCR 為第一 KPI |
| G3 | 純 CPU、零重型依賴 | `requirements.txt` 不新增 torch/onnx |
| G4 | 覆蓋 9+7 項盲點，P0 門檻為進 P1a 必要條件 | 見 §21 |

### 非目標

- DL 車道分割、IMU、法律級精度。

## 3. 現況架構盤點

| 模組 | 位置 | 關鍵特性 |
|------|------|----------|
| 解碼 | `core/video_worker.py:39 VideoWorker(QThread)` | `queue.Queue[_Command]` 單消費者、`get_nowait:292`、 `_target_fps:250` 換片重置、`RELEASE_CAP:202` + `_ensure_cap:209` 雙解碼器協議、`try/finally cap.release:228`、禁用 `finished`（改 `play_finished/result_ready`） |
| GPS 提取 | `utils/gps_parser.py:283`, `core/gps_manager.py:55`, `core/managers/gps_facade.py:24` | Facade、`interpolate_gps:926 bisect`、`filter_gps_drift:80`、`_parse_gpx:686` 相對化（異常回 `[],None`）、`has_ocr_gps:340` 三幀取樣 + ROI 自動儲存 |
| 設定 | `models/capture_settings.py:62`, `config/settings.py:18` | 組合式 + `replace()`、`_SUB_FIELD_NAMES:54` 與 `_flat_capture_fields:18` 明確列舉、Windows 需 `_coerce_by_field_type:32`、ROI 命名 `gps_roi/{W}x{H}_{FMT}:137`、debounce 500ms `video_player.py:86` |
| 顯示 | `core/video_player.py:61` | `frame_ready` 15ms timer 節流(`:527`)、`frame_to_qimage:159` 需 `.copy()`、`overlay_gps_text:189 frame.copy()`、`MapView` 需 `LocalContentCanAccessRemoteUrls` 與 `_page_ready` |
| 匯出/批次 | `utils/ffmpeg.py:273 build_export_cmd`, `core/batch_capture.py:30` | `scale` only 無 `drawtext`、`preserve_gps -map 0`、獨立 cap + `try/finally`、OSM `160×160` + `_CACHE_LOCK:31` |

> 已審計發現：全專案 `frame_idx/fps` 線性（VFR 風險）、`GPSPoint.speed:52` 為 knots 且插值器丟棄該欄、GPX 無 `<extensions>`、併發解碼需遵守 `RELEASE_CAP`。

## 4. 需求定義

### 功能需求

- **FR1 開關**：選單/設定各一勾選「啟用軟體測速」，`QSettings capture/speed_enabled bool`，經 `Controller.update_capture_settings(replace(...))` 即時生效；切換時若掃描中則 `stop→restart` 語意明確（不留 orphan worker）。
- **FR2 視覺估速**：固定梯形 ROI（下 1/3、寬 60%）→ **Multi-strip 分解（Y 軸 3-4 條，等分起步，預留透視加權）** → 各條時序 `S_i(t)` 自相關/週期性 → harmonic `D/2,D,2D` 假設 → **strip consensus + margin 拒答** → `v=D/Δt`（D 預設 10.0m 可調 6-12m 為 **Scene hypothesis** 非定值，**內部一律 m/s**）。
- **FR3 GPS 比對**：互相關求 `offset`（預留 `a*t+b` 漂移，P1 僅 `a=1`），分窗 `ratio=median(gps/vision)` 經品質閘門+離群排除後 EMA 更新校正檔，動態品質加權融合。
- **FR4 顯示**：狀態列與疊加顯示 `行車速度`（與「播放速度 0.25x」區分），含信心等級 `HIGH/MEDIUM/LOW` 與 `≈/估計/--` 標記；批次/單張可燒字；地圖著色延至 P4。
- **FR5 匯出**：GPX `<extensions><gpxtpx:TrackPointExtension><gpxtpx:speed m/s>`（宣告 `xmlns:gpxtpx`），`estimated` 段標記；影片 `drawtext` 僅保留 API，UX 延後。

### 非功能需求

- 關閉零開銷；開啟時 `SpeedWorker` 低優先序 + 預算制，不擠壓 `VideoWorker`。
- VFR 正確（PTS 主路徑）、中文路徑（`open_video_capture:62`）、QThread 不靜默。
- 未校正機型純視覺不偽裝可用（G1 修訂）；`false-high-confidence` 為首要風險指標。

## 5. 總體架構設計

```
VideoPlayer ──→ Controller (薄 Orchestrator, 12 signals relay)
   │                ├─ PlaybackManager (節流)
   │                ├─ CaptureManager
   │                ├─ ExportManager
   │                ├─ GPSFacade → GPSManager → GPSParser/OCR/ROI
   │                └─ SpeedManager ──→ SpeedScheduler ──→ SpeedWorker(QThread) ──→ vision_speed 純函式
   │                      │                   │                    │
   │                      │                   │                 open_video_capture (獨立 cap, RELEASE_CAP)
   │                      │                   │                 pts_loader (ffprobe 一次產 pts_list+keyframe_times)
   │                      │                   └─ LowPriority + 預算制 (播放中降載/暫停全速)
   │                      └─ AppSettings (speed_enabled/D/roi/calibration/camera_profile)
   │
   └─ 狀態列/疊加 (frame.copy) ← speed_updated(time,fused,confidence,source)
       MapView（P4 著色）      ← speed_ready(SpeedTrack)
```

- 新增不污染 GPS 鏈；融合在 `SpeedManager`；離線掃描產 `SpeedTrack[]`，播放以 `bisect` 插值。
- **P0.5 閘門**：先做 `vision_speed.py + pts_loader.py + offline_test.py` 離線可視化，不碰 `Controller/QSettings/GPS fusion/UI`（見 §17）。

## 6. 視覺測速管線 v2.2（Multi-strip + Harmonic + 可測試 Confidence）

### 為何改（Gemini/Claude 指正）

全寬水平投影會使梯形 ROI 內遠近不同像素速度的相位互相抵消；P1 承諾「BEV 透視即解決」在無標定下不成立。改為 multi-strip 後每條帶內殘餘透視已可控，P2 再評估真 IPM。

### 新管線

```
Frame(BGR) → Video suitability check
             (亮度/對比/Laplacian sharpness/標線密度/ROI 可見度 → overall_score → SUPPORTED/LIMITED/UNSUITABLE，
              初版以亮度/對比/邊緣強度簡易統計量起步，真實夜間/雨天再調門檻)
  → ROI梯形裁切 → 灰階 → CLAHE(clipLimit=2.0,8x8, 複用 preprocess_clahe_otsu:168)
  → Otsu/自適應二值 → 形態閉運算
  → Multi-strip 分解（Y 軸 3-4 條，見 §6.1）
      各 strip: 水平投影 → 峰值候選
  → 各 strip 時序 S_i(t) → 自相關/週期性檢測
  → Harmonic 假設 D/2,D,2D 候選（見 §6.2 決策樹，含 margin 拒答）
  → Strip consensus（見 §6.1）
  → 真實 PTS 求 Δt → v=D/Δt（內部 m/s）
  → 滑動中值 + 變道抑制（峰距突變>40% 或極性破壞 → 該窗低信心，閾值作 debug 可調參數）
  → 不可測區：v<V_MIN(≈5 km/h) 或 strip 長期不一致 → NO_VISION_SPEED（不硬猜 0）
  → Confidence（見 §6.3）
```

- **零速語意**：不以 `frame diff 低 → 0 km/h` 判定（易受震動/旁車干擾），P1 僅輸出 `NO_VISION_SPEED`，有 GPS 則顯示 GPS，無則 `速度: --`。需區分 `NO_VISION_SPEED（不可測/盲區）` vs `NO_ESTIMATE（外推逾時 §7）`。
- **CLAHE 噪爆**：白像素占比 `>40%` 或 `<2%` **不硬拒幀**，改計入 `sparsity_quality` 子分（見 §6.3），僅 P1a 驗證有效後才考慮硬門檻。
- **輸出**：`VisionPoint(time, raw, confidence, period_hypothesis, strip_votes, reject_reason)` 序列，低信心標記供融合器決策。
- **對照基線（Claude 建議）**：P1a 同步以「細掃描線 tripwire（近/中/遠三條穿越計時）」作 baseline 對照，與 multi-strip+自相關版跑同一合成四類測試，以 MAE/false-high-confidence 決定是否簡化。

#### 6.1 Multi-strip 參數（Grok 指正補齊）

| 參數 | P1a 起步值 | 備註 |
|------|-----------|------|
| strip 數量 | 4 條，等分 | 預留透視加權（近處權重略高），P1a 先等分，實驗後定 |
| 高度 | ROI 高度 / 4 | 示意圖：`S1(近) S2 S3 S4(遠)` 自下而上 |
| 共識門檻 | ≥3/4 週期一致 | 一致定義：兩週期相對誤差 ≤10%（可調 ±5/10/15% 作參數實驗） |
| 降級接受 | 僅中央 1-2 條有效時可降級接受 | 但強制降為 LOW 並大幅扣 confidence，僅供 ESTIMATED 外推參考 |
| 權重實驗 | `equal` vs `perspective-weighted` | P1a 以合成 B/C 類驗證遠近相位差下的 FHCR 決定 |

偽碼：

```python
periods = [estimate_period(S_i) for S_i in strips]  # 各條自相關峰
candidates = cluster(periods, tol=0.10)  # 相對誤差 10% 內聚類
consensus_group = max(candidates, key=len)
if len(consensus_group) >= 3:
    period = median(consensus_group)
    consensus = len(consensus_group)/4
else:
    if central_strips_valid: period, consensus = median(central), 0.5*base
    else: return NO_VISION_SPEED
```

#### 6.2 Harmonic 決策（含拒答）

```python
scores = {h: score_period(h) for h in ["D/2","D","2D"]}  # 0-1
best, second = top2(scores)
if best.score < MIN_SCORE:              # 例 0.60，P1a 調校
    return NO_VISION_SPEED
if best.score - second.score < MIN_MARGIN:  # 例 0.10
    return NO_VISION_SPEED  # 三者相近→證據不足
if consensus < MIN_CONSENSUS:           # 例 3/4
    return NO_VISION_SPEED
return best.hypothesis  # 才接受
```

> 取代「三選一必有答案」；實際閾值由 P1a 合成+真實統計定，不在文件硬編。

#### 6.3 Confidence 公式（可測試化，子分獨立性需檢驗）

```
C = 0.30*marking_strength + 0.25*strip_consensus + 0.20*periodicity
  + 0.15*speed_plausibility + 0.10*temporal_stability  ∈ [0,1]
HIGH ≥0.75, MEDIUM 0.45-0.75, LOW <0.45
含 sparsity_quality 併入 marking_strength（非硬門檻）
```

**注意**：`periodicity` 與 `strip_consensus` 高度相關（相關係數可能 >0.7），P1a 以四類合成算 5×5 相關矩陣，若過高則合併或降權，避免重複計入。

## 7. GPS 特徵比對與校正 v2.2（品質閘門 + EMA + Outlier）

### 真值來源（動態品質融合，非固定優先序）

各來源皆有 `quality score`（GPS: 衛星/HDOP/跳變；OCR: 數字信心/時序一致/合理範圍；Vision: 週期一致/strip 共識/峰強度），**加權融合**而非 `OCR>GPS>Vision` 硬排序。

- **來源相依退化**：HDOP/衛星數僅 NMEA GGA 有（`_parse_nmea_line:624` 現丟棄，需擴充 `GPSPoint` 欄位，見 §9）；GPX/SRT/embedded-tags 天生無此欄 → quality 改以「點密度 + 跳變偵測 + 取樣率」評分。
- **OCR 可用性契約（P1b）**：Tesseract 為 CLI 子程序（`requirements.txt` 無 pytesseract、EXE 未封裝、CI apt 未安裝）→ 實作前必經 `shutil.which("tesseract")` 探測；不可用時 OCR quality 歸零、狀態列提示「OCR 速度來源不可用」，靜默降級為 GPS/Vision 融合，不得報錯中斷。

### 比對步驟

1. **重取樣** 1Hz 等距。
2. **時基對齊** `t_gps_aligned = a*t_gps + b`（P1 僅估 `b=offset` 互相關峰值，長片>10min 或 residual 大時預留 `a≠1` drift 模型；**永遠將 GPS/OCR 映射至 video canonical timeline，不改 vision**）。
3. **品質閘門**：`strip_consensus + periodicity + temporal_stability` 達標才可參與 `ratio` 計算；且要求 **minimum valid samples**（例窗口內 `N≥5` vision+GPS 點、GPS quality≥thr、vision quality≥thr、correlation≥thr、speed_range≥thr，具體由 P1a 定）。
4. **分窗校正** 5-10s 窗 `ratio=median(gps/vision)`，`vision_corrected = vision * ratio`；名稱為 `empirical calibration factor`。
5. **Outlier 排除 → EMA**：`candidate` 先 sanity check（與 `old` 偏差 >門檻如 0.4 即使 confidence 達標亦丟棄，防匝道雙線等極端污染）→ `new = α*old + (1-α)*candidate (α=0.8)`。
6. **加權融合** `fused = w*corrected + (1-w)*gps`，`w` 隨 vision confidence 與窗相關係數動態。
7. **持久化**：`CalibrationProfile {camera_profile, ratio, offset, a, sample_count, ratio_mad/std, speed_min/max, confidence, source_coverage, updated_at}`，鍵 `speed_calibration/{profile}/{W}x{H}_{FMT}`；僅 `confidence>threshold` 才保存；市區/高速/彎道可分 profile。

### 校正描述

> Vision 提供相對尺度，GPS 提供實際尺度校正（見 §5 架構圖 `Weak prior → Relative → Empirical factor → Fused`）。

### Estimated 外推

```
0-5s   estimated 正常（confidence 指數衰減）
5-15s  estimated 急衰減
>15s   NO_ESTIMATE（不無限外推）
confidence(t)=confidence0*exp(-λt)（λ 可調，例 5s→0.6 10s→0.35）
GPX <extensions><gpxtpx:TrackPointExtension> 同步標記，UI 顯示「估計」而非僅 ~
```

### 產品能力分級（明確契約）

```
Level 0: GPS 無 + Vision 無 → 速度: --
Level 1: Vision 有 + GPS 無 → 52 km/h 未校正 低信心
Level 2: Vision + GPS       → 52 km/h 已校正 高信心
（Level 1 不得外觀等同 Level 2）
```

### D 定位

`distance_per_cycle` 為 **Scene hypothesis**（7/8/9/10/11/12m）而非定值，結合 `camera_profile + road_profile` 決定；市區自動提示是否需改 D。

## 8. 時間同步與 VFR 處理 v2.2（PTS 優先序 + Quality）

- **PTS 來源優先序**：`pkt_pts_time` → `best_effort_timestamp_time` → 持續時長累加 → `fps 線性 fallback`。
- **Quality 標記**：`EXACT(PTS) / DEGRADED(best_effort) / UNSAFE(linear)`，UNSAFE 時 vision 最高僅 MEDIUM/LOW，不得 HIGH。
- **探測**：`ffprobe -show_frames -select_streams v:0 -show_entries frame=pkt_pts_time,best_effort_timestamp_time,flags -of csv=p=0` 一次產 `pts_list + keyframe_times`（合併 `VideoWorker._start_keyframe_loader:88`）；timeout 採 `max(固定最小, duration-based budget)`，逾時標 `TIMESTAMP_UNAVAILABLE` 而非無條件線性（駁回 Gemini 3s 硬切）。
- 視覺 Δt 與 GPS `point.time` 皆對齊 video 相對秒（`canonical = video`）。

## 9. 模組與介面設計

### 新增檔案

| 檔案 | 職責 | 關鍵介面 |
|------|------|----------|
| `dashcam_player/models/speed.py` | 資料模型 | `SpeedPoint(time,fused,raw,corrected,confidence,source,period_hypothesis,strip_votes,reject_reason)`, `SpeedSettings(enabled,D,smoothing_window)`, `CalibrationProfile(...,ratio_mad/std,source_coverage)`, `SpeedTrack(points,profile,coverage,status: OK/PARTIAL)` |
| `dashcam_player/utils/vision_speed.py` | 純函式演算法（multi-strip/harmonic/confidence/suitability） | `detect_markings_multi_strip(roi)->peaks`, `estimate_vision_speed(peaks,pts,D)->VisionPoint[]`, `video_suitability(frame)->SUITABILITY`（無 Qt）|
| `dashcam_player/utils/pts_loader.py` | PTS+keyframe 探測 | `load_pts_and_keyframes(path)->(pts_list,keyframe_times,source,quality)` 共用 |
| `dashcam_player/utils/speed_calibration.py` | 校正檔 | `validate_and_ema_update(old,candidate)->new`（含 MAD/STD 與 outlier 門檻） |
| `dashcam_player/utils/speed_cache.py` | SpeedTrack 磁碟快取（P2a） | `load(path_hash,size,mtime)->SpeedTrack\|None` / `save(...)`，鍵 `{path_hash}-{size}-{mtime}.speedtrack.json` 於 temp 目錄 |
| `tools/vision_feasibility.py`（repo root，不納入套件/lint 範圍） | P0.5 離線驗證腳本 | CLI：影片路徑 → CSV + Debug Visualization 圖輸出至 `tempfile.gettempdir()/dashcam_speed_debug/` |
| `dashcam_player/core/speed_worker.py` | 背景掃描 | `QThread`, `result_ready(str,SpeedTrack)`, `progress(int,int)`, `error_occurred(str)`, `stop()`，`try/finally`，`get_nowait`，`LowPriority` |
| `dashcam_player/core/speed_scheduler.py` | 調度 | `budget` 每 100ms 最多 X ms，依 `playback active? → low priority / paused → full scan` + `CPU pressure`（<60% normal/60-80% reduce/80-90% aggressive/>90% pause），批次截圖進行中視同 playback active |
| `dashcam_player/core/managers/speed_manager.py` | Facade | `speed_updated(float time, float fused_mps, float confidence, str level, str source)`、`speed_ready(str,SpeedTrack)`，單一 `active_worker` 保證、partial 保留、過期丟棄 |

### 修改檔案

| 檔案 | 改動 |
|------|------|
| `models/capture_settings.py` | 新增 `SpeedSettings`，同步改 7 處明確列舉（見 `AGENTS.md` 陷阱） |
| `config/settings.py` | `load/save_speed_settings()`, `load/save_speed_calibration(profile,W,H,FMT)`（`speed_calibration/` + `speed_roi/` 雙命名空間），`settings_version` 遷移，`_coerce_by_field_type`，缺鍵回 `SpeedSettings()` 預設 |
| `core/controller.py` | 持有 `SpeedManager/Scheduler`，`open_video/shutdown` 生命週期 |
| `core/video_worker.py` | 復用 `pts_loader` 結果，避免雙探測 |
| `utils/cv_utils.py` | 新增 `overlay_speed_text(frame, km_h, confidence)->frame`（`frame.copy()`），`debug_visualization` 供 P0.5 |
| `utils/ffmpeg.py` | ① `get_keyframe_positions:228` 擴充為 pts_loader 後端（全幀 PTS + keyframe 雙產物、duration-based timeout 取代固定 30s）；② `build_export_cmd` 新增 `burn_speed` 分支：`use_copy=False`，且 `-vf` 必須**鏈結**既有 scale（`scale=W:-2,drawtext=...`，不可第二個 `-vf`），含 `win32_short_path`，預設關閉 |
| `utils/gps_parser.py` | ① `GPSPoint` 新增 optional `hdop/satellites` 欄位 + `_parse_nmea_line` 解析 GGA 對應欄（現丟棄），供 §7 GPS quality；② `to_gpx` 宣告 `xmlns:gpxtpx` 並寫 `<gpxtpx:TrackPointExtension><gpxtpx:speed m/s>`，vision-only 繞過 `has_gps_data:888`；③ 新欄位需通過既有 31 項 GPS 測試回歸（to_dict/from_dict 相容） |

## 10. 資料模型與持久化 v2.2

```python
@dataclass
class SpeedPoint:
    time: float          # canonical video 相對秒
    raw_vision: float | None      # m/s
    corrected: float | None       # m/s
    fused: float | None           # m/s（顯示/匯出前轉 km/h）
    confidence: float
    source: str          # vision|gps|ocr|fused|estimated|none
    period_hypothesis: str = "D"  # D/2/D/2D
    strip_votes: str = ""         # 例 "3/4"
    reject_reason: str = ""       # 例 "harmonic_margin"

@dataclass
class CalibrationProfile:
    camera_profile: str = "default"
    ratio: float = 1.0
    offset: float = 0.0
    a: float = 1.0
    sample_count: int = 0
    ratio_mad: float = 0.0
    ratio_std: float = 0.0
    source_coverage: float = 0.0
    speed_min: float = 0.0
    speed_max: float = 0.0
    confidence: float = 0.0
    updated_at: float = 0.0

# QSettings
capture/speed_enabled, capture/distance_per_cycle, capture/smoothing_window
capture/settings_version  # 遷移用
speed_calibration/{profile}/{W}x{H}_{FMT}/ratio, /offset, /a, /ratio_mad, /ratio_std, /sample_count, /speed_min, /speed_max, /confidence, /updated_at
speed_roi/{W}x{H}_{FMT}/x,y,w,h,corner  # 另設，不與 gps_roi 耦合
```

- 內部一律 `m/s`，UI `km/h = m/s*3.6`，GPX `m/s`（Garmin 規範）。

### SpeedTrack 磁碟快取（v2.2 新增）

- **動機**：`open_video()` 每次都觸發全片掃描（5 分鐘片 20-50s+），反覆開同一支片重複付出成本。
- **鍵**：`{path_hash}-{size}-{mtime}.speedtrack.json`（temp 目錄），任一失效即重掃。
- **內容**：SpeedTrack 序列化 + `settings_version` + 掃描參數摘要（D/strip 設定變更亦視為失效）。
- **時機**：P2a 實作；P0.5/P1 階段不快取（演算法迭代中，避免舊結果混淆驗證）。

## 11. 執行緒與生命週期 v2.2（Scheduler + 優先序 + 預算）

- 命名禁用 `finished`；`stop flag → disconnect → wait(5-15s) → terminate`。
- **單一 active worker**：`SpeedManager.active_worker: Optional`，切片時 `cancel A → wait → discard → start B`，不並行；開關切換若掃描中則明確 `stop→restart`，不留 orphan。
- **優先序與預算**：`VideoWorker` normal，`SpeedWorker` `QThread.LowPriority` + 細粒度讓權（**每處理 1 幀或 20-30ms 即檢查**，非累積 1s 才 `msleep`；優先確保 `numpy` 向量化而非僅靠 sleep，見 Claude 指正），`SpeedScheduler` 依 `playback active?` 與 `CPU pressure` 自適應降採樣。
- **預算**：`SpeedScheduler` 依 CPU 分級（<60% normal / 60-80% reduce / 80-90% aggressive / >90% pause），`VideoWorker` 播放優先。
- **Partial**：取消時保留 `SpeedTrack(status=PARTIAL, coverage=done/total)`，不全丟。
- **併發實測**：P1 以 1080p H264/H265、4K H265 實測雙 cap 是否衝突，再決定是否硬綁 `CAP_FFMPEG`（不預設必然 race）；失敗時 fail-safe 標 `speed unavailable` 不崩潰。
- **解碼成本驗證**：`cap.read()` 即使跳過仍需完整解碼，P1a 需驗證 `decode-only cost` 是否隨 skip-rate 線性下降（Grok/Claude 遺漏點）。
- **三解碼器協調（v2.2 新增）**：播放（`VideoWorker.cap`）+ 批次截圖（`BatchCaptureWorker` 自開 cap）+ 測速掃描可能並存 → `CaptureManager.start_batch_capture` 觸發時 SpeedScheduler 視同 playback active（降載或暫緩），禁止三解碼器全速並行。
- **pts_loader 與 seek 相容（v2.2 新增）**：P1c 僅「共用探測結果」；`VideoWorker._nearest_keyframe_idx` 的 `target/fps` 對映與 seek 邏輯**不動**（VFR 下改吃 PTS 會波及播放核心），如需切換另以 flag 評估。

## 12. UI 設計 v2.2

### 選單

```
速度 (新增)
 ├─ [ ] 啟用軟體測速
 ├─ 速度設定...（D/平滑/燒錄開關 + camera_profile 選擇 + suitability 閾值，含未校正提示）
 └─ 狀態：未校正/低信心時明確提示
```

### 顯示

- **狀態列**：`行車速度: 52 km/h  信心: 中`（或 `52 km/h ≈ 估計`），長隧道後 `NO_ESTIMATE` 顯示 `--`；Level 1/2 外觀明確區分。
- **疊加**：`overlay_speed_text` 在原圖 copy 上繪製，避開 `overlay_osm_thumbnail:214` 區與縮放裁切；P0.5 需 **Debug Visualization**（原圖 + S1..S4 框 + Period/Hypothesis/Consensus/Confidence/Speed）。
- **地圖著色/影片燒字**：延至 P4/P5，P3 僅保留 API 不做完整 UX；燒字預設關閉、字型打包開源字型（Noto Sans TC，OFL 授權需隨附文件）避免跨平台 `fontfile` 痛點。
- **OCR 來源提示（v2.2）**：Tesseract 不可用時狀態列顯示「OCR 速度來源不可用」，不影響 GPS/Vision 融合。
- **免責**：設定頁常駐「估算值僅供參考，非執法依據」；未校正機型純視覺標 `未校正`。

## 13. 兩段式自適應取樣 v2.2（護網語意明確）

```
Stage1 粗探：前 8 秒全幀率掃 ROI → 估 v_p95（multi-strip consensus 後的中位）
Stage2 主掃：目標 6-8 samples/cycle（P0 最低 6，目標 8，P1 先以 6 保守起步）
            rate = clamp(ceil(v_p95/10*target*1.2), 6, 30) fps
            例 110km/h → 15-18fps 覆蓋全速域
            samples_per_period = observed_period / sample_interval ≥ N_min（例 6）才接受，
            不足 → 該窗 invalid（取代原「週期 <2.5×取樣間隔」模糊表述）
護網：bad window → 擴展 ±1 cycle → 30fps 局部重掃一次，仍異常 → NO_VISION_SPEED（不無限重掃成 CPU 黑洞）
實作：循序 cap.read() 跳過不處理幀；進度語意為「主掃進度」，重掃為第二階段進度條，不超 100%
```

- 低功耗（Pi）：強制 540p ROI + 6-8fps + 關 CLAHE，手動開關優先於自動降載。
- **短片邊界（v2.2）**：影片 <8s 時 Stage1 clamp 至可用時長；<2s 或樣本不足一個完整週期 → 直接 `LIMITED/UNSUITABLE`，不輸出速度。
- **V_MIN 單位（v2.2）**：文件值 ≈5 km/h = 內部 1.39 m/s；實作常數以 m/s 定義並註明換算。

## 14. 錯誤處理與邊界 v2.2

| 情境 | 處理（v2.2 增量） |
|------|----------------|
| 無標線/隧道/施工 | 低信心 `estimated` 指數衰減後 `NO_ESTIMATE`，GPX 標記 |
| 多組平行標線（對向/路肩/匝道） | Multi-strip consensus 自然抑制；必要時僅中央 strip |
| 前車遮擋 vs 標線缺失 | 前者暫時低信心（車移開恢復），後者路段級低信心（不長距離外推） |
| 變道 | 峰距突變>40% 或極性破壞 → 該窗低信心，不入校正（閾值 debug 可調） |
| 未校正機型 | 純視覺標 `未校正/低信心`（G1 修訂） |
| 廣角畸變 | 文件明示，預留單應校正；ratio 僅 empirical；P1a 以合成 C 類驗收 |
| 低速/停等 | `V<V_MIN` → `NO_VISION_SPEED`，不硬猜 0（需全局運動+標線穩定雙條件才可 P2 再做） |
| 同步 | 互相關 + 漂移預留、品質閘門後才校正 |
| 解碼失敗 | 連續失敗 >N 幀終止並 `error_occurred`，OOM 則截斷 `SpeedTrack` |

## 15. 效能與資源 v2.2（預算制 + 驗證指標）

- ROI `640×120` 單幀 2-6ms 為 **未驗證假設**，P1 以 benchmark 定基線，指標：`decode FPS / analysis FPS / CPU% / RAM / dropped frames / playback frame latency / UI response`；**驗證 `decode-only cost` 是否隨 skip-rate 線性下降**。
- **SpeedTrack 記憶體（v2.2 修正）**：週期級取樣 2h ≈ 21.6k 點 × ~120B ≈ 2.6MB，無需截斷；改為 benchmark 後確認即可（避免過度設計）。
- 背景掃描有預算制（每 100ms 最多 X ms），播放優先；`decode` 本身同步阻塞無法細切，需實測。

## 16. 測試計畫 v2.2（五類合成 + Negative + FHCR）

**合成五類**：A 理想 / B 透視（pitch/yaw）/ C 畸變（fisheye）/ D 雜訊（陰影/遮擋/缺線/壓縮）/ **E 對抗**（護欄/對向/雙標線/匝道反光，專測 false period），各測 `0/5/10/30/60/100/120/150 km/h`。另 **Negative**（隧道/施工/停車場/夜間無標線）期待 `NO_VISION_SPEED` 拒答。

**指標**：`MAE / MedianAE / P95AE / Bias / Coverage / low-confidence rate / false-high-confidence rate`（FHCR 第一 KPI），定義 `FHCR = 高信心且誤差超門檻 / 全部高信心`。

**P1a 驗收（不硬定數字，先 benchmark 後定）**：

```
A 理想通過、B 透視通過、C/D 可控、E 不亂報 HIGH、Negative 能拒答、
VFR 不暴衝、5-10 支真實影片可重現、FHCR 在門檻內
```

**合成基礎設施（v2.2）**：CI 在 **ubuntu-latest** headless（非 Windows），`cv2.VideoWriter` 合成影片建議 `XVID/.avi`（mp4v 相容性風險），writer 開啟失敗即 `pytest.skip`；P1b OCR 測試一律 mock `_run_tesseract`（CI apt 未裝 tesseract，見 §7 可用性契約）。

**整合/手動**：沿用 `isolated_qsettings:13`、中文路徑、過期丟棄；5 支真實 dashcam（含隧道/雨夜/變道）對照 GPS 真值。手動真實影片測試僅能本機 Windows 執行（CI 無實車資料與 GUI）。

**薄掃描線對照**：P1a 以 tripwire 三線穿越計時作 baseline，與 multi-strip+自相關版同跑合成，擇優。

## 17. 實作階段與時程 v2.2（含 P0.5 閘門）

| 階段 | 內容 | 產出 | 估時 |
|------|------|------|------|
| **P0.5** | **Vision Feasibility Gate**（僅 `vision_speed.py` + `pts_loader` + `tools/vision_feasibility.py` + Debug Visualization，不碰 Manager/QSettings/UI） | `time/raw_period/hypothesis/raw_speed/confidence/strip_consensus/suitability/reject_reason` CSV + 可視化圖 | 1d |
| P1a | 多條帶管線 + harmonic + confidence + suitability（含 baseline 對照） | 離線腳本白天高速多條帶共識可用（不含 GPS），FHCR 達標 | 1.5d |
| P1b | 畫面速度 OCR（`speed_roi/` 另設）**＋ `shutil.which("tesseract")` 可用性閘門** | `ocr_speeds` 品質分數；不可用時靜默降級 + UI 提示 | 0.5d |
| P1c | `pts_loader` 合併探測（csv + 優先序 + source/quality 標記） | 單次 ffprobe 雙產物 | 0.5d |
| P1d | 變道/平行標線/遮擋區分抑制 | 突變窗不污染校正 | 0.5d |
| P1e | 兩段式 + Scheduler + Worker（低優先序/預算/partial/細粒度讓權） | 合成五類 + 真實雙軌驗證 | 1d |
| P2a | Manager/Facade + 設定持久化（含 version/EMA/profile/MAD/STD）**＋ SpeedTrack 磁碟快取** | 開關/切片/過期語意正確；重開同片免重掃 | 1d |
| P2b | GPS 融合（互相關+漂移預留+品質加權+衰減外推 + outlier） | 中位誤差與 FHCR 報告 | 1d |
| P3a | UI（選單/設定/狀態列/疊加，未校正提示，`≈` 標記，`--`） | 畫面與批次燒字（單張） | 1d |
| P3b | GPX 擴充（`gpxtpx` 命名空間 + `m/s` + `TrackPointExtension` wrapper） | 匯出通過 Garmin/QGIS 驗證 | 0.5d |
| P3c | 影片 `drawtext` API 保留（UX 延後）；**`-vf` 與 scale 鏈結** | 僅 API，不做字型選擇 UI | 0.25d |
| P4 | 文件/測試 CI 全綠（ruff/mypy/pytest） | 本文件定版 | 0.5d |

> **開工策略**：**先 P0.5/P1a 實證 vision-only 成立才進 P1b**，否則回爐演算法；不做 v3 大改（ChatGPT 警示過度設計）。

## 18. 風險與緩解

| 風險 | 緩解（v2.2） |
|------|-----------|
| 透視/魚眼偏差 | Multi-strip + 明示 + 預留單應；ratio 僅 empirical；P1a 以 C 類驗收 |
| VFR | PTS 主路徑 + quality 標記 + duration-based timeout |
| 併發卡頓 | LowPriority + 細粒度預算 + 實測後定 CAP_FFMPEG；驗證 decode-only 線性 |
| 市區 D 不準 | 可調 D + ratio 補償 + suitability 分段 + Scene hypothesis |
| 夜間/雨天全盲 | 明確 `UNSUITABLE/NO_VISION_SPEED`，不偽造；Negative 測試覆蓋 |
| Harmonic 錯週期 | D/2,D,2D + margin 拒答 + consensus |
| 校正污染 | 品質閘門 + MAD/STD + outlier + EMA + camera_profile |
| QSettings/qThread | `_coerce` + `isolated_qsettings` + `result_ready` + `try/finally` + `get_nowait` |
| 過度設計 | P0.5 閘門後才擴功能，FHCR 驅動決策 |

## 19. 文件與規範異動

- `AGENTS.md`：`numpy` 允許新增 `vision_speed.py`/`pts_loader.py`/`speed_worker.py`/`speed_scheduler.py`/`speed_calibration.py`。
- `pyproject.toml`：為新增模組加 `disable_error_code`（如需）。
- `requirements.txt`：不新增。

## 20. 開放問題決議（v2.2 定案）

1. **OCR ROI**：另設 `speed_roi/`（與 `gps_roi/` 解耦）。  
2. **影片燒字**：P3c 僅保留 API，UX 延後，預設關閉。  
3. **地圖著色**：延至 P4。

## 21. P0 門檻清單（有條件通過）

> 勾選後依「P0.5 實證 → P1a → P1b/c 融合」順序開工；未勾選不得進 P1a。

- [x] **P0-1 幾何模型改述 + multi-strip + suitability** — 已改為弱幾何尺度，§6 明定 4 條等分/共識 3/4/相對誤差 10%/降級邏輯 + `SUPPORTED/LIMITED/UNSUITABLE` 六維度
- [x] **P0-2 時間軸契約 `canonical=video` + `t=a*t+b`（P1 `a=1`）+ source/quality 標記** — §8 定 `pkt_pts_time→best_effort→linear` + `EXACT/DEGRADED/UNSAFE`
- [x] **P0-3 Harmonic D/2,D,2D + consensus + margin 拒答** — §6.2 明定 `best<MIN_SCORE / margin<MIN_MARGIN / consensus<MIN` 即 `NO_VISION_SPEED`
- [x] **P0-4 Confidence 量化公式 + HIGH/MEDIUM/LOW 閾值 + 子分相關檢驗** — §6.3 定 `0.30/0.25/0.20/0.15/0.10` + 相關係數>0.7 合併
- [x] **P0-5 CalibrationProfile + EMA(α=0.8) + camera_profile + MAD/STD/coverage + outlier 門檻** — §7/§10 定 `ratio_mad/std/source_coverage` + 品質閘門
- [x] **P0-6 Estimated 指數衰減 + `NO_ESTIMATE` 上限（5s/15s）+ 產品分級 Level0-2** — §7 定 `exp(-λt)` + 三級能力契約
- [x] **P0-7 內部 `m/s` 統一 + GPX `m/s` + UI `km/h` + `<gpxtpx:TrackPointExtension>` 正確結構** — §10 定 `xmlns:gpxtpx` + wrapper
- [x] **P0-8 SpeedTrack 磁碟快取（path_hash+size+mtime 失效鍵，P2a 實作）** — §10 定義；避免每次開片全片重掃
- [x] **P0-9 三解碼器調度契約（播放+批次+測速）** — §11 定義批次視同 playback active 降載/暫緩；pts_loader 不波及 seek 核心

> **狀態**：P0 七項已在文件層面完備，進入 **P0.5 Vision Feasibility Gate** 實證階段。

## 22. 附錄

### 週期法試算（重算）

108km/h=30m/s，10m/週期=0.33s。目標 8 samples/cycle → 需 24fps 理論；P1 以 6 samples 保守 → 18fps，兩段式自適應可在 15-18fps 覆蓋全速域，餘裕由護網重掃補。P0.5 需驗證此假設是否隨透視/畸變仍成立。

### 參考

- 台灣《道路交通標誌標線號誌設置規則》第 182 條（白虛線 4m+6m）。
- 多 AI 討論：Gemini/DeepSeek/Grok/OpenRouter/NVIDIA NIM + ChatGPT 綜述。
- 既有 GPS：`docs/PROJECT_GPS_PLAN.md` v1.2；單目測速文獻（ISPRS Annals 2020, MAPE 20.9%）。

---

> 本文件為 v2.2 已修訂版，P0 九項已勾選，下一階段為 P0.5 離線實證。
