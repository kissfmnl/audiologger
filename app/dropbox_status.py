from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pathlib import Path

from sqlmodel import Session, select

from app.database import engine
from app.dropbox_accounts import load_dropbox_accounts, resolve_station_account
from app.dropbox_upload import build_dropbox_remote_path
from app.models import Recording
from app.recorder import build_filename
from app.stations import load_stations


def describe_dropbox_path_pattern(station: dict, account_root: str) -> str:
    country = (station.get("country") or "NL").upper()
    station_id = station.get("id") or "unknown"
    root = account_root.rstrip("/") or "/AudioLogger"
    return f"{root}/{country}/{station_id}/{{bestandsnaam}}.mp3"


def example_dropbox_path(station: dict, account_root: str, moment: datetime | None = None) -> str:
    tz = ZoneInfo(station.get("timezone", "Europe/Amsterdam"))
    now = moment or datetime.now(tz)
    hour_start = now.replace(minute=0, second=0, microsecond=0, tzinfo=None)
    filename = build_filename(station, hour_start)
    return build_dropbox_remote_path(account_root, station, filename)


def get_dropbox_archive_checklist() -> dict:
    stations = load_stations()
    accounts = load_dropbox_accounts()
    archived: list[dict] = []
    issues: list[dict] = []
    pending_uploads = 0

    with Session(engine) as session:
        for station in stations:
            if not station.get("dropbox_archive"):
                continue

            resolved = resolve_station_account(station)
            ready = resolved is not None
            account_id = resolved[0] if resolved else None
            account_label = resolved[1]["label"] if resolved else None
            account_root = resolved[1]["root"] if resolved else "/AudioLogger"

            last_uploaded = session.exec(
                select(Recording)
                .where(
                    Recording.station_id == station["id"],
                    Recording.status == "completed",
                    Recording.dropbox_path != None,  # noqa: E711
                )
                .order_by(Recording.start_time.desc())
            ).first()

            pending = session.exec(
                select(Recording).where(
                    Recording.station_id == station["id"],
                    Recording.status == "completed",
                    Recording.dropbox_path == None,  # noqa: E711
                )
            ).all()
            pending_count = sum(1 for rec in pending if rec.file_path)
            pending_uploads += pending_count

            item = {
                "id": station["id"],
                "name": station["name"],
                "flag": station.get("flag", "📻"),
                "ready": ready,
                "account_id": account_id,
                "account_label": account_label or "—",
                "path_pattern": describe_dropbox_path_pattern(station, account_root),
                "path_example": example_dropbox_path(station, account_root) if ready else None,
                "pending_count": pending_count,
                "last_upload_label": None,
                "last_upload_path": None,
            }

            if last_uploaded:
                item["last_upload_label"] = (
                    f"{last_uploaded.start_time.strftime('%d-%m-%Y %H:%M')} "
                    f"({int(last_uploaded.duration_seconds // 60)} min)"
                )
                item["last_upload_path"] = last_uploaded.dropbox_path

            if ready:
                archived.append(item)
            else:
                issues.append(item)

    return {
        "archived": archived,
        "issues": issues,
        "archived_count": len(archived),
        "issue_count": len(issues),
        "total_stations": len(stations),
        "inactive_count": len(stations) - len(archived) - len(issues),
        "pending_uploads": pending_uploads,
        "account_count": len(accounts),
        "upload_policy": "Na afloop van elk uur (:00 + ~1 min), zodra de opname compleet is",
    }


def upload_previous_hour_recordings() -> dict:
    """Upload completed recordings from the hour that just ended (runs shortly after :00)."""
    from app.dropbox_upload import upload_recording
    from app.stations import get_station_by_id

    attempted = 0
    uploaded = 0
    now = datetime.now(ZoneInfo("Europe/Amsterdam"))

    for station in load_stations():
        if not station.get("dropbox_archive"):
            continue
        if not resolve_station_account(station):
            continue

        tz = ZoneInfo(station.get("timezone", "Europe/Amsterdam"))
        local_now = now.astimezone(tz)
        if local_now.minute > 12:
            continue

        previous_hour = (local_now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)).replace(
            tzinfo=None
        )

        with Session(engine) as session:
            recording = session.exec(
                select(Recording).where(
                    Recording.station_id == station["id"],
                    Recording.start_time == previous_hour,
                    Recording.status == "completed",
                )
            ).first()
            if not recording or recording.dropbox_path:
                continue

            path = Path(recording.file_path)
            if not path.exists():
                continue

            fresh_station = get_station_by_id(station["id"]) or station
            attempted += 1
            if upload_recording(path, fresh_station, recording.id):
                uploaded += 1

    if attempted:
        from logging import getLogger

        getLogger(__name__).info(
            "Previous-hour Dropbox sync: %d attempted, %d uploaded",
            attempted,
            uploaded,
        )
    return {"attempted": attempted, "uploaded": uploaded}
