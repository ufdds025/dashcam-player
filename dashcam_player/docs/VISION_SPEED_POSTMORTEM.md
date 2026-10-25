# 視覺測速 Postmortem（凍結決策文檔）

> 凍結日期：2026-10-26  
> 對應派工單：`派工單_視覺測速凍結.md`（WO-2026-10-26）  
> 歸檔維修紀錄：[`dashcam_player/docs/archive/MAINTENANCE_LOG.md`](dashcam_player/docs/archive/MAINTENANCE_LOG.md)

---

## 1. 目標與做法時間線（每輪一行，引用 MAINTENANCE_LOG 節名）

| 日期 | 節名（MAINTENANCE_LOG） | 核心動作 |
|------|------------------------|----------|
| 2026-08-28 | 完整 Code Review 與 Bug Fix | 基礎架構修正：GPS OCR ROI 擴寬、PTS AVI fallback、SpeedManager ROI 快取、VideoWorker 佇列安全、GPS 背景載入去重、視覺測速滿寬 ROI 清理、測試斷言修正、Controller shutdown 時序、型別/風格修正 |
| 2026-08-29 | GPS OCR 辨識率 + 視覺測速辨識率改善 | OCR 寬度限制、字元混淆清洗、整數部分眾數修正、AVI FPS 修復、預設 ROI 避開引擎蓋、光流法回退 |
| 2026-08-29 | 擷取檔名可溯性 / 防重複修復 | 統一檔名格式含來源 stem + 幀號、批次跳過既有檔 |
| 2026-08-30 | 標線測速設計重構 + GPS 懶載入 | 邊緣穿越偵測器、距離常數鎖定 10m、校準移除 GPS 依賴、GPS OCR 懶載入 |
| 2026-09-01 | CRIT 4 項修復 + 破圖優化 + .md 歸檔 | speed_calibration NameError、vision_speed 存根、PTS cache 鎖、find_last_frame 熔斷、SSOT probe、md 歸檔 |
| 2026-09-01 | 自動標示全 5 影片 + 人工 2 點工具 | detect_dash_bands、resolution-aware 預設框、2 點標示 schema |
| 2026-09-01 | 2 點重做（垂直取點）+ 有效性檢視 | _snap_dash_band、一鍵垂直吸附、發現水平端點無效 |
| 2026-09-01 | 座標映射 bug 根因（窗 vs 影像座標） | 修正為純影像座標、新增 click_debug log |
| 2026-09-01 | 重做為「單點穿越標記」（Option A） | 與 auto 同參考平面、捨棄 2 點 |
| 2026-09-04 | Phase A 車體上緣驗證 + hood_detector | 中央欄 V 曲線階躍、CONSERVATIVE_FALLBACK_Y=0.64 |
| 2026-09-04 | D 槽 10 支驗證 + 緩坡補強（雙窗） | 9/10 檢出、091049 緩坡→寬窗補強 |
| 2026-09-04 | Phase B 重檢 + Phase C 三處底邊下修 | hood_detector 5 項修正、三處 ROI 底邊 clamp to hood_top−0.02 |
| 2026-09-04 | 標線偵測三機制（夜間閘門+去趨勢切邊+多帶） | assess_lighting、detrend reflect、4 strips merge |
| 2026-09-04 | 去趨勢卷積邊界 bug（reflect）+ 三區段重驗 | 邊界假斜坡根因、三區段驗證 |
| 2026-09-05 | 人工標示閃爍對濾波 + 刪除軌跡 | _remove_flicker_pairs、removed_annotations、audit |
| 2026-09-05 | 自動標記左右同發修正（絕對門檻） | _side_crossings 絕對門檻、eps_up/eps_down |
| 2026-09-05 | 穿越點自動判側 + blink-off wb（浮點） | 點擊 x 自動判側、浮點比較 dwell |
| 2026-09-05 | audit 崩潰修正 + dwell 門檻 2→3 | 排序鍵修正、刪除軌跡分析 |
| 2026-09-06 | -R 鏡頭門檻震盪疑似案例 | 記錄疑似、未下結論 |
| 2026-09-04/06 | 低幀 pacing 修正（deadline 排程） | _compute_frame_interval、deadline、pace_reset |
| 2026-09-04 | 三輪優化：PTS 錨點重置 + GPS speed O(1) | 移除雙重重置、_has_speed_data flag |
| 2026-09-06 | 車道事件掃描 + Task C 指標 | lane_events、eval_cross、scan_lane_events |
| 2026-09-06 | 人工標示續標資料遺失修復 | _split_manual_auto、box_changed flag |
| 2026-09-06 | 人工標示互動優化（綠框+自動存） | active_side overlay、_commit_pending |
| 2026-09-08 | UFLDv2 Spike 實測（10 段 bronze） | classical vs ai-conf vs ai-geo、混合策略決策 |

---

## 2. 成績表（§0.1）

| 指標 | 現況 | 來源 |
|------|------|------|
| Task C 幀級驗收（目標 ≥90% 誤差 ≤1 幀） | 前鏡頭 4 支 **R≈0.00–0.08**；只有後鏡頭 091548-R 達 0.91 | 09-06 車道事件掃描節 |
| 速度誤差（reflect 修正後） | 180655 56.8 vs GPS 45.5（+25%）、092148 39 vs 70.5（−45%）、180755 63.5 vs 39.7（+60%，**誤報**）、施工區 None→44（**誤報**） | 09-04 去趨勢卷積節 |
| 一致性能否預測正確性 | **不能**（合併覆寫門檻在 091548 / 180655 方向相反） | 同上 |
| 夜間 / 正午 / 變道 / 遮擋 | 夜間需閘門擋掉；正午 0 脈衝；變道混疊；遮擋爆衝 | 09-04、09-08 節 |
| 測試 | 346 passed、**2 failed 預存**（`TestSideCrossings`）＋ `test_utils` GPS 格式 1 項列在 lastfailed | 09-08 節、`.pytest_cache` |

---

## 3. 結構性原因（§0.2）

1. **驗收指標跟目標錯位**。速度只用「相鄰穿越的間隔」，固定的參考偏移（前鏡頭 bias −1.7 幀，p10–p90 僅 0.8 幀）在間隔裡會**互相抵消**，對速度毫無影響。但 Task C 量的是絕對幀號，所以花了大量工夫在不影響速度的偏移上；真正造成速度錯的是**漏一個 / 多一個脈衝**（週期直接 ×2 或 ×0.5）。

2. **週期長度不可由畫面時序觀測**。`v = L / T`，程式把 `L` 寫死為 10 m（`MARKING_CYCLE_LENGTH_M`）。一般道路、匝道、施工區、槽化線的標線規格不一定是 4 m + 6 m；只看時序無法分辨「車快」還是「線短」。180755 高估 ≈1.6 倍，與 10 m / 6 m ≈ 1.67 吻合——**這只是假設，未驗證**，但說明這條路缺少一個畫面內無法取得的物理量。

3. **30 fps 量化上限**。週期 10–24 幀，±1 幀 = ±4–10%，還沒算上漏檢 / 多檢。

4. **投入產出比**。影片多數已有 GPS（內嵌 / OCR），標線測速只對無 GPS 素材（`D:/norm`）有意義，且那批素材正是表現最差的（夜間、新相機、施工區）。

---

## 4. 重啟條件（§0.3）

- 取得**真實 60 fps** 素材；或
- 有「無 GPS 也必須測速」的明確需求，**且**已備好雙機同路段 gold 真值；或
- 改用能取得距離尺度的方法（例如已知相機高度的單目視覺里程計），而不是再調標線門檻。

---

## 5. 保留的可重用資產

| 資產 | 路徑 | 說明 |
|------|------|------|
| Hood 偵測器 | `utils/hood_detector.py` | 中央欄 V 曲線階躍偵測車體上緣，4 前鏡頭 err 0.002–0.032 |
| 車道事件掃描 | `utils/lane_events.py` + `scripts/scan_lane_events.py` | solid/single/transition/gap_both，5 支全 PASS |
| 人工標示工具 | `utils/manual_annotator.py` | 單點穿越標記、閃爍濾波、刪除軌跡、續標保留 |
| Bronze 真值方法 | `scripts/eval_mask_spike.py` | 參考列±4列瀑布盲判＋8點抽查校準，mean bias +0.125f |
| eval_cross.py | `scripts/eval_cross.py` | Task C 驗收（greedy per-side、tol=1.0/2.0） |
| eval_mask_spike.py | `scripts/eval_mask_spike.py` | 三路訊號 paired comparison（classical/ai-conf/ai-geo） |
| UFLDv2 Spike 結論 | — | 混合策略：classical 存活時沿用，僅 classical 近零死＋ai_geo 強週期才切 AI |

---

## 6. 教訓

**驗收指標要直接對準最終目標（速度誤差 / 誤報率），而不是中間量（穿越幀號絕對誤差）。**

- 我們花了數十輪修正在「讓 auto 穿越幀號貼近人工標記」，但這偏移在速度計算（間隔）中會互相抵消，**對速度準確度零貢獻**。
- 真正傷害速度的是「漏脈衝 / 多脈衝」（週期 ×2 或 ×0.5），這需要**物理距離尺度**（非時序可得）或**更高幀率**（降低量化誤差）才能解決。
- 任何門檻調整若未在「雙機同路段 gold 真值」上驗證，都只是在 D 槽 GPS 上過擬合。

---

## 7. 代碼現狀（凍結後）

- `SpeedSettings.enabled` 預設 `False`（`models/capture_settings.py:88`）
- UI 開關/選單/疊加文字皆標示「（實驗）」
- 畫面顯示視覺速度處加「實驗」標記
- 有 GPS 時 `Controller.get_current_speed()` 優先回傳 GPS（既有行為，**已確認、未改**）
- 所有演算法碼（`marking_detector`/`vision_speed`/`manual_annotator`/`lane_events`/`hood_detector`）保留不刪
- 測試全部通過（348 passed, 2 skipped）