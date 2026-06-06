from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlmodel import Session

from app.database import get_recording_for_hour, get_recordings
from app.peaks import estimate_duration, read_embed_peaks
from app.recorder import get_partial_path_for_hour, is_hour_actively_recording
from app.stations import should_record_station


def _slot_audio_path(
    station: dict,
    hour_start: datetime,
    recording,
    status: str,
) -> Path | None:
    if recording and status == "completed":
        path = Path(recording.file_path)
        return path if path.exists() else None
    if status == "recording":
        return get_partial_path_for_hour(station, hour_start)
    return None


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
        recordings_by_hour[recording.start_time.hour] = recording

    slots = []
    for hour in range(24):
        hour_start = datetime(day.year, day.month, day.day, hour, 0, 0)
        slot_moment = hour_start.replace(tzinfo=tz)
        recording = recordings_by_hour.get(hour)
        active = is_hour_actively_recording(station, hour_start)

        if recording:
            status = recording.status
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

        progress_label = ""
        if status == "recording":
            elapsed = max(0, int((now - slot_moment).total_seconds()))
            progress_label = f"{max(1, elapsed // 60)} min"

        playable = False
        audio_url = ""
        download_url = ""
        peaks_url = ""
        recording_id = None
        duration_seconds = 3600
        wire_peaks = None

        if status == "completed" and recording:
            playable = True
            filename = recording.file_path.split("/")[-1]
            audio_url = f"/recordings/{filename}"
            download_url = audio_url
            peaks_url = f"/api/peaks/{recording.id}"
            recording_id = recording.id
            duration_seconds = recording.duration_seconds or 3600
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

        if playable:
            audio_path = _slot_audio_path(station, hour_start, recording, status)
            wire_peaks = read_embed_peaks(audio_path)

        slots.append(
            {
                "hour": hour,
                "label": f"{hour:02d}:00:00",
                "recording": recording,
                "status": status,
                "progress_label": progress_label,
                "playable": playable,
                "audio_url": audio_url,
                "download_url": download_url,
                "peaks_url": peaks_url,
                "recording_id": recording_id,
                "duration_seconds": duration_seconds,
                "wire_peaks": wire_peaks,
            }
        )

    return slots
