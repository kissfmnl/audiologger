import logging
import os
import threading
from pathlib import Path

from sqlmodel import Session, select

from app.database import engine
from app.models import Recording

logger = logging.getLogger(__name__)

DROPBOX_ACCESS_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN", "").strip()
DROPBOX_ROOT_FOLDER = os.getenv("DROPBOX_ROOT_FOLDER", "/AudioLogger").strip().rstrip("/") or "/AudioLogger"


def dropbox_configured() -> bool:
    return bool(DROPBOX_ACCESS_TOKEN)


def should_archive_station(station: dict) -> bool:
    return bool(station.get("dropbox_archive")) and dropbox_configured()


def build_dropbox_remote_path(station: dict, filename: str) -> str:
    country = (station.get("country") or "NL").upper()
    station_id = station.get("id") or "unknown"
    return f"{DROPBOX_ROOT_FOLDER}/{country}/{station_id}/{filename}"


def _get_dropbox_client():
    import dropbox

    return dropbox.Dropbox(DROPBOX_ACCESS_TOKEN)


def upload_recording(file_path: Path, station: dict, recording_id: int | None = None) -> str | None:
    if not should_archive_station(station):
        return None
    if not file_path.exists() or file_path.stat().st_size < 1024:
        logger.warning("Dropbox skip (missing/empty file): %s", file_path.name)
        return None

    remote_path = build_dropbox_remote_path(station, file_path.name)

    try:
        import dropbox
        from dropbox.files import WriteMode

        dbx = _get_dropbox_client()
        with file_path.open("rb") as handle:
            dbx.files_upload(
                handle.read(),
                remote_path,
                mode=WriteMode.overwrite,
                mute=True,
            )
    except Exception:
        logger.exception(
            "Dropbox upload failed for %s -> %s",
            file_path.name,
            remote_path,
        )
        return None

    if recording_id is not None:
        with Session(engine) as session:
            recording = session.get(Recording, recording_id)
            if recording:
                recording.dropbox_path = remote_path
                session.add(recording)
                session.commit()

    logger.info("Dropbox upload OK: %s -> %s", file_path.name, remote_path)
    return remote_path


def ensure_dropbox_upload_async(
    file_path: Path,
    station: dict,
    recording_id: int | None = None,
) -> None:
    if not should_archive_station(station):
        return
    threading.Thread(
        target=upload_recording,
        args=(file_path, station, recording_id),
        daemon=True,
        name=f"dropbox-{file_path.stem}",
    ).start()


def retry_pending_dropbox_uploads() -> dict:
    if not dropbox_configured():
        return {"attempted": 0, "uploaded": 0}

    from app.stations import get_station_by_id, load_stations

    archive_station_ids = {
        station["id"]
        for station in load_stations()
        if station.get("dropbox_archive")
    }
    if not archive_station_ids:
        return {"attempted": 0, "uploaded": 0}

    attempted = 0
    uploaded = 0
    with Session(engine) as session:
        completed = session.exec(
            select(Recording).where(Recording.status == "completed")
        ).all()
        for recording in completed:
            if recording.dropbox_path or recording.station_id not in archive_station_ids:
                continue
            path = Path(recording.file_path)
            if not path.exists():
                continue
            station = get_station_by_id(recording.station_id)
            if not station:
                continue
            attempted += 1
            if upload_recording(path, station, recording.id):
                uploaded += 1

    if attempted:
        logger.info("Dropbox retry: %d attempted, %d uploaded", attempted, uploaded)
    return {"attempted": attempted, "uploaded": uploaded}
