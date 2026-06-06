import logging
import subprocess
from pathlib import Path

from sqlmodel import Session, select

from app.database import RECORDINGS_DIR, engine
from app.models import Recording
from app.recorder import MP3_BITRATE

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".wav", ".wave", ".flac", ".aac", ".m4a", ".ogg"}


def _get_file_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    return round(path.stat().st_size / (1024 * 1024), 2)


def convert_file_to_mp3(source: Path, target: Path) -> bool:
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        MP3_BITRATE,
        "-ar",
        "44100",
        str(target),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if result.returncode != 0:
            logger.error(
                "ffmpeg convert failed for %s: %s",
                source.name,
                (result.stderr or result.stdout)[-300:],
            )
            return False
        return target.exists() and target.stat().st_size > 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.error("ffmpeg convert failed for %s: %s", source.name, exc)
        return False


def _update_recording_path(session: Session, old_path: Path, new_path: Path) -> None:
    old_str = str(old_path)
    new_str = str(new_path)
    recordings = list(
        session.exec(select(Recording).where(Recording.file_path == old_str)).all()
    )
    for recording in recordings:
        recording.file_path = new_str
        recording.file_size_mb = _get_file_size_mb(new_path)
        session.add(recording)

    if not recordings:
        stem = old_path.stem
        for recording in session.exec(select(Recording)).all():
            if Path(recording.file_path).stem == stem:
                recording.file_path = new_str
                recording.file_size_mb = _get_file_size_mb(new_path)
                session.add(recording)


def convert_wav_recordings() -> dict:
    """Convert non-MP3 recordings to 128kbps MP3 and remove originals."""
    converted = 0
    skipped = 0
    failed = 0
    freed_mb = 0.0

    audio_files = sorted(
        path
        for path in RECORDINGS_DIR.iterdir()
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
    )

    if not audio_files:
        return {"converted": 0, "skipped": 0, "failed": 0, "freed_mb": 0.0}

    logger.info("Found %d non-MP3 recordings to convert", len(audio_files))

    with Session(engine) as session:
        for source in audio_files:
            target = source.with_suffix(".mp3")
            if target.exists():
                skipped += 1
                source.unlink(missing_ok=True)
                _update_recording_path(session, source, target)
                continue

            old_size_mb = _get_file_size_mb(source)
            if not convert_file_to_mp3(source, target):
                failed += 1
                target.unlink(missing_ok=True)
                continue

            new_size_mb = _get_file_size_mb(target)
            _update_recording_path(session, source, target)
            source.unlink(missing_ok=True)
            converted += 1
            freed_mb += max(0.0, old_size_mb - new_size_mb)

        session.commit()

    result = {
        "converted": converted,
        "skipped": skipped,
        "failed": failed,
        "freed_mb": round(freed_mb, 2),
    }
    if converted or failed:
        logger.info("WAV/FLAC conversion done: %s", result)
    return result
