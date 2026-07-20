"""內建 GPS 位置模板

提供常見行車紀錄器的 GPS 文字區域預設位置。
每個模板綁定角落位置與文字排列模式，用於自動偵測的初始搜索區域。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GPSTemplate:
    """內建 GPS 位置模板"""

    name: str
    corner: str
    layout: str

    roi_x: float = 0.0
    roi_y: float = 0.0
    roi_w: float = 0.50
    roi_h: float = 0.15

    min_resolution_w: int = 0
    min_resolution_h: int = 0

    sample_text_pattern: str = ""
    num_lines: int = 1


# 預設模板：適用於多數行車紀錄器
_BUILTIN_TEMPLATES: list[GPSTemplate] = [
    GPSTemplate(
        name="左上角（最常見）",
        corner="top_left",
        layout="three_line",
        roi_x=0.0,
        roi_y=0.0,
        roi_w=0.50,
        roi_h=0.15,
        num_lines=3,
    ),
    GPSTemplate(
        name="右上角",
        corner="top_right",
        layout="three_line",
        roi_x=0.50,
        roi_y=0.0,
        roi_w=0.50,
        roi_h=0.15,
        num_lines=3,
    ),
    GPSTemplate(
        name="左下角",
        corner="bottom_left",
        layout="single_line",
        roi_x=0.0,
        roi_y=0.85,
        roi_w=0.50,
        roi_h=0.15,
        num_lines=1,
    ),
    GPSTemplate(
        name="右下角",
        corner="bottom_right",
        layout="single_line",
        roi_x=0.50,
        roi_y=0.85,
        roi_w=0.50,
        roi_h=0.15,
        num_lines=1,
    ),
]


def get_templates(min_width: int = 0, min_height: int = 0) -> list[GPSTemplate]:
    """取得符合解析度門檻的模板列表"""
    return [
        t
        for t in _BUILTIN_TEMPLATES
        if t.min_resolution_w <= min_width and t.min_resolution_h <= min_height
    ]


def get_template_by_corner(corner: str) -> GPSTemplate | None:
    """根據角落名稱取得模板"""
    for t in _BUILTIN_TEMPLATES:
        if t.corner == corner:
            return t
    return None
