from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.models import Trip
router = APIRouter(prefix="/trips", tags=["trips"])

def _trip_dict(t: Trip) -> dict:
    return {"id": t.id, "line_id": t.line_id, "trip_no": t.trip_no,
            "planned_depart": t.planned_depart.isoformat(), "vehicle_no": t.vehicle_no,
            "cancelled": t.cancelled}

@router.get("")
def list_trips(line_id: int | None = None, db: Session = Depends(get_db)):
    q = select(Trip).order_by(Trip.planned_depart)
    if line_id is not None: q = q.where(Trip.line_id == line_id)
    return [_trip_dict(r) for r in db.scalars(q).all()]

def _set_cancelled(trip_id: int, cancelled: bool, db: Session):
    trip = db.get(Trip, trip_id)
    if not trip: raise HTTPException(404, "班次不存在")
    trip.cancelled = cancelled
    db.commit(); db.refresh(trip)
    return _trip_dict(trip)

@router.post("/{trip_id}/cancel")
def cancel_trip(trip_id: int, db: Session = Depends(get_db)):
    return _set_cancelled(trip_id, True, db)

@router.post("/{trip_id}/restore")
def restore_trip(trip_id: int, db: Session = Depends(get_db)):
    return _set_cancelled(trip_id, False, db)
