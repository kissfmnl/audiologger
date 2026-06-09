import logging
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from app.database import LOGS_DIR, RECORDINGS_DIR, engine
from app.dropbox_upload import ensure_dropbox_upload_async
from app.galio import ensure_galio_async
from app.peaks import ensure_peaks_async, get_audio_duration, peaks_cache_path
from app.models import Recording

logger = logging.getLogger(__name__)

RECORDING_DURATION_SECONDS = 3600
MP3_BITRATE = "128k"
BYTES_PER_SECOND_128K = 16000
RECORDING_USER_AGENT = "Mozilla/5.0 (compatible; AudioLogger/1.0)"
# 20+ zenders loggen elk heel uur tegelijk — elk slot blijft ~60 min bezet.
MAX_CONCURRENT_RECORDINGS = max(4, int(os.environ.get("MAX_CONCURRENT_RECORDINGS", "24")))
_recording_slots = threading.BoundedSemaphore(MAX_CONCURRENT_RECORDINGS)

MIN_SEGMENT_BYTES = 50_000
MIN_PLAYABLE_SECONDS = 300
MIN_COMPLETED_BYTES = 2 * 1024 * 1024


def sanitize_name(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name).strip("_")


def build_filename(station: dict, start_time: datetime) -> str:
    country = station["country"]
    name = sanitize_name(station["name"])
    return f"{country}_{name}_{start_time.strftime('%Y-%m-%d')}_{start_time.strftime('%H00')}.mp3"


def build_output_path(station: dict, start_time: datetime) -> Path:
    return RECORDINGS_DIR / build_filename(station, start_time)


def get_file_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    return round(path.stat().st_size / (1024 * 1024), 2)


def partial_size_mb(output_path: Path) -> float:
    parts_dir = output_path.parent / f".{output_path.stem}_parts"
    if not parts_dir.exists():
        return 0.0
    total = sum(part.stat().st_size for part in parts_dir.glob("part*.mp3") if part.exists())
    return round(total / (1024 * 1024), 2)


def _hour_window(station: dict, start_time: datetime) -> tuple[datetime, datetime, ZoneInfo]:
    tz = ZoneInfo(station.get("timezone", "Europe/Amsterdam"))
    hour_start = start_time.replace(tzinfo=tz) if start_time.tzinfo is None else start_time.astimezone(tz)
    return hour_start, hour_start + timedelta(hours=1), tz


def recording_file_is_valid(path: Path, min_seconds: float = MIN_PLAYABLE_SECONDS) -> bool:
    if not path.exists() or path.stat().st_size < MIN_COMPLETED_BYTES:
        return False
    duration = get_audio_duration(path)
    return duration >= min_seconds


def recording_is_playable(
    path: Path,
    duration_seconds: int | None = None,
    min_seconds: float = MIN_PLAYABLE_SECONDS,
) -> bool:
    """Snelle playable-check voor pagina's — geen ffprobe tenzij nodig."""
    if not path.exists() or path.stat().st_size < MIN_COMPLETED_BYTES:
        return False
    if duration_seconds and duration_seconds >= min_seconds:
        return True
    return recording_file_is_valid(path, min_seconds)


def min_required_seconds(station: dict, start_time: datetime, record_started_at: datetime) -> float:
    _, hour_end, tz = _hour_window(station, start_time)
    if record_started_at.tzinfo is None:
        record_started_at = record_started_at.replace(tzinfo=tz)
    else:
        record_started_at = record_started_at.astimezone(tz)
    available = max(0.0, (hour_end - record_started_at).total_seconds())
    if available <= 0:
        return MIN_PLAYABLE_SECONDS
    return max(60.0, min(MIN_PLAYABLE_SECONDS, available * 0.85))


def is_hour_actively_recording(
    station: dict,
    start_time: datetime,
    now: datetime | None = None,
) -> bool:
    """True only while ffmpeg is plausibly still capturing this hour (not stale part dirs)."""
    _, hour_end, tz = _hour_window(station, start_time)
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    output_path = build_output_path(station, start_time.replace(tzinfo=None))
    log_path = LOGS_DIR / f"{output_path.stem}.log"
    log_recent = log_path.exists() and time.time() - log_path.stat().st_mtime < 90

    if now < hour_end + timedelta(minutes=2):
        if log_recent:
            return True
        parts_dir = output_path.parent / f".{output_path.stem}_parts"
        if parts_dir.is_dir():
            for part in parts_dir.glob("part*.mp3"):
                if part.stat().st_size > 0:
                    return True
        return False

    return log_recent and now < hour_end + timedelta(minutes=5)


def get_partial_path_for_hour(station: dict, start_time: datetime) -> Path | None:
    output_path = build_output_path(station, start_time)
    if output_path.exists() and output_path.stat().st_size > 0:
        return output_path

    parts_dir = output_path.parent / f".{output_path.stem}_parts"
    if not parts_dir.exists():
        return None

    parts = sorted(parts_dir.glob("part*.mp3"), key=lambda path: path.stat().st_mtime)
    for part in reversed(parts):
        if part.stat().st_size > 0:
            return part
    return None


def get_partial_recording_path(recording: Recording) -> Path | None:
    output_path = Path(recording.file_path)
    if output_path.exists() and output_path.stat().st_size > 0:
        return output_path

    parts_dir = output_path.parent / f".{output_path.stem}_parts"
    if not parts_dir.exists():
        return None

    parts = sorted(parts_dir.glob("part*.mp3"), key=lambda path: path.stat().st_mtime)
    for part in reversed(parts):
        if part.stat().st_size > 0:
            return part
    return None


def has_completed_recording(station_id: str, start_time: datetime) -> bool:
    with Session(engine) as session:
        existing = session.exec(
            select(Recording).where(
                Recording.station_id == station_id,
                Recording.start_time == start_time,
                Recording.status == "completed",
            )
        ).first()
        if not existing:
            return False
        return recording_file_is_valid(Path(existing.file_path))


def _ffmpeg_input_args(url: str) -> list[str]:
    return [
        "-re",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
        "-rw_timeout",
        "15000000",
        "-user_agent",
        RECORDING_USER_AGENT,
        "-i",
        url,
    ]


def run_ffmpeg_record(
    url: str,
    output_path: Path,
    log_path: Path,
    duration_seconds: int = RECORDING_DURATION_SECONDS,
) -> tuple[bool, str]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",
        *_ffmpeg_input_args(url),
        "-t",
        str(duration_seconds),
        "-vn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        MP3_BITRATE,
        "-ar",
        "44100",
        "-write_xing",
        "0",
        str(output_path),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=duration_seconds + 120,
        )

        log_content = (
            f"Command: {' '.join(cmd)}\n"
            f"Return code: {result.returncode}\n"
            f"--- STDOUT ---\n{result.stdout}\n"
            f"--- STDERR ---\n{result.stderr}\n"
        )
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"\n--- segment {time.strftime('%H:%M:%S')} ---\n{log_content}")

        if result.returncode != 0:
            error = (result.stderr or result.stdout or "").strip()
            return False, error[-500:] if error else "ffmpeg returned non-zero exit code"

        if not output_path.exists() or output_path.stat().st_size == 0:
            return False, "Output file missing or empty after recording"

        return True, ""

    except subprocess.TimeoutExpired:
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"\n--- segment {time.strftime('%H:%M:%S')} ---\nRecording timed out.\n")
        return False, "Recording timed out"
    except FileNotFoundError:
        return False, "ffmpeg not found on system PATH"
    except OSError as exc:
        return False, str(exc)


def cap_recording_duration(
    path: Path,
    max_seconds: float = RECORDING_DURATION_SECONDS,
) -> float:
    """Trim MP3 to max_seconds when capture ran over the hour budget."""
    duration = get_audio_duration(path)
    if duration <= max_seconds + 1.5:
        return duration

    temp_path = path.with_suffix(".trim.mp3")
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-i",
        str(path),
        "-t",
        str(max_seconds),
        "-c",
        "copy",
        "-y",
        str(temp_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0 or not temp_path.exists():
            logger.warning(
                "Could not trim %s to %ss: %s",
                path.name,
                max_seconds,
                (result.stderr or result.stdout)[-300:],
            )
            return duration
        shutil.move(str(temp_path), str(path))
        return get_audio_duration(path)
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Trim failed for %s: %s", path.name, exc)
        temp_path.unlink(missing_ok=True)
        return duration


def concat_mp3_segments(segments: list[Path], output_path: Path) -> bool:
    if not segments:
        return False

    if len(segments) == 1:
        shutil.move(str(segments[0]), str(output_path))
        return output_path.exists() and output_path.stat().st_size > 0

    list_path = output_path.with_suffix(".concat.txt")
    list_path.write_text(
        "\n".join(f"file '{segment.resolve()}'" for segment in segments),
        encoding="utf-8",
    )
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-c",
        "copy",
        "-y",
        str(output_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        list_path.unlink(missing_ok=True)
        if result.returncode != 0:
            logger.error("Concat failed: %s", (result.stderr or result.stdout)[-500:])
            return False
        return output_path.exists() and output_path.stat().st_size > 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.error("Concat failed: %s", exc)
        list_path.unlink(missing_ok=True)
        return False


def save_recording_to_db(
    session: Session,
    station: dict,
    start_time: datetime,
    output_path: Path,
    status: str,
    error_message: str | None = None,
    actual_duration: int | None = None,
) -> Recording:
    end_time = start_time + timedelta(seconds=RECORDING_DURATION_SECONDS)
    duration = actual_duration if actual_duration is not None else (
        RECORDING_DURATION_SECONDS if status == "completed" else 0
    )
    if status == "completed":
        file_size = get_file_size_mb(output_path)
    elif status == "recording":
        file_size = partial_size_mb(output_path)
    else:
        file_size = 0.0

    existing = session.exec(
        select(Recording).where(
            Recording.station_id == station["id"],
            Recording.start_time == start_time,
        )
    ).first()

    peaks_file = None
    if status == "completed" and output_path.exists():
        peaks_file = peaks_cache_path(output_path).name

    if existing:
        existing.station_name = station["name"]
        existing.country = station["country"]
        existing.end_time = end_time
        existing.duration_seconds = duration
        existing.file_path = str(output_path)
        existing.file_size_mb = file_size
        existing.status = status
        if peaks_file:
            existing.peaks_file = peaks_file
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing

    recording = Recording(
        station_id=station["id"],
        station_name=station["name"],
        country=station["country"],
        start_time=start_time,
        end_time=end_time,
        duration_seconds=duration,
        file_path=str(output_path),
        file_size_mb=file_size,
        status=status,
        peaks_file=peaks_file,
    )
    session.add(recording)
    session.commit()
    session.refresh(recording)
    if error_message:
        logger.warning(
            "Recording failed for %s at %s: %s",
            station["id"],
            start_time.isoformat(),
            error_message,
        )
    return recording


def record_station(station: dict, start_time: datetime | None = None) -> Recording:
    tz = ZoneInfo(station.get("timezone", "Europe/Amsterdam"))

    if start_time is None:
        now = datetime.now(tz)
        start_time = now.replace(minute=0, second=0, microsecond=0, tzinfo=None)

    target_hour = start_time.replace(tzinfo=None)

    if has_completed_recording(station["id"], target_hour):
        with Session(engine) as session:
            return session.exec(
                select(Recording).where(
                    Recording.station_id == station["id"],
                    Recording.start_time == target_hour,
                    Recording.status == "completed",
                )
            ).one()

    logger.info(
        "Waiting for recording slot (%s at %s, max %s concurrent)",
        station["id"],
        target_hour.strftime("%Y-%m-%d %H:%M"),
        MAX_CONCURRENT_RECORDINGS,
    )
    _recording_slots.acquire()
    try:
        return _record_station_locked(station, target_hour, tz)
    finally:
        _recording_slots.release()


def _record_station_locked(station: dict, start_time: datetime, tz: ZoneInfo) -> Recording:
    hour_start = start_time.replace(tzinfo=tz)
    hour_end = hour_start + timedelta(hours=1)
    record_started_at = datetime.now(tz)
    min_seconds = min_required_seconds(station, start_time, record_started_at)

    if record_started_at >= hour_end:
        logger.error(
            "Recording slot acquired too late for %s hour %s (now %s)",
            station["id"],
            start_time.strftime("%H:%M"),
            record_started_at.strftime("%H:%M:%S"),
        )

    output_path = build_output_path(station, start_time)
    log_path = LOGS_DIR / f"{output_path.stem}.log"
    log_path.write_text(
        f"Recording hour {start_time.isoformat()} (slot acquired {record_started_at.isoformat()})\n",
        encoding="utf-8",
    )

    parts_dir = output_path.parent / f".{output_path.stem}_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    segments: list[Path] = []
    segment_idx = 0
    last_error = ""

    with Session(engine) as session:
        save_recording_to_db(session, station, start_time, output_path, "recording")

    try:
        captured_seconds = 0.0
        max_capture_seconds = min(
            RECORDING_DURATION_SECONDS,
            max(0.0, (hour_end - record_started_at).total_seconds()),
        )

        while (
            captured_seconds < max_capture_seconds - 0.5
            and datetime.now(tz) < hour_end
        ):
            remaining_audio = max_capture_seconds - captured_seconds
            remaining_wall = (hour_end - datetime.now(tz)).total_seconds()
            segment_duration = int(min(remaining_audio, remaining_wall))
            if segment_duration < 1:
                break

            segment_path = parts_dir / f"part{segment_idx:04d}.mp3"
            success, last_error = run_ffmpeg_record(
                station["url"],
                segment_path,
                log_path,
                duration_seconds=segment_duration,
            )

            if segment_path.exists() and segment_path.stat().st_size >= MIN_SEGMENT_BYTES:
                segment_seconds = get_audio_duration(segment_path)
                segments.append(segment_path)
                segment_idx += 1
                captured_seconds += segment_seconds
                with Session(engine) as session:
                    save_recording_to_db(session, station, start_time, output_path, "recording")
            elif segment_path.exists() and segment_path.stat().st_size == 0:
                segment_path.unlink(missing_ok=True)

            # Stream kan na enkele seconden stoppen — doorloggen tot budget of uur-einde.
            if not success:
                time.sleep(2)

        concat_ok = bool(segments) and concat_mp3_segments(segments, output_path)
        if concat_ok and output_path.exists():
            cap_recording_duration(output_path, max_capture_seconds)
        valid = concat_ok and recording_file_is_valid(output_path, min_seconds)
        status = "completed" if valid else "failed"

        if not valid and output_path.exists():
            output_path.unlink(missing_ok=True)

        actual_duration = 0
        if valid:
            actual_duration = int(get_audio_duration(output_path))

        with Session(engine) as session:
            recording = save_recording_to_db(
                session,
                station,
                start_time,
                output_path,
                status,
                error_message=None if valid else last_error or "Opname te kort of leeg",
                actual_duration=actual_duration if valid else 0,
            )

        if not valid:
            raise RuntimeError(
                f"Recording failed for {station['id']} at {start_time.isoformat()}: {last_error or 'too short'}"
            )

        ensure_peaks_async(output_path)
        ensure_galio_async(output_path)
        ensure_dropbox_upload_async(
            output_path, station, recording.id if recording else None, start_time
        )
        return recording
    finally:
        shutil.rmtree(parts_dir, ignore_errors=True)


def finalize_stale_recording(
    session: Session,
    station: dict,
    recording: Recording,
) -> Recording:
    """Close out recordings left in 'recording' after crashes, deploys, or hung jobs."""
    if recording.status != "recording":
        return recording

    start_time = recording.start_time
    _, hour_end, tz = _hour_window(station, start_time)
    now = datetime.now(tz)

    if is_hour_actively_recording(station, start_time, now):
        return recording

    output_path = Path(recording.file_path)
    parts_dir = output_path.parent / f".{output_path.stem}_parts"
    min_seconds = MIN_PLAYABLE_SECONDS

    if output_path.exists() and recording_file_is_valid(output_path, min_seconds):
        duration = int(cap_recording_duration(output_path, RECORDING_DURATION_SECONDS))
        updated = save_recording_to_db(
            session, station, start_time, output_path, "completed", actual_duration=duration
        )
        ensure_peaks_async(output_path)
        ensure_galio_async(output_path)
        ensure_dropbox_upload_async(
            output_path, station, updated.id if updated else None, start_time
        )
        logger.info("Finalized stale recording as completed: %s %s", station["id"], start_time)
        return updated

    segments = []
    if parts_dir.is_dir():
        segments = [
            part for part in sorted(parts_dir.glob("part*.mp3"))
            if part.stat().st_size >= MIN_SEGMENT_BYTES
        ]

    if segments and concat_mp3_segments(segments, output_path):
        cap_recording_duration(output_path, RECORDING_DURATION_SECONDS)
        if recording_file_is_valid(output_path, min_seconds):
            duration = int(get_audio_duration(output_path))
            updated = save_recording_to_db(
                session, station, start_time, output_path, "completed", actual_duration=duration
            )
            ensure_peaks_async(output_path)
            ensure_galio_async(output_path)
            ensure_dropbox_upload_async(
                output_path, station, updated.id if updated else None, start_time
            )
            shutil.rmtree(parts_dir, ignore_errors=True)
            logger.info("Salvaged stale recording from parts: %s %s", station["id"], start_time)
            return updated

    updated = save_recording_to_db(session, station, start_time, output_path, "failed")
    shutil.rmtree(parts_dir, ignore_errors=True)
    if output_path.exists():
        output_path.unlink(missing_ok=True)
    logger.warning("Marked stale recording as failed: %s %s", station["id"], start_time)
    return updated


def invalidate_short_completed_recordings() -> int:
    """Mark bogus 'completed' rows (2s clips etc.) as failed so retries can run."""
    from app.stations import get_station_by_id

    fixed = 0
    with Session(engine) as session:
        completed = session.exec(select(Recording).where(Recording.status == "completed")).all()
        for recording in completed:
            path = Path(recording.file_path)
            if not path.exists() or path.stat().st_size < MIN_COMPLETED_BYTES:
                station = get_station_by_id(recording.station_id)
                if not station:
                    continue
                save_recording_to_db(session, station, recording.start_time, path, "failed")
                fixed += 1
                continue

            db_duration = recording.duration_seconds or 0
            if db_duration >= MIN_PLAYABLE_SECONDS and db_duration <= RECORDING_DURATION_SECONDS + 45:
                continue

            if recording_file_is_valid(path):
                duration = int(cap_recording_duration(path, RECORDING_DURATION_SECONDS))
                if abs(duration - db_duration) > 30:
                    recording.duration_seconds = duration
                    session.add(recording)
                continue
            station = get_station_by_id(recording.station_id)
            if not station:
                continue
            save_recording_to_db(session, station, recording.start_time, path, "failed")
            fixed += 1
        session.commit()
    if fixed:
        logger.warning("Invalidated %s too-short completed recordings", fixed)
    return fixed


def finalize_all_stale_recordings() -> int:
    from app.stations import get_station_by_id

    finalized = 0
    with Session(engine) as session:
        stale = session.exec(select(Recording).where(Recording.status == "recording")).all()
        for recording in stale:
            station = get_station_by_id(recording.station_id)
            if not station:
                continue
            before = recording.status
            finalize_stale_recording(session, station, recording)
            session.refresh(recording)
            if recording.status != before:
                finalized += 1
    return finalized
