import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.TemporaryDirectory()
    db_path = os.path.join(tmp.name, "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("SEED_DEMO", "0")

    # Import lazily so DB_PATH is read from the patched environment.
    import importlib

    from app import database, main

    importlib.reload(database)
    importlib.reload(main)

    with TestClient(main.app) as c:
        yield c

    tmp.cleanup()


def rect(no, x1, y1, x2, y2):
    return {"block_no": no, "x1": x1, "y1": y1, "x2": x2, "y2": y2}


def upload(client, year, blocks):
    return client.put("/boundaries", json={"year": year, "blocks": blocks})


def test_geometry_overlap_and_invalid_rect_rejected(client):
    r = upload(client, 2024, [rect("A", 0, 0, 10, 10), rect("B", 5, 5, 15, 15)])
    assert r.status_code == 422 and "重叠" in r.json()["detail"]

    r = upload(client, 2024, [rect("A", 0, 0, 0, 10)])
    assert r.status_code == 422 and "边界非法" in r.json()["detail"]

    r = upload(client, 2024, [rect("A", 0, 0, 10, 10), rect("A", 1, 1, 2, 2)])
    assert r.status_code == 422 and "重复" in r.json()["detail"]


def test_edge_touching_does_not_overlap(client):
    r = upload(client, 2024, [rect("A", 0, 0, 10, 10), rect("B", 10, 0, 20, 10)])
    assert r.status_code == 200
    assert r.json()["block_count"] == 2


def test_split_block_is_finest_matching_group(client):
    assert upload(client, 2024, [rect("A", 0, 0, 10, 10), rect("B", 10, 0, 20, 10)]).status_code == 200
    assert upload(
        client, 2025,
        [rect("A1", 0, 0, 6, 10), rect("A2", 6, 0, 10, 10), rect("B", 10, 0, 20, 10)],
    ).status_code == 200

    r = client.get("/yoy/compare", params={"year_a": 2024, "year_b": 2025})
    body = r.json()
    assert len(body["groups"]) == 2

    split = next(g for g in body["groups"] if [b["block_no"] for b in g["year_a_blocks"]] == ["A"])
    assert split["status"] == "missing_counts"
    assert split["reason"] and "缺数块" in split["reason"]
    assert {b["block_no"] for b in split["missing"]} == {"A", "A1", "A2"}

    only_b = next(g for g in body["groups"] if g is not split)
    # B blocks exist on both sides but counts are missing everywhere.
    assert [b["block_no"] for b in only_b["year_a_blocks"]] == ["B"]
    assert [b["block_no"] for b in only_b["year_b_blocks"]] == ["B"]
    assert body["all_complete"] is False


def test_full_counts_produce_totals_and_difference(client):
    upload(client, 2024, [rect("A", 0, 0, 10, 10)])
    upload(client, 2025, [rect("A1", 0, 0, 4, 10), rect("A2", 4, 0, 10, 10)])
    assert client.post("/counts", json={
        "year": 2024, "block_no": "A", "households": 100, "boundary_rev": 1,
    }).status_code == 200
    assert client.post("/counts", json={
        "year": 2025, "block_no": "A1", "households": 30, "boundary_rev": 1,
    }).status_code == 200
    assert client.post("/counts", json={
        "year": 2025, "block_no": "A2", "households": 80, "boundary_rev": 1,
    }).status_code == 200

    r = client.get("/yoy/compare", params={"year_a": 2024, "year_b": 2025}).json()
    assert r["all_complete"] is True
    g = r["groups"][0]
    assert g["status"] == "matched"
    assert g["households_a_total"] == 100
    assert g["households_b_total"] == 110
    assert g["difference_b_minus_a"] == 10


def test_range_mismatch_listed_with_reason(client):
    upload(client, 2024, [rect("A", 0, 0, 10, 10)])
    # Smaller replacement footprint plus an extra block elsewhere.
    upload(client, 2025, [rect("A1", 0, 0, 8, 10), rect("C", 20, 20, 25, 25)])

    body = client.get("/yoy/compare", params={"year_a": 2024, "year_b": 2025}).json()
    statuses = {g["status"] for g in body["groups"]}
    assert statuses == {"range_mismatch"}
    reasons = " ".join(g["reason"] for g in body["groups"])
    assert "覆盖范围不一致" in reasons or "无对应块" in reasons


def test_boundary_replacement_retention_and_reset(client):
    upload(client, 2024, [rect("A", 0, 0, 10, 10), rect("B", 10, 0, 20, 10)])
    client.post("/counts", json={"year": 2024, "block_no": "A", "households": 50, "boundary_rev": 1})
    client.post("/counts", json={"year": 2024, "block_no": "B", "households": 60, "boundary_rev": 1})

    # A unchanged -> retain; B moved -> new revision, counts void; C new -> rev 2.
    r = upload(client, 2024, [rect("A", 0, 0, 10, 10), rect("B", 11, 0, 21, 10), rect("C", 0, 10, 5, 15)])
    body = r.json()
    assert body["retained_blocks"] == ["A"]
    assert sorted(body["reset_blocks"]) == ["B", "C"]
    assert body["removed_blocks"] == []

    blocks = {b["block_no"]: b for b in body["blocks"]}
    assert blocks["A"]["boundary_rev"] == 1 and blocks["A"]["households"] == 50
    assert blocks["B"]["boundary_rev"] == 2 and blocks["B"]["households"] is None
    assert blocks["C"]["boundary_rev"] == 2 and blocks["C"]["households"] is None


def test_removed_block_disappears_on_full_replace(client):
    upload(client, 2024, [rect("A", 0, 0, 10, 10), rect("B", 10, 0, 20, 10)])
    r = upload(client, 2024, [rect("A", 0, 0, 10, 10)])
    assert r.json()["removed_blocks"] == ["B"]
    nos = [b["block_no"] for b in client.get("/blocks/2024").json()]
    assert nos == ["A"]


def test_count_report_requires_current_revision(client):
    upload(client, 2024, [rect("A", 0, 0, 10, 10)])
    # Change the boundary -> revision 2.
    upload(client, 2024, [rect("A", 0, 0, 12, 10)])

    r = client.post("/counts", json={
        "year": 2024, "block_no": "A", "households": 9, "boundary_rev": 1,
    })
    assert r.status_code == 409 and "修订号不匹配" in r.json()["detail"]

    r = client.post("/counts", json={
        "year": 2024, "block_no": "A", "households": 9, "boundary_rev": 2,
    })
    assert r.status_code == 200

    r = client.post("/counts", json={
        "year": 2024, "block_no": "GHOST", "households": 1, "boundary_rev": 2,
    })
    assert r.status_code == 404


def test_range_result_reused_when_only_counts_change(client):
    upload(client, 2024, [rect("A", 0, 0, 10, 10)])
    upload(client, 2025, [rect("A", 0, 0, 10, 10)])

    first = client.get("/yoy/compare", params={"year_a": 2024, "year_b": 2025}).json()
    assert first["reused_range_result"] is False

    client.post("/counts", json={"year": 2024, "block_no": "A", "households": 10, "boundary_rev": 1})
    second = client.get("/yoy/compare", params={"year_a": 2024, "year_b": 2025}).json()
    assert second["reused_range_result"] is True
    assert second["state_id"] > first["state_id"]

    # After a boundary change the cache must miss.
    upload(client, 2025, [rect("A", 0, 0, 9, 10)])
    third = client.get("/yoy/compare", params={"year_a": 2024, "year_b": 2025}).json()
    assert third["reused_range_result"] is False


def test_snapshot_is_immutable_after_later_writes(client):
    upload(client, 2024, [rect("A", 0, 0, 10, 10)])
    upload(client, 2025, [rect("A", 0, 0, 10, 10)])
    snap = client.get("/yoy/compare", params={"year_a": 2024, "year_b": 2025}).json()

    client.post("/counts", json={"year": 2024, "block_no": "A", "households": 77, "boundary_rev": 1})
    stored = client.get(f"/yoy/comparisons/{snap['comparison_id']}").json()
    assert stored["state_id"] == snap["state_id"]
    assert stored["groups"][0]["households_a_total"] is None


def test_failed_write_preserves_original_data(client):
    upload(client, 2024, [rect("A", 0, 0, 10, 10)])
    client.post("/counts", json={"year": 2024, "block_no": "A", "households": 42, "boundary_rev": 1})

    # Invalid replacement (overlap): request rejected, old boundary and count remain.
    r = upload(client, 2024, [rect("A", 0, 0, 10, 10), rect("X", 5, 0, 8, 10)])
    assert r.status_code == 422
    blocks = client.get("/blocks/2024").json()
    assert len(blocks) == 1 and blocks[0]["households"] == 42
    assert blocks[0]["x2"] == 10

    # Stale-revision report rejected, count untouched.
    r = client.post("/counts", json={
        "year": 2024, "block_no": "A", "households": 999, "boundary_rev": 2,
    })
    assert r.status_code == 409
    assert client.get("/blocks/2024").json()[0]["households"] == 42


def test_state_history_tracks_every_commit(client):
    upload(client, 2024, [rect("A", 0, 0, 10, 10)])
    client.post("/counts", json={"year": 2024, "block_no": "A", "households": 5, "boundary_rev": 1})
    state = client.get("/state").json()
    changes = [h["change"] for h in state["history"]]
    assert changes == ["boundary_replace", "count_report"]
    assert state["state_id"] == len(state["history"])


def test_max_100_blocks_enforced(client):
    blocks = [rect(f"B{i:03d}", i, 0, i + 1, 1) for i in range(101)]
    r = upload(client, 2024, blocks)
    assert r.status_code == 422


def test_empty_year_comparison(client):
    upload(client, 2024, [rect("A", 0, 0, 10, 10)])
    body = client.get("/yoy/compare", params={"year_a": 2024, "year_b": 2025}).json()
    assert body["all_complete"] is False
    assert body["groups"][0]["status"] == "range_mismatch"
    assert "无对应块" in body["groups"][0]["reason"]
