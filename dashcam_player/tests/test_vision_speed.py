from __future__ import annotations

import numpy as np

from dashcam_player.utils.vision_speed import _optical_flow_displacement


def _blob_img(h: int = 200, w: int = 300, cy: int = 110, cx: int = 150) -> np.ndarray:
    """灰底亮點影像（高斯斑點，供 Farneback 追蹤）。"""
    img = np.full((h, w), 50, dtype=np.uint8)
    yy, xx = np.mgrid[0:h, 0:w]
    blob = np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * 8.0 ** 2)))
    img = img + (blob * 120).astype(np.uint8)
    return img


class TestOpticalFlowDisplacement:
    def test_downward_motion_detected(self) -> None:
        prev = _blob_img(cy=110)
        curr = _blob_img(cy=116)  # 向下移 6px
        disp = _optical_flow_displacement(prev, curr)
        assert disp is not None
        assert 2.0 < disp < 12.0

    def test_identical_frames_returns_none(self) -> None:
        img = _blob_img()
        assert _optical_flow_displacement(img, img.copy()) is None

    def test_shape_mismatch_returns_none(self) -> None:
        assert _optical_flow_displacement(np.zeros((100, 100), np.uint8),
                                          np.zeros((90, 100), np.uint8)) is None

    def test_min_shift_threshold(self) -> None:
        prev = _blob_img()
        curr = _blob_img()
        curr[2:4, :] = 0  # 極小局部變動，不足以構成可信位移
        assert _optical_flow_displacement(prev, curr, min_shift=2.0) is None
