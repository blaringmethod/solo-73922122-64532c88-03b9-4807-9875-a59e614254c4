"""Finest whole-block coverage matching between two years.

Build a bipartite intersection graph: an edge links a year-A block and a
year-B block whenever their rectangles share a positive-area intersection.
Each connected component is the *finest* whole-block combination with the
same footprint:

* blocks in different components never share coverage, so a whole-block match
  cannot merge two components;
* blocks inside one component are transitively linked through shared area, so
  every whole-block match covering one of them must cover all of them.

A component's coverage is "identical" only when every covered unit cell on
each side is also covered on the other side. Because blocks within a year do
not overlap, equal total areas on both sides of a component are equivalent to
full two-sided coverage (no side-only cells can hide once the sums agree).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .geometry import Rect


@dataclass
class Component:
    a_blocks: list[str] = field(default_factory=list)
    b_blocks: list[str] = field(default_factory=list)
    a_area: int = 0
    b_area: int = 0
    # Positive-area intersection pairs (a_no, b_no, area).
    intersections: list[tuple[str, str, int]] = field(default_factory=list)

    @property
    def matched(self) -> bool:
        return bool(self.a_blocks) and bool(self.b_blocks) and self.a_area == self.b_area

    def reason(self, a_year: int, b_year: int) -> str | None:
        if self.matched:
            return None
        if not self.b_blocks:
            return f"{a_year} 年块 {self.a_blocks} 的覆盖范围在 {b_year} 年无对应块"
        if not self.a_blocks:
            return f"{b_year} 年块 {self.b_blocks} 的覆盖范围在 {a_year} 年无对应块"
        return (
            f"块组合覆盖范围不一致：{a_year} 年面积 {self.a_area}，"
            f"{b_year} 年面积 {self.b_area}，存在仅一侧覆盖的区域"
        )


def build_components(
    a: dict[str, Rect], b: dict[str, Rect]
) -> list[Component]:
    # Union-find over nodes named ("a", no) / ("b", no).
    parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(node: tuple[str, str]) -> tuple[str, str]:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(n1: tuple[str, str], n2: tuple[str, str]) -> None:
        r1, r2 = find(n1), find(n2)
        if r1 != r2:
            parent[r2] = r1

    edges: list[tuple[str, str, int]] = []
    for no_a, ra in a.items():
        find(("a", no_a))
        for no_b, rb in b.items():
            area = ra.intersection_area(rb)
            if area > 0:
                union(("a", no_a), ("b", no_b))
                edges.append((no_a, no_b, area))

    groups: dict[tuple[str, str], Component] = {}
    for no_a, ra in a.items():
        comp = groups.setdefault(find(("a", no_a)), Component())
        comp.a_blocks.append(no_a)
        comp.a_area += ra.area
    for no_b, rb in b.items():
        comp = groups.setdefault(find(("b", no_b)), Component())
        comp.b_blocks.append(no_b)
        comp.b_area += rb.area

    edge_by_root: dict[tuple[str, str], list[tuple[str, str, int]]] = {}
    for no_a, no_b, area in edges:
        root = find(("a", no_a))
        edge_by_root.setdefault(root, []).append((no_a, no_b, area))

    result: list[Component] = []
    for root, comp in groups.items():
        comp.a_blocks.sort()
        comp.b_blocks.sort()
        comp.intersections = sorted(edge_by_root.get(root, []))
        result.append(comp)

    result.sort(
        key=lambda c: (
            c.a_blocks[0] if c.a_blocks else "",
            c.b_blocks[0] if c.b_blocks else "",
        )
    )
    return result
