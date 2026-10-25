"""車道事件函式庫測試（皆為 headless 純函式，不開影片）。"""
from __future__ import annotations

from dashcam_player.utils.lane_events import (
    bias_stats,
    detect_lane_events,
    match_side,
    truth_events_from_cross,
    validate_events,
)


def _zero_runs(occ: list[float]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    s: int | None = None
    for i, v in enumerate(occ):
        if v <= 0:
            if s is None:
                s = i
        elif s is not None:
            runs.append((s, i - 1))
            s = None
    if s is not None:
        runs.append((s, len(occ) - 1))
    return runs


def _occ(frames: int, high: list[tuple[int, int]], level: float = 0.2) -> list[float]:
    o = [0.0] * frames
    for s, e in high:
        for i in range(s, min(e + 1, frames)):
            o[i] = level
    return o


class TestMatchSide:
    def test_basic(self) -> None:
        hits, pairs = match_side([4.3, 14.3], [6.0, 16.0], tol=1.0)
        assert hits == 0  # bias -1.7 超出 tol
        hits, pairs = match_side([4.3, 14.3], [6.0, 16.0], tol=2.0)
        assert hits == 2

    def test_greedy_nearest(self) -> None:
        hits, pairs = match_side([10.0], [9.0, 11.0], tol=1.5)
        assert hits == 1

    def test_empty(self) -> None:
        assert match_side([], [1.0], 1.0) == (0, [])


class TestBiasStats:
    def test_systematic_lead(self) -> None:
        b = bias_stats([4.3, 14.3, 24.3], [6.0, 16.0, 26.0])
        assert abs(b["median"] + 1.7) < 0.01
        assert b["p90"] - b["p10"] < 0.1  # spread 緊


class TestDetectLaneEvents:
    def test_solid_side(self) -> None:
        """左恆高 200 幀＋右起伏 → solid_left；右正常不報。"""
        n = 200
        frames = list(range(0, n * 3, 3))
        occ_l = [0.2] * n
        occ_r = [0.2 if i % 4 == 0 else 0.0 for i in range(n)]
        evs = detect_lane_events(frames, occ_l, occ_r, stride=3)
        types = [e["type"] for e in evs]
        assert "solid_left" in types
        assert "solid_right" not in types

    def test_both_static_no_solid(self) -> None:
        """雙側恆高（停等紅燈）→ 不判實線（對側無起伏）。"""
        n = 200
        frames = list(range(0, n * 3, 3))
        evs = detect_lane_events(frames, [0.2] * n, [0.2] * n, stride=3)
        assert [e for e in evs if e["type"].startswith("solid_")] == []

    def test_single_side_interruption(self) -> None:
        """左暫斷 60 幀後恢復、右持續起伏 → transition（缺口會結束）；
        未達 90 幀不成 single。」"""
        n = 200
        frames = list(range(0, n * 3, 3))
        occ_l = [0.0 if 20 <= i < 40 else (0.2 if i % 4 == 0 else 0.0) for i in range(n)]
        occ_r = [0.2 if i % 4 == 0 else 0.0 for i in range(n)]
        evs = detect_lane_events(frames, occ_l, occ_r, stride=3)
        types = [e["type"] for e in evs]
        assert "transition" in types
        assert not [t for t in types if t.startswith("single_")]

    def test_short_gap_ignored(self) -> None:
        """短缺口（<90 幀）不成事件，避免 2~3 次漏檢誤報。"""
        n = 100
        frames = list(range(0, n * 3, 3))
        occ_l = [0.0 if 20 <= i < 40 else (0.2 if i % 4 == 0 else 0.0) for i in range(n)]
        occ_r = [0.2 if i % 4 == 0 else 0.0 for i in range(n)]
        evs = detect_lane_events(frames, occ_l, occ_r, stride=3)
        assert [e for e in evs if e["type"].startswith("single_")] == []

    def test_gap_both(self) -> None:
        """雙側同時歸零 60 幀以上 → gap_both。"""
        n = 100
        frames = list(range(0, n * 3, 3))
        base = [0.2 if i % 4 == 0 else 0.0 for i in range(n)]
        occ_l = [0.0 if 30 <= i < 70 else base[i] for i in range(n)]
        occ_r = [0.0 if 30 <= i < 70 else base[i] for i in range(n)]
        evs = detect_lane_events(frames, occ_l, occ_r, stride=3)
        assert "gap_both" in [e["type"] for e in evs]

    def test_too_short_series(self) -> None:
        assert detect_lane_events([0, 3], [0.1, 0.2], [0.0, 0.0]) == []

    def test_end_clamped_to_last_sample(self) -> None:
        """事件 end 不超過取樣末幀（stride 尾段不外推）。"""
        frames = list(range(0, 300, 3))  # 末幀 297
        occ = [0.0] * 100
        other = [0.2 if i % 4 == 0 else 0.0 for i in range(100)]
        evs = detect_lane_events(frames, occ, other, stride=3)
        assert evs
        assert all(e["end"] <= 297 for e in evs)

    def test_stride_aliasing_constraint(self) -> None:
        """混疊約束文件化：10 幀週期、亮態 4 幀的虛線，stride=5 壞相位會
        造出 130 幀級假歸零（180655-F 實測），stride=3 不會。
        規則本身不防混疊——呼叫端必須用 stride≤3（見 compute docstring）。"""
        train = [0.2 if i % 10 < 4 else 0.0 for i in range(600)]
        bad5 = train[4::5]  # 壞相位：4,9,14,… mod10 恆落暗態(4~9)
        good3 = train[::3]
        zero5 = max((e - s + 1 for s, e in _zero_runs(bad5)), default=0) * 5
        zero3 = max((e - s + 1 for s, e in _zero_runs(good3)), default=0) * 3
        assert zero5 >= 100, "stride=5 壞相位應示範假長歸零"
        assert zero3 < 90, "stride=3 不應產生事件級假歸零"

    def test_transition_coincident_gaps(self) -> None:
        """雙側缺口重疊 → transition（092148-A 型：左缺 46、右缺 37）。"""
        n = 200
        frames = list(range(0, n * 3, 3))
        base = [0.2 if i % 4 == 0 else 0.0 for i in range(n)]
        occ_l = [0.0 if 80 <= i < 96 else base[i] for i in range(n)]
        occ_r = [0.0 if 70 <= i < 83 else base[i] for i in range(n)]
        evs = detect_lane_events(frames, occ_l, occ_r, stride=3)
        tr = [e for e in evs if e["type"] == "transition"]
        assert tr, "雙側缺口重疊應產出 transition"
        assert tr[0]["start"] <= 70 * 3 <= tr[0]["end"]

    def test_transition_solo_temporary_gap(self) -> None:
        """單側暫斷 40 幀後恢復、對側正常 → transition（092148-B 型）。"""
        n = 200
        frames = list(range(0, n * 3, 3))
        base = [0.2 if i % 4 == 0 else 0.0 for i in range(n)]
        occ_r = [0.0 if 60 <= i < 74 else base[i] for i in range(n)]
        evs = detect_lane_events(frames, base, occ_r, stride=3)
        tr = [e for e in evs if e["type"] == "transition"]
        assert tr, "單側暫斷應產出 transition"

    def test_transition_solo_permanent_excluded(self) -> None:
        """缺口延續到序列尾（永久單側）→ 不算 transition（歸 single/solid 管）。"""
        n = 200
        frames = list(range(0, n * 3, 3))
        base = [0.2 if i % 4 == 0 else 0.0 for i in range(n)]
        occ_l = [0.0 if i >= 100 else base[i] for i in range(n)]
        evs = detect_lane_events(frames, occ_l, base, stride=3)
        assert [e for e in evs if e["type"] == "transition"] == []
        assert "single_right" in [e["type"] for e in evs]


class TestTruthAndValidate:
    def _cross(self, side: str, lo: int, hi: int, step: int = 10) -> list[dict]:
        return [
            {"frame": float(f), "side": side, "kind": "cross"}
            for f in range(lo, hi, step)
        ]

    def test_truth_single_side(self) -> None:
        cross = self._cross("left", 0, 1800) + self._cross("right", 0, 750)
        evs = truth_events_from_cross(cross, 1800)
        singles = [e for e in evs if e["type"] == "single_left"]
        assert singles, "右歸零段應產出 single_left"
        assert singles[0]["start"] <= 780 <= singles[0]["end"]

    def test_validate_hit(self) -> None:
        truth = [{"type": "single_right", "start": 750, "end": 1799, "detail": ""}]
        det = [{"type": "single_right", "start": 760, "end": 1790, "detail": ""}]
        m = validate_events(det, truth)
        assert (m["precision"], m["recall"]) == (1.0, 1.0)

    def test_validate_type_mismatch(self) -> None:
        """僅存側不一致不算命中。"""
        truth = [{"type": "single_right", "start": 750, "end": 1799, "detail": ""}]
        det = [{"type": "single_left", "start": 750, "end": 1799, "detail": ""}]
        m = validate_events(det, truth)
        assert (m["precision"], m["recall"]) == (0.0, 0.0)

    def test_validate_solid_maps_to_single(self) -> None:
        """solid_* 以單側段為代理真值比對（真值尚無實線註記前）。"""
        truth = [{"type": "single_left", "start": 300, "end": 1049, "detail": ""}]
        det = [{"type": "solid_right", "start": 310, "end": 1040, "detail": ""}]
        m = validate_events(det, truth)
        assert m["recall"] == 1.0
