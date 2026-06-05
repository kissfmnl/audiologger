from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select

from app.models import Recording

BASE_DIR = Path(__file__).resolve().parent.parent
DATABASE_PATH = BASE_DIR / "audiologger.db"
RECORDINGS_DIR = BASE_DIR / "recordings"
LOGS_DIR = RECORDINGS_DIR / "logs"
TRIMMED_DIR = BASE_DIR / "static" / "trimmed"

DATABASE_URL = f"sqlite:///{DATABASE_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)


def init_db() -> None:
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    TRIMMED_DIR.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(engine)


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
