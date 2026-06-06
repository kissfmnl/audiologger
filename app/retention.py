import logging
from datetime import datetime, timedelta
from pathlib import Path

from sqlmodel import Session, select

from app.database import LOGS_DIR, engine
from app.models import Recording, Station
from app.stations import retention_days_for_station

logger = logging.getLogger(__name__)


def _delete_recording_files(recording: Recording) -> None:
    file_path = Path(recording.file_path)
    if file_path.exists():
        file_path.unlink(missing_ok=True)

    log_path = LOGS_DIR / f"{file_path.stem}.log"
    if log_path.exists():
        log_path.unlink(missing_ok=True)


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

            _delete_recording_files(recording)
            session.delete(recording)
            deleted += 1

        if deleted:
            session.commit()

    if deleted:
        logger.info("Retention cleanup: %d opname(s) verwijderd", deleted)

    return {"deleted": deleted}
