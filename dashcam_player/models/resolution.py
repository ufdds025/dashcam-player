from __future__ import annotations

from enum import Enum


class Resolution(Enum):
    """影片截圖/匯出解析度選項"""

    ORIGINAL = "原始"
    HD720 = "1280x720"
    HD1080 = "1920x1080"
    QHD = "2560x1440"
    UHD4K = "3840x2160"

    @classmethod
    def from_value(cls, value: str) -> Resolution:
        """從字串值建立 Resolution 列舉，含舊設定相容性處理

        直接比對 value 屬性，找不到時 fallback 到 ORIGINAL。
        """
        for member in cls:
            if member.value == value:
                return member
        return cls.ORIGINAL

    @property
    def is_original(self) -> bool:
        """是否為原始解析度"""
        return self == Resolution.ORIGINAL

    @property
    def scale_str(self) -> str:
        """回傳傳遞給 apply_resolution() 的字串，ORIGINAL → '原始'"""
        return self.value

    @classmethod
    def values(cls) -> list[str]:
        """回傳所有顯示字串列表，供 combo box 使用"""
        return [m.value for m in cls]
