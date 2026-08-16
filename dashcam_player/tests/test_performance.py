"""效能基準測試（預設跳過，避免拖慢 CI）。

執行方式:
    RUN_PERF_TESTS=1 pytest dashcam_player/tests/test_performance.py -v -s
"""

from __future__ import annotations

import os
import time

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PERF_TESTS") != "1",
    reason="效能基準測試需設定 RUN_PERF_TESTS=1 才執行",
)


@pytest.fixture(scope="module")
def synthetic_1080p_video(tmp_path_factory) -> str:
    """生成 3 秒 1080p @30fps 測試影片（含中文檔名，同時驗證 Unicode 路徑）。"""
    import cv2

    path = str(tmp_path_factory.mktemp("perf") / "基準測試_1080p.mp4")
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(path, fourcc, 30.0, (1920, 1080))
    if not writer.isOpened():
        pytest.skip("mp4v 編碼器不可用，無法生成測試影片")
    rng = np.random.default_rng(42)
    for _ in range(90):
        writer.write(rng.integers(0, 256, (1080, 1920, 3), dtype=np.uint8))
    writer.release()
    return path


def test_decode_fps_1080p(synthetic_1080p_video) -> None:
    """1080p 影片解碼吞吐量：合成影片（易解碼）至少須達 15fps。"""
    from dashcam_player.utils.cv_utils import open_video_capture

    cap = open_video_capture(synthetic_1080p_video)
    assert cap.isOpened(), "應能以中文路徑開啟影片"
    try:
        start = time.perf_counter()
        n = 0
        while True:
            ret, _ = cap.read()
            if not ret:
                break
            n += 1
        elapsed = time.perf_counter() - start
    finally:
        cap.release()

    fps = n / elapsed if elapsed > 0 else 0.0
    print(f"\n[perf] 1080p 解碼: {n} 幀 / {elapsed:.2f}s = {fps:.1f} fps")
    assert fps > 15, f"1080p 解碼過慢: {fps:.1f} fps"


def test_imwrite_safe_1080p(tmp_path) -> None:
    """1080p JPEG 寫入（imwrite_safe + 中文路徑）需在 2 秒內完成。"""
    import cv2

    from dashcam_player.utils.cv_utils import imwrite_safe

    rng = np.random.default_rng(7)
    frame = rng.integers(0, 256, (1080, 1920, 3), dtype=np.uint8)
    path = str(tmp_path / "基準_1080p.jpg")

    start = time.perf_counter()
    ok = imwrite_safe(path, frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    elapsed = time.perf_counter() - start

    assert ok
    print(f"\n[perf] 1080p JPEG 寫入: {elapsed * 1000:.0f} ms")
    assert elapsed < 2.0, f"1080p JPEG 寫入過慢: {elapsed:.2f}s"
