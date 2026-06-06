import logging
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from sqlmodel import Session, select

from app.database import LOGS_DIR, RECORDINGS_DIR, engine
from app.models import Recording

logger = logging.getLogger(__name__)

RECORDING_DURATION_SECONDS = 3600
MP3_BITRATE = "128k"
RECORDING_USER_AGENT = "Mozilla/5.0 (compatible; AudioLogger/1.0)"
ATTEMPT_DELAYS_SECONDS = [0, 20, 60, 120]


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


def has_completed_recording(station_id: str, start_time: datetime) -> bool:
    with Session(engine) as session:
        existing = session.exec(
            select(Recording).where(
                Recording.station_id == station_id,
                Recording.start_time == start_time,
                Recording.status == "completed",
            )
        ).first()
        return existing is not None


def _ffmpeg_input_args(url: str) -> list[str]:
    return [
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "10",
        "-rw_timeout",
        "15000000",
        "-user_agent",
        RECORDING_USER_AGENT,
        "-i",
        url,
    ]


def run_ffmpeg_record(url: str, output_path: Path, log_path: Path) -> tuple[bool, str]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",
        *_ffmpeg_input_args(url),
        "-t",
        str(RECORDING_DURATION_SECONDS),
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
            timeout=RECORDING_DURATION_SECONDS + 180,
        )

        log_content = (
            f"Command: {' '.join(cmd)}\n"
            f"Return code: {result.returncode}\n"
            f"--- STDOUT ---\n{result.stdout}\n"
            f"--- STDERR ---\n{result.stderr}\n"
        )
        log_path.write_text(log_content, encoding="utf-8")

        if result.returncode != 0:
            error = (result.stderr or result.stdout or "").strip()
            return False, error[-500:] if error else "ffmpeg returned non-zero exit code"

        if not output_path.exists() or output_path.stat().st_size == 0:
            return False, "Output file missing or empty after recording"

        return True, ""

    except subprocess.TimeoutExpired:
        log_path.write_text(
            f"Command: {' '.join(cmd)}\nRecording timed out.\n",
            encoding="utf-8",
        )
        return False, "Recording timed out"
    except FileNotFoundError:
        return False, "ffmpeg not found on system PATH"
    except OSError as exc:
        return False, str(exc)


def save_recording_to_db(
    session: Session,
    station: dict,
    start_time: datetime,
    output_path: Path,
    status: str,
    error_message: str | None = None,
) -> Recording:
    end_time = start_time + timedelta(seconds=RECORDING_DURATION_SECONDS)
    duration = RECORDING_DURATION_SECONDS if status == "completed" else 0
    file_size = get_file_size_mb(output_path) if status == "completed" else 0.0

    existing = session.exec(
        select(Recording).where(
            Recording.station_id == station["id"],
            Recording.start_time == start_time,
        )
    ).first()

    if existing:
        existing.station_name = station["name"]
        existing.country = station["country"]
        existing.end_time = end_time
        existing.duration_seconds = duration
        existing.file_path = str(output_path)
        existing.file_size_mb = file_size
        existing.status = status
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
    if start_time is None:
        start_time = datetime.now().replace(minute=0, second=0, microsecond=0)

    if has_completed_recording(station["id"], start_time):
        with Session(engine) as session:
            return session.exec(
                select(Recording).where(
                    Recording.station_id == station["id"],
                    Recording.start_time == start_time,
                    Recording.status == "completed",
                )
            ).one()

    output_path = build_output_path(station, start_time)
    log_path = LOGS_DIR / f"{output_path.stem}.log"

    success = False
    last_error = ""

    for attempt, delay in enumerate(ATTEMPT_DELAYS_SECONDS):
        if delay:
            logger.info(
                "Retry recording %s (%s) in %ss (attempt %d)",
                station["id"],
                start_time.strftime("%Y-%m-%d %H:%M"),
                delay,
                attempt + 1,
            )
            time.sleep(delay)

        success, last_error = run_ffmpeg_record(station["url"], output_path, log_path)
        if success:
            break
        if output_path.exists():
            output_path.unlink(missing_ok=True)

    status = "completed" if success else "failed"

    if not success and output_path.exists():
        output_path.unlink(missing_ok=True)

    with Session(engine) as session:
        recording = save_recording_to_db(
            session,
            station,
            start_time,
            output_path,
            status,
            error_message=None if success else last_error,
        )

    if not success:
        raise RuntimeError(
            f"Recording failed for {station['id']} after {len(ATTEMPT_DELAYS_SECONDS)} attempts: {last_error}"
        )

    return recording
