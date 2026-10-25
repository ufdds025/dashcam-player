"""人工標示工具測試（閃爍對濾波 / 刪除軌跡存取）。

皆為 headless 純函式測試，不開 GUI。
"""
from __future__ import annotations

import json
import os

import numpy as np

from dashcam_player.utils.manual_annotator import (
    _draw_overlay,
    _load_annotations,
    _load_removed,
    _remove_flicker_pairs,
    _save_annotations,
    _side_crossings,
    _split_manual_auto,
)


def _mk(frame: float, typ: str, side: str = "left", auto: bool = True) -> dict:
    return {"frame": frame, "type": typ, "side": side, "auto": auto}


class TestRemoveFlickerPairs:
    def test_adjacent_opposite_dropped(self) -> None:
        """前一幀 bw、下一幀 wb（同側）→ 閃爍，捨棄 wb。"""
        anns = [_mk(10, "bw"), _mk(11, "wb"), _mk(30, "bw"), _mk(40, "wb")]
        out = _remove_flicker_pairs(anns)
        assert [(int(a["frame"]), a["type"]) for a in out] == [(10, "bw"), (30, "bw"), (40, "wb")]

    def test_normal_dwell_kept(self) -> None:
        """正常亮態（bw→wb 間隔 ≥3 幀）保留；140km/h 真實短亮態 ~3.3 幀不受影響。"""
        anns = [_mk(10, "bw"), _mk(13, "wb"), _mk(30, "bw"), _mk(35, "wb")]
        out = _remove_flicker_pairs(anns)
        assert len(out) == 4

    def test_short_dwell_dropped(self) -> None:
        """dwell 2.x 幀（180655 刪除軌跡主體，duty 遠低於規範 40%）視為閃爍。"""
        anns = [_mk(10, "bw"), _mk(12.3, "wb"), _mk(30, "bw"), _mk(35, "wb")]
        out = _remove_flicker_pairs(anns)
        assert [(a["frame"], a["type"]) for a in out] == [
            (10, "bw"), (30, "bw"), (35, "wb")]

    def test_sides_independent(self) -> None:
        """左右側獨立判斷，不跨側誤刪。"""
        anns = [_mk(10, "bw", "left"), _mk(11, "wb", "right")]
        out = _remove_flicker_pairs(anns)
        assert len(out) == 2

    def test_same_type_untouched(self) -> None:
        """同型序列由交替濾波處理，本函式不動。"""
        anns = [_mk(10, "bw"), _mk(11, "bw"), _mk(30, "wb")]
        out = _remove_flicker_pairs(anns)
        assert len(out) == 3

    def test_empty(self) -> None:
        assert _remove_flicker_pairs([]) == []

    def test_subframe_rounding(self) -> None:
        """次幀內插小數（10.4 vs 11.2，浮點差 0.8）視為相鄰。"""
        anns = [_mk(10.4, "bw"), _mk(11.2, "wb")]
        assert len(_remove_flicker_pairs(anns)) == 1

    def test_blink_off_dropped_bw_kept(self) -> None:
        """高速 blink（092148 右側實測）：bw 1166.4 → wb 1167.8（差 1.4 幀），
        wb 捨棄、bw 保留。四捨五入會算成差 2 而漏擋，故須用浮點比較。"""
        anns = [_mk(1166.4, "bw", "right"), _mk(1167.8, "wb", "right"),
                _mk(1175.3, "bw", "right"), _mk(1176.9, "wb", "right")]
        out = _remove_flicker_pairs(anns)
        assert [(a["frame"], a["type"]) for a in out] == [
            (1166.4, "bw"), (1175.3, "bw")]


class TestSideCrossings:
    """出現/消失絕對門檻（092148 左彎道漏檢迴歸）。

    注意：min_persist=3 意味著需連續 3 幀維持同一狀態才確認交界，
    這是刻意設計（AGENTS.md 09-06 節），用於過濾單/雙幀雜訊震盪。
    """

    def test_thin_entry_fires_after_persist(self) -> None:
        """左側細線進入（0→0.076→0.165）：min_persist=3 需 3 幀確認，
        觸發幀為 pending_start（第 3 幀確認時回溯到第 1 幀）。
        預期：索引 5(0.076) 開始，索引 7 確認 → frame 105。"""
        occ = [0.0] * 5 + [0.076, 0.165, 0.227, 0.188, 0.15, 0.0, 0.0]
        out = _side_crossings(occ, start=100)
        assert out, "應有穿越"
        assert out[0][1] == "bw"
        # min_persist=3：索引 5,6,7 確認 → pending_start=5 → frame 105
        assert out[0][0] == 105.0

    def test_disappearance_fires_wb_after_persist(self) -> None:
        """消失需連續 3 幀 < eps_down 才確認 wb。
        給 3 個零結尾：0.2→0.25→0.22→0.2→0.18→0.0→0.0→0.0"""
        occ = [0.0, 0.2, 0.25, 0.22, 0.2, 0.18, 0.0, 0.0, 0.0]
        out = _side_crossings(occ, start=0)
        types = [t for _, t in out]
        assert types == ["bw", "wb"]
        # bw: 索引 1,2,3 確認 → pending_start=1 → frame 1
        # wb: 索引 6,7,8 確認 → pending_start=6 → frame 6
        assert out[0][0] == 1.0
        assert out[1][0] == 6.0

    def test_all_zero_no_events(self) -> None:
        assert _side_crossings([0.0] * 30, start=0) == []

    def test_too_few_nonzero_no_events(self) -> None:
        assert _side_crossings([0.0] * 28 + [0.05, 0.06], start=0) == []

    def test_noise_below_eps_ignored(self) -> None:
        """eps 以下抖動（0.005-0.015）不觸發。"""
        occ = [0.0, 0.01, 0.015, 0.005, 0.012] + [0.0] * 25
        assert _side_crossings(occ, start=0) == []


class TestRemovedTrail:
    def _write(self, path: str, anns: list[dict], removed: list[dict]) -> None:
        _save_annotations(path, "v.avi", 2560, 1440, 30.0,
                          (1000, 800, 1300, 960), anns, removed)

    def test_roundtrip(self, tmp_path) -> None:
        p = str(tmp_path / "a_annotations.json")
        anns = [_mk(10, "bw", auto=True)]
        removed = [{**_mk(12, "wb", auto=True), "removed_via": "r",
                    "removed_box": [1000, 800, 1300, 960]}]
        self._write(p, anns, removed)
        assert _load_removed(p) == removed
        data = json.load(open(p, encoding="utf-8"))
        assert data["total_removed"] == 1
        assert data["total_annotations"] == 1

    def test_no_removed_no_key(self, tmp_path) -> None:
        """無刪除時不寫 removed key（舊檔相容，讀回空）。"""
        p = str(tmp_path / "b_annotations.json")
        self._write(p, [_mk(10, "bw")], [])
        data = json.load(open(p, encoding="utf-8"))
        assert "removed_annotations" not in data
        assert _load_removed(p) == []

    def test_missing_file(self, tmp_path) -> None:
        assert _load_removed(str(tmp_path / "none.json")) == []

    def test_legacy_file_without_key(self, tmp_path) -> None:
        """舊檔無 removed key → 讀回空，不報錯。"""
        p = str(tmp_path / "c_annotations.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"annotations": [_mk(10, "bw")]}, f)
        assert _load_removed(p) == []
        assert os.path.exists(p)


def _cross(frame: int, side: str = "left") -> dict:
    return {"kind": "cross", "side": side, "point": [1100, 870],
            "frame": frame, "time": frame / 30.0, "auto": False}


class TestManualPreservedOnRebox:
    """重設框架／重跑自動偵測時，人工標記一律保留（只丟棄舊程式候選）。

    迴歸：續標 Y＋重設框架 Y 曾清空全部標記，進入後看不到人工資料。
    """

    def test_split(self) -> None:
        anns = [_cross(100), _mk(110, "bw", auto=False),
                _mk(120, "bw", auto=True), _mk(130, "wb", auto=True)]
        manual, auto = _split_manual_auto(anns)
        assert len(manual) == 2 and len(auto) == 2
        assert all(not bool(a.get("auto", False)) for a in manual)

    def test_rebox_merge_roundtrip(self, tmp_path) -> None:
        """模擬重設框架流程：舊檔（人工＋舊auto）→ 保留人工＋新auto → 存檔重載後人工完整。"""
        p = str(tmp_path / "rebox_annotations.json")
        old = [_cross(100), _mk(110, "bw", auto=False),
               _mk(105, "bw", auto=True), _mk(115, "wb", auto=True)]
        _save_annotations(p, "v.avi", 2560, 1440, 30.0, (1000, 800, 1300, 960), old, [])
        # 重設框架：只丟舊 auto，人工保留，與新 auto 合併
        loaded = _load_annotations(p)
        manual_kept, _ = _split_manual_auto(loaded)
        new_auto = [_mk(200, "bw", auto=True), _mk(210, "wb", auto=True)]
        merged = manual_kept + new_auto
        _save_annotations(p, "v.avi", 2560, 1440, 30.0, (900, 700, 1400, 960), merged, [])
        reloaded = _load_annotations(p)
        kinds = [(a.get("kind", a.get("type")), a.get("frame"), bool(a.get("auto", False)))
                 for a in reloaded]
        assert ("cross", 100, False) in kinds
        assert ("bw", 110, False) in kinds  # 人工 b/w 補標保留
        assert ("bw", 105, True) not in kinds  # 舊 auto 已丟棄
        assert ("bw", 200, True) in kinds  # 新 auto 已合併
        data = json.load(open(p, encoding="utf-8"))
        assert data["scan_box"] == {"x0": 900, "y0": 700, "x1": 1400, "y1": 960}

    def test_restart_clears_all(self, tmp_path) -> None:
        """續標 n（重新開始）才清空全部（含人工）——與重設框架區分。"""
        p = str(tmp_path / "restart_annotations.json")
        _save_annotations(p, "v.avi", 2560, 1440, 30.0, (1000, 800, 1300, 960),
                          [_cross(100), _mk(105, "bw", auto=True)], [])
        loaded = _load_annotations(p)
        manual_kept, _ = _split_manual_auto(loaded)
        assert len(manual_kept) == 1  # restart 流程外，split 本身不丟資料


class TestActiveSideOverlay:
    """Tab 側半框綠色標示（headless 煙霧測試，不開 GUI）。"""

    def _green_mask(self, img: np.ndarray) -> np.ndarray:
        return (img[:, :, 0] == 0) & (img[:, :, 1] == 255) & (img[:, :, 2] == 0)

    @staticmethod
    def _at(img: np.ndarray, y: int, x: int) -> tuple:
        return tuple(int(v) for v in img[y, x])

    def test_left_active(self) -> None:
        fr = np.zeros((200, 400, 3), dtype=np.uint8)
        out = _draw_overlay(fr, (100, 50, 300, 150), [], 0, 30.0, None, 400, 200,
                            None, "left")
        assert self._at(out, 51, 150) == (0, 255, 0), "左半上邊應為綠色"
        assert self._at(out, 51, 250) != (0, 255, 0), "右半上邊不應為綠色"

    def test_right_active(self) -> None:
        fr = np.zeros((200, 400, 3), dtype=np.uint8)
        out = _draw_overlay(fr, (100, 50, 300, 150), [], 0, 30.0, None, 400, 200,
                            None, "right")
        assert self._at(out, 51, 250) == (0, 255, 0), "右半上邊應為綠色"
        assert self._at(out, 51, 150) != (0, 255, 0), "左半上邊不應為綠色"

    def test_no_active_no_green(self) -> None:
        fr = np.zeros((200, 400, 3), dtype=np.uint8)
        out = _draw_overlay(fr, (100, 50, 300, 150), [], 0, 30.0, None, 400, 200)
        assert not self._green_mask(out).any(), "無 active_side 時不應有綠框"
