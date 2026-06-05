import logging
import shutil
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select

from app.models import Recording, Station

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
RECORDINGS_DIR = BASE_DIR / "recordings"
LOGS_DIR = RECORDINGS_DIR / "logs"
LOGOS_DIR = RECORDINGS_DIR / "logos"
TRIMMED_DIR = BASE_DIR / "static" / "trimmed"
LEGACY_DATABASE_PATH = BASE_DIR / "audiologger.db"
DATABASE_PATH = RECORDINGS_DIR / "audiologger.db"

DATABASE_URL = f"sqlite:///{DATABASE_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)


def migrate_database_location() -> None:
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    if DATABASE_PATH.exists() or not LEGACY_DATABASE_PATH.exists():
        return
    shutil.copy2(LEGACY_DATABASE_PATH, DATABASE_PATH)
    logger.info("Migrated database to persistent volume: %s", DATABASE_PATH)


def init_db() -> None:
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    TRIMMED_DIR.mkdir(parents=True, exist_ok=True)
    LOGOS_DIR.mkdir(parents=True, exist_ok=True)

    migrate_database_location()
    SQLModel.metadata.create_all(engine)

    from app.stations import (
        ensure_stations_backup_exists,
        migrate_station_schema,
        reconcile_logos,
        restore_stations_from_backup_if_needed,
    )
    from app.models import Station

    migrate_station_schema()
    restored = restore_stations_from_backup_if_needed()
    ensure_stations_backup_exists()
    reconcile_logos()

    with Session(engine) as session:
        station_count = len(session.exec(select(Station)).all())

    logger.info("Database path: %s (exists=%s)", DATABASE_PATH, DATABASE_PATH.exists())
    logger.info("Stations in database: %d%s", station_count, " (restored from backup)" if restored else "")


def get_session():
    with Session(engine) as session:
        yield session


def get_recording_by_id(session: Session, recording_id: int) -> Recording | None:
    return session.get(Recording, recording_id)


def get_recordings(
    session: Session,
    station_id: str | None = None,
    date_filter: str | None = None,
) -> list[Recording]:
    statement = select(Recording).order_by(Recording.start_time.desc())

    if station_id:
        statement = statement.where(Recording.station_id == station_id)

    recordings = list(session.exec(statement).all())

    if date_filter:
        recordings = [
            r for r in recordings if r.start_time.strftime("%Y-%m-%d") == date_filter
        ]

    return recordings


def get_station_stats(session: Session, station_id: str) -> dict:
    recordings = get_recordings(session, station_id=station_id)
    completed = [r for r in recordings if r.status == "completed"]
    last_recording = completed[0] if completed else None

    return {
        "total_recordings": len(completed),
        "total_storage_mb": round(sum(r.file_size_mb for r in completed), 2),
        "last_recording": last_recording,
    }


def get_global_stats(session: Session) -> dict:
    recordings = list(
        session.exec(
            select(Recording).where(Recording.status == "completed")
        ).all()
    )

    total_hours = round(sum(r.duration_seconds for r in recordings) / 3600, 1)
    total_storage = round(sum(r.file_size_mb for r in recordings), 2)

    return {
        "total_recordings": len(recordings),
        "total_hours": total_hours,
        "total_storage_mb": total_storage,
    }
