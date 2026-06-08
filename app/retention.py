import logging
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from app.database import LOGS_DIR, RECORDINGS_DIR, engine
from app.galio import galio_cache_path
from app.models import Recording, Station
from app.peaks import peaks_cache_path
from app.stations import retention_days_for_station

logger = logging.getLogger(__name__)


def delete_recording_files(recording: Recording) -> None:
    file_path = Path(recording.file_path)
    if not file_path.is_absolute():
        file_path = RECORDINGS_DIR / file_path.name

    for path in (
        file_path,
        peaks_cache_path(file_path),
        galio_cache_path(file_path),
        LOGS_DIR / f"{file_path.stem}.log",
    ):
        if path.exists():
            path.unlink(missing_ok=True)

    parts_dir = file_path.parent / f".{file_path.stem}_parts"
    if parts_dir.is_dir():
        shutil.rmtree(parts_dir, ignore_errors=True)


def _recording_file_size_mb(recording: Recording) -> float:
    path = Path(recording.file_path)
    if not path.is_absolute():
        path = RECORDINGS_DIR / path.name
    if path.exists():
        return round(path.stat().st_size / (1024 * 1024), 2)
    return recording.file_size_mb or 0.0


def get_archive_stats() -> dict:
    with Session(engine) as session:
        recordings = list(session.exec(select(Recording)).all())

    total_mb = sum(_recording_file_size_mb(r) for r in recordings)
    oldest = min((r.start_time for r in recordings), default=None)
    newest = max((r.start_time for r in recordings), default=None)

    tz = ZoneInfo("Europe/Amsterdam")
    today = datetime.now(tz).date()
    today_count = sum(1 for r in recordings if r.start_time.date() == today)

    return {
        "total": len(recordings),
        "total_mb": round(total_mb, 1),
        "today_count": today_count,
        "older_than_today": len(recordings) - today_count,
        "oldest": oldest.strftime("%d-%m-%Y %H:%M") if oldest else "—",
        "newest": newest.strftime("%d-%m-%Y %H:%M") if newest else "—",
    }


def _matches_purge(
    recording: Recording,
    *,
    mode: str,
    older_than_days: int | None,
    before_date: date | None,
    today: date,
) -> bool:
    rec_date = recording.start_time.date()
    if mode == "all":
        return True
    if mode == "except_today":
        return rec_date < today
    if mode == "older_than_days" and older_than_days is not None:
        cutoff = datetime.now() - timedelta(days=older_than_days)
        return recording.start_time < cutoff
    if mode == "before_date" and before_date is not None:
        return rec_date < before_date
    return False


def preview_purge(
    mode: str,
    *,
    older_than_days: int | None = None,
    before_date: date | None = None,
) -> dict:
    tz = ZoneInfo("Europe/Amsterdam")
    today = datetime.now(tz).date()

    with Session(engine) as session:
        recordings = list(session.exec(select(Recording)).all())

    matched = [
        r
        for r in recordings
        if _matches_purge(
            r,
            mode=mode,
            older_than_days=older_than_days,
            before_date=before_date,
            today=today,
        )
    ]
    size_mb = sum(_recording_file_size_mb(r) for r in matched)
    return {"count": len(matched), "size_mb": round(size_mb, 1)}


def purge_recordings(
    mode: str,
    *,
    older_than_days: int | None = None,
    before_date: date | None = None,
) -> dict:
    tz = ZoneInfo("Europe/Amsterdam")
    today = datetime.now(tz).date()

    deleted = 0
    freed_mb = 0.0

    with Session(engine) as session:
        recordings = list(session.exec(select(Recording)).all())
        for recording in recordings:
            if not _matches_purge(
                recording,
                mode=mode,
                older_than_days=older_than_days,
                before_date=before_date,
                today=today,
            ):
                continue

            freed_mb += _recording_file_size_mb(recording)
            delete_recording_files(recording)
            session.delete(recording)
            deleted += 1

        if deleted:
            session.commit()

    if deleted:
        logger.info("Manual purge (%s): %d opname(s), %.1f MB freed", mode, deleted, freed_mb)

    return {"deleted": deleted, "freed_mb": round(freed_mb, 1)}


def cleanup_expired_recordings() -> dict:
    """Verwijder opnames ouder dan de bewaartermijn van de zender."""
    now = datetime.now()
    deleted = 0

    with Session(engine) as session:
        stations = {
            station.id: station for station in session.exec(select(Station)).all()
        }
        recordings = list(session.exec(select(Recording)).all())

        for recording in recordings:
            station = stations.get(recording.station_id)
            if not station:
                days = 7
            else:
                days = retention_days_for_station(
                    {
                        "is_event": station.is_event,
                        "retention_days": station.retention_days,
                    }
                )

            cutoff = now - timedelta(days=days)
            if recording.start_time >= cutoff:
                continue

            delete_recording_files(recording)
            session.delete(recording)
            deleted += 1

        if deleted:
            session.commit()

    if deleted:
        logger.info("Retention cleanup: %d opname(s) verwijderd", deleted)

    return {"deleted": deleted}
