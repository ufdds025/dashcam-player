from __future__ import annotations

import queue

import numpy as np
import pytest

from dashcam_player.core.video_worker import VideoWorker


@pytest.fixture
def video_worker() -> VideoWorker:
    """Pre-configured VideoWorker with a fresh command queue."""
    w = VideoWorker()
    w._total_frames = 100
    w._fps = 30.0
    w._current_idx = 10
    w._cmd_queue = queue.Queue()
    return w


@pytest.fixture
def frame_1080p() -> np.ndarray:
    return np.zeros((1080, 1920, 3), dtype=np.uint8)


@pytest.fixture
def frame_480p() -> np.ndarray:
    return np.zeros((480, 640, 3), dtype=np.uint8)


@pytest.fixture
def frame_small() -> np.ndarray:
    return np.zeros((100, 100, 3), dtype=np.uint8)
