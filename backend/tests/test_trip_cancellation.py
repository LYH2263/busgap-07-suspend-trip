"""Trip cancellation: excluded from pairing/timeline/suggestions, restorable, arrivals untouched."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.services.seed import seed_if_empty


@pytest.fixture()
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    seed_db = TestingSession()
    seed_if_empty(seed_db)
    seed_db.close()

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)  # no lifespan: global Postgres engine stays untouched
    app.dependency_overrides.clear()


def trip_id(client, trip_no):
    trips = {t["trip_no"]: t for t in client.get("/api/trips").json()}
    return trips[trip_no]["id"]


def run_events(client):
    return client.post("/api/reports/run?line_id=1").json()["events"]


def pairs_at(events, stop):
    return [(e["earlier_trip"], e["later_trip"]) for e in events if e["stop_name"] == stop]


def mentions(events, trip_no):
    return [e for e in events if trip_no in (e["earlier_trip"], e["later_trip"])]


def test_baseline_t04_has_events(client):
    assert mentions(run_events(client), "T04")


def test_cancel_t04_drops_its_events_and_recomputes(client):
    client.post(f"/api/trips/{trip_id(client, 'T04')}/cancel")
    events = run_events(client)
    assert mentions(events, "T04") == []
    # T01–T03 keep their adjacency at 市民中心: T01→T02 串车, T02→T03 大间隔
    assert pairs_at(events, "市民中心") == [("T01", "T02"), ("T02", "T03")]
    # timeline no longer draws T04
    marks = client.get("/api/reports/timeline?line_id=1&stop_name=市民中心").json()["marks"]
    assert [m["trip_no"] for m in marks] == ["T01", "T02", "T03"]
    # suggestions no longer name T04
    sugg = client.get("/api/reports/suggestions?line_id=1").json()["suggestions"]
    assert mentions(sugg, "T04") == []


def test_cancel_middle_trip_recomputes_adjacency(client):
    client.post(f"/api/trips/{trip_id(client, 'T03')}/cancel")
    events = run_events(client)
    assert mentions(events, "T03") == []
    # T02 and T04 become the new adjacent pair at 市民中心
    assert pairs_at(events, "市民中心") == [("T01", "T02"), ("T02", "T04")]


def test_cancelled_state_persists_across_requests(client):
    tid = trip_id(client, "T04")
    resp = client.post(f"/api/trips/{tid}/cancel")
    assert resp.status_code == 200 and resp.json()["cancelled"] is True
    trips = {t["trip_no"]: t for t in client.get("/api/trips").json()}
    assert trips["T04"]["cancelled"] is True
    assert trips["T01"]["cancelled"] is False


def test_restore_brings_trip_back(client):
    tid = trip_id(client, "T04")
    client.post(f"/api/trips/{tid}/cancel")
    assert mentions(run_events(client), "T04") == []
    resp = client.post(f"/api/trips/{tid}/restore")
    assert resp.status_code == 200 and resp.json()["cancelled"] is False
    assert mentions(run_events(client), "T04")
    marks = client.get("/api/reports/timeline?line_id=1&stop_name=市民中心").json()["marks"]
    assert "T04" in [m["trip_no"] for m in marks]


def test_cancel_does_not_change_arrival_times(client):
    before = {a["id"]: a["actual_arrive"] for a in client.get("/api/arrivals").json()}
    client.post(f"/api/trips/{trip_id(client, 'T04')}/cancel")
    client.post(f"/api/trips/{trip_id(client, 'T03')}/cancel")
    after = {a["id"]: a["actual_arrive"] for a in client.get("/api/arrivals").json()}
    assert before == after


def test_cancel_unknown_trip_404(client):
    assert client.post("/api/trips/9999/cancel").status_code == 404
    assert client.post("/api/trips/9999/restore").status_code == 404
