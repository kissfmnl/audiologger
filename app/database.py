import logging
import os
import shutil
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import Recording, Station

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
TRIMMED_DIR = BASE_DIR / "static" / "trimmed"
LEGACY_DATABASE_PATH = BASE_DIR / "audiologger.db"


def resolve_recordings_dir() -> Path:
    """Gebruik Railway volume mount als die beschikbaar is (data blijft dan bestaan)."""
    volume_mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if volume_mount:
        return Path(volume_mount)

    override = os.environ.get("RECORDINGS_DIR", "").strip()
    if override:
        return Path(override)

    return BASE_DIR / "recordings"


RECORDINGS_DIR = resolve_recordings_dir()
LOGS_DIR = RECORDINGS_DIR / "logs"
LOGOS_DIR = RECORDINGS_DIR / "logos"
DATABASE_PATH = RECORDINGS_DIR / "audiologger.db"
STATIONS_BACKUP_PATH = RECORDINGS_DIR / "stations.backup.json"
STATIONS_BACKUP_BAK_PATH = RECORDINGS_DIR / "stations.backup.json.bak"

DATABASE_URL = f"sqlite:///{DATABASE_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)


def get_storage_status() -> dict:
    backup_count = 0
    if STATIONS_BACKUP_PATH.exists():
        try:
            import json

            backup_count = len(json.loads(STATIONS_BACKUP_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            backup_count = -1

    volume_mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    on_railway = bool(os.environ.get("RAILWAY_ENVIRONMENT"))
    persist_ok = bool(volume_mount) or not on_railway

    recordings_bytes = 0
    if RECORDINGS_DIR.exists():
        for path in RECORDINGS_DIR.rglob("*.mp3"):
            try:
                recordings_bytes += path.stat().st_size
            except OSError:
                pass

    disk_total = disk_used = disk_free = 0
    usage_percent = None
    try:
        disk_total, disk_used, disk_free = shutil.disk_usage(RECORDINGS_DIR)
        if disk_total > 0:
            usage_percent = round(disk_used / disk_total * 100, 1)
    except OSError:
        pass

    def _gb(n: int) -> float:
        return round(n / (1024 ** 3), 2)

    return {
        "recordings_dir": str(RECORDINGS_DIR),
        "volume_mount": volume_mount or None,
        "on_railway": on_railway,
        "persist_ok": persist_ok,
        "database_exists": DATABASE_PATH.exists(),
        "database_size_kb": round(DATABASE_PATH.stat().st_size / 1024, 1)
        if DATABASE_PATH.exists()
        else 0,
        "backup_exists": STATIONS_BACKUP_PATH.exists(),
        "backup_station_count": backup_count,
        "recordings_gb": _gb(recordings_bytes),
        "disk_total_gb": _gb(disk_total),
        "disk_used_gb": _gb(disk_used),
        "disk_free_gb": _gb(disk_free),
        "usage_percent": usage_percent,
    }


def estimate_archive_storage_gb(
    station_count: int,
    retention_days: int,
    *,
    hours_per_day: int = 24,
    mb_per_hour: float = 57.6,
) -> float:
    """Geschat archief bij 128 kbps MP3 (~57.6 MB per station-uur)."""
    total_mb = station_count * retention_days * hours_per_day * mb_per_hour
    return round(total_mb / 1024, 1)


def storage_capacity_plan(
    disk_total_gb: float,
    station_count: int,
    retention_days: int,
) -> dict:
    needed_gb = estimate_archive_storage_gb(station_count, retention_days)
    max_days = 0.0
    max_stations = 0
    if station_count > 0:
        max_days = round(disk_total_gb * 1024 / (station_count * 24 * 57.6), 1)
    if retention_days > 0:
        max_stations = int(disk_total_gb * 1024 / (retention_days * 24 * 57.6))
    return {
        "needed_gb": needed_gb,
        "fits": needed_gb <= disk_total_gb if disk_total_gb > 0 else None,
        "headroom_gb": round(disk_total_gb - needed_gb, 1) if disk_total_gb > 0 else None,
        "max_retention_days": max_days,
        "max_stations": max_stations,
    }


def verify_persistent_storage() -> None:
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    marker = RECORDINGS_DIR / ".volume_write_test"
    try:
        marker.write_text("ok", encoding="utf-8")
        marker.unlink(missing_ok=True)
    except OSError as exc:
        logger.error("Cannot write to recordings dir %s: %s", RECORDINGS_DIR, exc)

    status = get_storage_status()
    if status["on_railway"] and not status["volume_mount"]:
        logger.error(
            "CRITICAL: RAILWAY_VOLUME_MOUNT_PATH is not set. "
            "Stations and recordings will be LOST on every deploy. "
            "Attach a volume to this service (mount path /app/recordings)."
        )
    elif status["volume_mount"]:
        logger.info("Persistent volume mounted at %s", status["volume_mount"])

    logger.info(
        "Storage: db=%s (%s KB), backup=%s (%s zenders in backup)",
        DATABASE_PATH,
        status["database_size_kb"],
        STATIONS_BACKUP_PATH,
        status["backup_station_count"],
    )


def migrate_database_location() -> None:
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    if DATABASE_PATH.exists() or not LEGACY_DATABASE_PATH.exists():
        return
    shutil.copy2(LEGACY_DATABASE_PATH, DATABASE_PATH)
    logger.info("Migrated database to persistent volume: %s", DATABASE_PATH)


def migrate_recording_schema() -> None:
    columns = {
        "peaks_file": "TEXT",
    }

    with engine.connect() as conn:
        existing = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(recording)")).fetchall()
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE recording ADD COLUMN {name} {definition}"))
        conn.commit()


def init_db() -> None:
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    TRIMMED_DIR.mkdir(parents=True, exist_ok=True)
    LOGOS_DIR.mkdir(parents=True, exist_ok=True)

    verify_persistent_storage()
    migrate_database_location()
    SQLModel.metadata.create_all(engine)

    from app.stations import (
        ensure_stations_backup_exists,
        migrate_station_schema,
        reconcile_logos,
        restore_stations_from_backup_if_needed,
    )

    migrate_station_schema()
    migrate_recording_schema()
    restored = restore_stations_from_backup_if_needed()
    ensure_stations_backup_exists()
    reconcile_logos()

    with Session(engine) as session:
        station_count = len(session.exec(select(Station)).all())

    logger.info("Database path: %s (exists=%s)", DATABASE_PATH, DATABASE_PATH.exists())
    logger.info(
        "Stations in database: %d%s",
        station_count,
        " (restored from backup)" if restored else "",
    )


def get_session():
    with Session(engine) as session:
        yield session


def get_recording_by_id(session: Session, recording_id: int) -> Recording | None:
    return session.get(Recording, recording_id)


def get_recording_for_hour(
    session: Session,
    station_id: str,
    start_time: datetime,
) -> Recording | None:
    return session.exec(
        select(Recording).where(
            Recording.station_id == station_id,
            Recording.start_time == start_time,
        )
    ).first()


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
        target = date.fromisoformat(date_filter)
        recordings = [r for r in recordings if r.start_time.date() == target]

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
