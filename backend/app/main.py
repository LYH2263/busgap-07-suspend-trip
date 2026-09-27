from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect, text

from app.api.router import api_router
from app.config import settings
from app.database import Base, SessionLocal, engine
from app.services.seed import seed_if_empty


def _ensure_trip_cancelled_column() -> None:
    """create_all 不会给已存在的 trips 表补列，这里做一次幂等迁移。"""
    if "cancelled" in {c["name"] for c in inspect(engine).get_columns("trips")}:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE trips ADD COLUMN cancelled BOOLEAN NOT NULL DEFAULT FALSE"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _ensure_trip_cancelled_column()
    if settings.seed_on_empty:
        db = SessionLocal()
        try:
            seed_if_empty(db)
        finally:
            db.close()
    yield


app = FastAPI(title="BusGap", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router, prefix="/api")
