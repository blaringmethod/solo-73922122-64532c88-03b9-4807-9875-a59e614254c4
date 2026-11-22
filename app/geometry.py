"""Integer-rectangle geometry for statistical blocks.

Coordinates use a shared metric coordinate system. Rectangles are half-open
integer boxes [x1, x2) x [y1, y2); touching edges therefore do not overlap and
do not count as shared coverage.
"""
from __future__ import annotations

from dataclasses import dataclass


class GeometryError(ValueError):
    """Raised for invalid rectangles or overlapping blocks in one year."""


@dataclass(frozen=True)
class Rect:
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def area(self) -> int:
        return (self.x2 - self.x1) * (self.y2 - self.y1)

    def intersection_area(self, other: "Rect") -> int:
        w = min(self.x2, other.x2) - max(self.x1, other.x1)
        h = min(self.y2, other.y2) - max(self.y1, other.y1)
        if w <= 0 or h <= 0:
            return 0
        return w * h

    def to_dict(self) -> dict:
        return {"x1": self.x1, "y1": self.y1, "x2": self.x2, "y2": self.y2}


def validate_rect(x1: int, y1: int, x2: int, y2: int, block_no: str) -> Rect:
    for name, value in (("x1", x1), ("y1", y1), ("x2", x2), ("y2", y2)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise GeometryError(f"块 {block_no} 的坐标 {name} 必须是整数")
    if x2 <= x1 or y2 <= y1:
        raise GeometryError(
            f"块 {block_no} 边界非法：要求 x2>x1 且 y2>y1，"
            f"收到 ({x1},{y1})-({x2},{y2})"
        )
    return Rect(x1, y1, x2, y2)


def assert_no_overlap(rects: dict[str, Rect]) -> None:
    """Ensure no two blocks of the same year share positive-area coverage."""
    items = sorted(rects.items())
    for i in range(len(items)):
        no_a, ra = items[i]
        for j in range(i + 1, len(items)):
            no_b, rb = items[j]
            if ra.intersection_area(rb) > 0:
                raise GeometryError(f"同年块 {no_a} 与 {no_b} 存在重叠区域")


def canonical_geometry(blocks: list[tuple[str, Rect]]) -> str:
    """Deterministic fingerprint of a year's boundaries (counts excluded)."""
    parts = [
        f"{no}:{r.x1},{r.y1},{r.x2},{r.y2}"
        for no, r in sorted(blocks, key=lambda t: t[0])
    ]
    return "|".join(parts)
