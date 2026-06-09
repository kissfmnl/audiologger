import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from app.database import engine
from app.dropbox_accounts import (
    dropbox_configured,
    load_dropbox_accounts,
    resolve_station_account,
)
from app.models import Recording

logger = logging.getLogger(__name__)

UPLOAD_AFTER_HOUR_SECONDS = 60


def should_archive_station(station: dict) -> bool:
    return resolve_station_account(station) is not None


def build_dropbox_remote_path(root: str, station: dict, filename: str) -> str:
    country = (station.get("country") or "NL").upper()
    station_id = station.get("id") or "unknown"
    root = root.rstrip("/") or "/AudioLogger"
    return f"{root}/{country}/{station_id}/{filename}"


def seconds_until_upload_allowed(station: dict, start_time: datetime | None) -> float:
    if not start_time:
        return 0
    tz = ZoneInfo(station.get("timezone", "Europe/Amsterdam"))
    hour_start = start_time.replace(tzinfo=tz) if start_time.tzinfo is None else start_time.astimezone(tz)
    upload_after = hour_start + timedelta(hours=1, seconds=UPLOAD_AFTER_HOUR_SECONDS)
    return max(0.0, (upload_after - datetime.now(tz)).total_seconds())


def upload_recording(file_path: Path, station: dict, recording_id: int | None = None) -> str | None:
    resolved = resolve_station_account(station)
    if not resolved:
        return None

    account_id, account = resolved
    if not file_path.exists() or file_path.stat().st_size < 1024:
        logger.warning("Dropbox skip (missing/empty file): %s", file_path.name)
        return None

    remote_path = build_dropbox_remote_path(account["root"], station, file_path.name)

    try:
        import dropbox
        from dropbox.files import WriteMode

        dbx = dropbox.Dropbox(account["token"])
        with file_path.open("rb") as handle:
            dbx.files_upload(
                handle.read(),
                remote_path,
                mode=WriteMode.overwrite,
                mute=True,
            )
    except Exception:
        logger.exception(
            "Dropbox upload failed (%s) for %s -> %s",
            account_id,
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

    logger.info(
        "Dropbox upload OK (%s / %s): %s -> %s",
        account_id,
        account["label"],
        file_path.name,
        remote_path,
    )
    return remote_path


def _upload_when_hour_ready(
    file_path: Path,
    station: dict,
    recording_id: int | None,
    start_time: datetime | None,
) -> None:
    wait = seconds_until_upload_allowed(station, start_time)
    if wait > 0:
        logger.info(
            "Dropbox upload for %s wacht %.0fs tot na uur-einde",
            file_path.name,
            wait,
        )
        time.sleep(wait)
    upload_recording(file_path, station, recording_id)


def ensure_dropbox_upload_async(
    file_path: Path,
    station: dict,
    recording_id: int | None = None,
    start_time: datetime | None = None,
) -> None:
    if not should_archive_station(station):
        return
    threading.Thread(
        target=_upload_when_hour_ready,
        args=(file_path, station, recording_id, start_time),
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
            if not station or not should_archive_station(station):
                continue
            attempted += 1
            if upload_recording(path, station, recording.id):
                uploaded += 1

    if attempted:
        logger.info("Dropbox retry: %d attempted, %d uploaded", attempted, uploaded)
    return {"attempted": attempted, "uploaded": uploaded}
