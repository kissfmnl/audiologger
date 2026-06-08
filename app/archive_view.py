import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlmodel import Session

logger = logging.getLogger(__name__)

from app.database import get_recording_for_hour, get_recordings
from app.peaks import estimate_duration
from app.recorder import finalize_stale_recording, get_partial_path_for_hour, is_hour_actively_recording, recording_is_playable
from app.stations import should_record_station


def build_hour_slots(
    station: dict,
    selected_date: str,
    session: Session,
    now: datetime | None = None,
) -> list[dict]:
    tz = ZoneInfo(station.get("timezone", "Europe/Amsterdam"))
    day = date.fromisoformat(selected_date)
    now = now or datetime.now(tz)

    recordings_by_hour: dict[int, object] = {}
    for recording in get_recordings(session, station_id=station["id"], date_filter=selected_date):
        if recording.status == "recording":
            try:
                recording = finalize_stale_recording(session, station, recording)
            except Exception:
                logger.exception("Could not finalize recording %s", recording.id)
        recordings_by_hour[recording.start_time.hour] = recording

    slots = []
    for hour in range(24):
        hour_start = datetime(day.year, day.month, day.day, hour, 0, 0)
        slot_moment = hour_start.replace(tzinfo=tz)
        recording = recordings_by_hour.get(hour)
        active = is_hour_actively_recording(station, hour_start, now)

        if recording:
            status = recording.status
            if status == "recording":
                hour_end = slot_moment + timedelta(hours=1)
                if now > hour_end + timedelta(minutes=3) and not active:
                    status = "failed" if recording.file_size_mb else "missing"
        elif active:
            status = "recording"
            recording = get_recording_for_hour(session, station["id"], hour_start)
        elif slot_moment > now:
            status = "future"
        elif day < now.date() or (day == now.date() and hour < now.hour):
            status = "missing"
        elif day == now.date() and hour == now.hour and should_record_station(station):
            status = "pending"
        else:
            status = "missing"

        playable = False
        audio_url = ""
        download_url = ""
        peaks_url = ""
        recording_id = None
        duration_seconds = 3600
        if status == "completed" and recording:
            path = Path(recording.file_path)
            if recording_is_playable(path, recording.duration_seconds):
                playable = True
                filename = recording.file_path.split("/")[-1]
                audio_url = f"/recordings/{filename}"
                download_url = audio_url
                peaks_url = f"/api/peaks/{recording.id}"
                recording_id = recording.id
                duration_seconds = recording.duration_seconds or int(estimate_duration(path))
            else:
                status = "missing"
        elif status == "recording":
            partial = get_partial_path_for_hour(station, hour_start)
            playable = partial is not None and partial.exists() and partial.stat().st_size > 0
            if playable:
                duration_seconds = int(estimate_duration(partial))
                if recording:
                    audio_url = f"/recordings/live/{recording.id}"
                    download_url = audio_url
                    peaks_url = f"/api/peaks/{recording.id}"
                    recording_id = recording.id
                else:
                    audio_url = (
                        f"/recordings/live-hour/{station['id']}"
                        f"?date={selected_date}&hour={hour}"
                    )
                    download_url = audio_url
                    peaks_url = (
                        f"/api/peaks/hour/{station['id']}"
                        f"?date={selected_date}&hour={hour}"
                    )

        slots.append(
            {
                "hour": hour,
                "label": f"{hour:02d}:00:00",
                "recording": recording,
                "status": status,
                "playable": playable,
                "audio_url": audio_url,
                "download_url": download_url,
                "peaks_url": peaks_url,
                "recording_id": recording_id,
                "duration_seconds": duration_seconds,
            }
        )

    return slots
