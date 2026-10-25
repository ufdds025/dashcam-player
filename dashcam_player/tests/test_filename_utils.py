from __future__ import annotations

import fnmatch

from dashcam_player.utils.filename_utils import (
    build_capture_filename,
    capture_glob,
    normalize_ext,
    sanitize_component,
    source_stem,
)


def test_sanitize_component_strips_illegal() -> None:
    assert sanitize_component("a/b:c*?") == "a_b_c"
    assert sanitize_component("") == "video"
    assert sanitize_component("   .  ") == "video"


def test_sanitize_component_truncates() -> None:
    assert len(sanitize_component("x" * 100)) <= 40


def test_normalize_ext() -> None:
    assert normalize_ext("JPEG") == "jpg"
    assert normalize_ext("jpeg") == "jpg"
    assert normalize_ext("PNG") == "png"


def test_source_stem() -> None:
    assert source_stem("/path/to/MYCLIP.mp4") == "MYCLIP"
    assert source_stem("") == "video"
    assert source_stem("/a/b/前後.AVI") == "前後"


def test_build_includes_stem_and_frame() -> None:
    name = build_capture_filename("/v/2024_08_15_前方.MP4", 120, "capture", "JPEG")
    assert name == "capture_2024_08_15_前方_f000120.jpg"


def test_build_prefix_optional() -> None:
    name = build_capture_filename("/v/CLIP.mp4", 5, "", "PNG")
    assert name == "CLIP_f000005.png"


def test_build_deterministic_same_source_frame() -> None:
    a = build_capture_filename("/v/CLIP.mp4", 100, "capture", "JPEG")
    b = build_capture_filename("/v/CLIP.mp4", 100, "capture", "JPEG")
    assert a == b  # 同來源同幀 → 同名（覆蓋、不產生重複副本）


def test_build_different_source_distinct() -> None:
    a = build_capture_filename("/v/CLIP_A.mp4", 100, "capture", "JPEG")
    b = build_capture_filename("/v/CLIP_B.mp4", 100, "capture", "JPEG")
    assert a != b  # 不同來源 → 不同檔名


def test_capture_glob() -> None:
    pattern = capture_glob("capture", "CLIP", "jpg")
    assert fnmatch.fnmatch("capture_CLIP_f000001.jpg", pattern)
    assert not fnmatch.fnmatch("other_CLIP_f000001.jpg", pattern)
    assert not fnmatch.fnmatch("capture_CLIP_f000001.png", pattern)
