"""lane_mask.py 單元測試（純函式，無 torch／無權重需求）。"""

import cv2
import numpy as np

from dashcam_player.utils.lane_mask import (
    intervals_to_mask,
    lane_windows_at_ref,
    lane_x_at_y,
    mask_to_occupancy,
    rasterize_lanes,
)


class TestRasterizeLanes:
    def test_vertical_line_lights_one_column_band(self):
        lanes = [[(50.0, 0.0), (50.0, 99.0)]]
        m = rasterize_lanes(lanes, 100, 100, thickness=1)
        assert m.shape == (100, 100)
        assert m[:, 50].max() == 255
        assert m.sum() > 0

    def test_empty_and_single_point_ignored(self):
        m = rasterize_lanes([[], [(10.0, 10.0)]], 20, 20)
        assert m.sum() == 0

    def test_thickness_increases_pixels(self):
        lane = [[(50.0, 0.0), (50.0, 99.0)]]
        a = rasterize_lanes(lane, 100, 100, thickness=2).sum()
        b = rasterize_lanes(lane, 100, 100, thickness=8).sum()
        assert b > a > 0


class TestMaskToOccupancy:
    def test_left_right_split(self):
        m = np.zeros((10, 100), np.uint8)
        m[:, 10:30] = 255  # 左半 20 欄
        occ_l, occ_r = mask_to_occupancy(m, 0.5)
        assert occ_l == 0.4  # 20/50
        assert occ_r == 0.0

    def test_empty_mask(self):
        occ_l, occ_r = mask_to_occupancy(np.zeros((5, 40), np.uint8))
        assert (occ_l, occ_r) == (0.0, 0.0)

    def test_empty_array_safe(self):
        assert mask_to_occupancy(np.zeros((0, 0), np.uint8)) == (0.0, 0.0)


class TestIntervalsParity:
    def test_mask_occupancy_matches_interval_frac(self):
        # 傳統 frac = 區間總寬/半區寬；Mask 欄計數應一致（無重疊時）
        box_w, box_h, x0 = 200, 30, 1000
        intervals = [(1010, 1030), (1060, 1090)]  # 左半(1000-1100)內 20+30=50 欄
        m = intervals_to_mask(intervals, box_w, box_h, x_offset=x0)
        occ_l, occ_r = mask_to_occupancy(m, 0.5)
        assert occ_l == 0.5  # 50/100
        assert occ_r == 0.0

    def test_cv2_reference_sanity(self):
        assert cv2.__version__  # 確保 cv2 可用


class TestLaneXAtY:
    def test_linear_interp(self):
        assert lane_x_at_y([(0.0, 0.0), (10.0, 10.0)], 5.0) == 5.0

    def test_out_of_range_none(self):
        assert lane_x_at_y([(0.0, 0.0), (10.0, 10.0)], 11.0) is None
        assert lane_x_at_y([(0.0, 5.0)], 5.0) is None


class TestLaneWindowsAtRef:
    def test_windows_sorted_and_clamped(self):
        lanes = [[(1480.0, 0.0), (1480.0, 100.0)], [(100.0, 0.0), (100.0, 100.0)]]
        wins = lane_windows_at_ref(lanes, 50.0, 30.0, frame_w=1500)
        assert wins[0] == (70, 130)
        assert wins[1] == (1450, 1500)  # 右側 clamp

    def test_fully_outside_frame_excluded(self):
        lanes = [[(2000.0, 0.0), (2000.0, 100.0)]]
        assert lane_windows_at_ref(lanes, 50.0, 30.0, frame_w=1500) == []

    def test_no_intersection_excluded(self):
        lanes = [[(50.0, 0.0), (50.0, 10.0)]]
        assert lane_windows_at_ref(lanes, 50.0, 10.0, frame_w=500) == []
