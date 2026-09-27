"""班次停运/恢复：检测、时间轴、建议、持久化。"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.services.bunch_engine import detect_bunching
from app.services.seed import seed_if_empty


@pytest.fixture()
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TestingSession = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    db = TestingSession()
    seed_if_empty(db)
    db.close()

    def override_get_db():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _trips_by_no(client):
    return {t["trip_no"]: t for t in client.get("/api/trips").json()}


def _run_events(client):
    resp = client.post("/api/reports/run", params={"line_id": 1})
    assert resp.status_code == 200
    return resp.json()["events"]


def _mentions(events, trip_no):
    return [e for e in events if e["earlier_trip"] == trip_no or e["later_trip"] == trip_no]


def test_seed_has_t04_events(client):
    events = _run_events(client)
    assert _mentions(events, "T04"), "种子数据下 T04 应参与间隔配对"


def test_cancel_removes_trip_from_events_timeline_suggestions(client):
    t04 = _trips_by_no(client)["T04"]
    resp = client.post(f"/api/trips/{t04['id']}/cancel")
    assert resp.status_code == 200
    assert resp.json()["cancelled"] is True

    events = _run_events(client)
    assert not _mentions(events, "T04"), "停运后 T04 不得出现在任何间隔事件里"
    assert _mentions(events, "T03"), "T03 与其余班次的事件应保留"

    marks = client.get("/api/reports/timeline", params={"line_id": 1}).json()["marks"]
    assert {m["trip_no"] for m in marks} == {"T01", "T02", "T03"}

    suggestions = client.get("/api/reports/suggestions", params={"line_id": 1}).json()["suggestions"]
    assert not _mentions(suggestions, "T04"), "建议里不得再点名 T04"


def test_cancel_middle_trip_recomputes_adjacency(client):
    trips = _trips_by_no(client)
    client.post(f"/api/trips/{trips['T02']['id']}/cancel")
    events = _run_events(client)
    assert not _mentions(events, "T02")
    # 市民中心：T01 07:06、T03 07:24，T02 停运后二者成为新相邻对，间隔 18 分钟
    pairs = {(e["stop_name"], e["earlier_trip"], e["later_trip"]): e for e in events}
    bridged = pairs[("市民中心", "T01", "T03")]
    assert bridged["gap_min"] == 18.0
    assert bridged["status"] == "large_gap"


def test_cancel_does_not_change_other_arrivals(client):
    before = {(a["trip_no"], a["stop_name"]): a["actual_arrive"]
              for a in client.get("/api/arrivals").json()}
    t04 = _trips_by_no(client)["T04"]
    client.post(f"/api/trips/{t04['id']}/cancel")
    after_rows = client.get("/api/arrivals").json()
    after = {(a["trip_no"], a["stop_name"]): a["actual_arrive"] for a in after_rows}
    assert len(after_rows) == len(before), "停运不得增删任何到站记录"
    for trip_no in ("T01", "T02", "T03"):
        for stop in ("起点站", "市民中心", "火车站", "终点站"):
            assert after[(trip_no, stop)] == before[(trip_no, stop)]


def test_cancel_state_persists_across_requests(client):
    t04 = _trips_by_no(client)["T04"]
    client.post(f"/api/trips/{t04['id']}/cancel")
    trips = _trips_by_no(client)  # 重新拉取，相当于离开页面再进来
    assert trips["T04"]["cancelled"] is True
    for no in ("T01", "T02", "T03"):
        assert trips[no]["cancelled"] is False


def test_restore_brings_trip_back(client):
    t04 = _trips_by_no(client)["T04"]
    client.post(f"/api/trips/{t04['id']}/cancel")
    assert not _mentions(_run_events(client), "T04")

    resp = client.post(f"/api/trips/{t04['id']}/restore")
    assert resp.status_code == 200
    assert resp.json()["cancelled"] is False

    events = _run_events(client)
    assert _mentions(events, "T04"), "恢复后重新检测，T04 应重新出现在报告里"
    marks = client.get("/api/reports/timeline", params={"line_id": 1}).json()["marks"]
    assert "T04" in {m["trip_no"] for m in marks}
    assert _trips_by_no(client)["T04"]["cancelled"] is False


def test_cancel_missing_trip_404(client):
    assert client.post("/api/trips/9999/cancel").status_code == 404
    assert client.post("/api/trips/9999/restore").status_code == 404


def test_engine_pairs_remaining_neighbors_after_filter():
    base = datetime(2026, 1, 1, 8, 0)
    arrivals = [
        {"stop_name": "A", "trip_no": "T1", "actual_arrive": base},
        {"stop_name": "A", "trip_no": "T2", "actual_arrive": base + timedelta(minutes=2)},
        {"stop_name": "A", "trip_no": "T3", "actual_arrive": base + timedelta(minutes=10)},
    ]
    active = [a for a in arrivals if a["trip_no"] != "T2"]  # T2 视为已停运
    events = detect_bunching(active, 8.0, 3.0, 15.0)
    assert len(events) == 1
    assert events[0].earlier_trip == "T1" and events[0].later_trip == "T3"
    assert events[0].gap_min == 10.0
