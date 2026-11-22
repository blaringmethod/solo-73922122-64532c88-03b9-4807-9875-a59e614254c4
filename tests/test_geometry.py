from app.geometry import Rect
from app.overlay import build_components


def R(x1, y1, x2, y2):
    return Rect(x1, y1, x2, y2)


def test_touching_edges_have_no_intersection():
    assert R(0, 0, 10, 10).intersection_area(R(10, 0, 20, 10)) == 0
    assert R(0, 0, 10, 10).intersection_area(R(5, 10, 8, 12)) == 0


def test_components_are_the_finest_whole_block_groups():
    # A-side: A covers [0,10)x[0,10); B sits far away.
    a = {"A": R(0, 0, 10, 10), "B": R(20, 20, 30, 30)}
    # B-side: A split into A1/A2 with identical footprint; B matches itself.
    b = {
        "A1": R(0, 0, 4, 10),
        "A2": R(4, 0, 10, 10),
        "B": R(20, 20, 30, 30),
    }
    comps = build_components(a, b)
    assert len(comps) == 2

    split = next(c for c in comps if c.a_blocks == ["A"])
    assert split.b_blocks == ["A1", "A2"]
    assert split.a_area == split.b_area == 100
    assert split.matched

    same = next(c for c in comps if c.a_blocks == ["B"])
    assert same.b_blocks == ["B"] and same.matched


def test_partial_coverage_component_is_unmatched():
    a = {"A": R(0, 0, 10, 10)}
    b = {"A1": R(0, 0, 8, 10)}  # area 80 vs 100
    comps = build_components(a, b)
    assert len(comps) == 1
    assert not comps[0].matched
    assert "覆盖范围不一致" in comps[0].reason(2024, 2025)


def test_side_only_components_are_unmatched():
    a = {"A": R(0, 0, 10, 10)}
    b = {"X": R(20, 20, 25, 25)}
    comps = build_components(a, b)
    assert len(comps) == 2
    by_side = {tuple(c.a_blocks): c for c in comps}
    assert ("A",) in by_side and ("X",) not in by_side
    reasons = [c.reason(2024, 2025) for c in comps]
    assert any("2024 年块" in r and "无对应块" in r for r in reasons)
    assert any("2025 年块" in r and "无对应块" in r for r in reasons)


def test_multi_block_chain_cannot_be_split():
    # Two A blocks both overlap one B block: one component, no finer match.
    a = {"A1": R(0, 0, 6, 10), "A2": R(6, 0, 10, 10)}
    b = {"B": R(0, 0, 10, 10)}
    comps = build_components(a, b)
    assert len(comps) == 1
    assert sorted(comps[0].a_blocks) == ["A1", "A2"]
    assert comps[0].b_blocks == ["B"]
    assert comps[0].matched
