import json
import logging
import struct
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_BARS = 1200


def peaks_cache_path(audio_path: Path) -> Path:
    return audio_path.with_name(f"{audio_path.name}.peaks.json")


def get_audio_duration(path: Path) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
        logger.warning("ffprobe failed for %s: %s", path, exc)
    return 3600.0


def _decode_peaks(path: Path, bars: int) -> tuple[list[float], float]:
    duration = max(get_audio_duration(path), 1.0)
    sample_rate = 100
    samples_per_bar = max(1, int(duration * sample_rate / bars))

    try:
        process = subprocess.Popen(
            [
                "ffmpeg",
                "-threads",
                "0",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-f",
                "s16le",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.warning("ffmpeg peak decode failed for %s: %s", path, exc)
        return [], duration

    peaks: list[float] = []
    bar_max = 0
    samples_in_bar = 0
    buffer = b""

    assert process.stdout is not None
    while len(peaks) < bars:
        chunk = process.stdout.read(131072)
        if not chunk:
            break
        buffer += chunk

        offset = 0
        while offset + 2 <= len(buffer) and len(peaks) < bars:
            sample = struct.unpack_from("<h", buffer, offset)[0]
            offset += 2
            bar_max = max(bar_max, abs(sample))
            samples_in_bar += 1
            if samples_in_bar >= samples_per_bar:
                peaks.append(bar_max / 32768.0)
                bar_max = 0
                samples_in_bar = 0

        buffer = buffer[offset:]

    if bar_max > 0 and len(peaks) < bars:
        peaks.append(bar_max / 32768.0)

    try:
        process.terminate()
        process.wait(timeout=3)
    except (subprocess.TimeoutExpired, OSError):
        process.kill()

    max_peak = max(peaks) if peaks else 0.0
    if max_peak > 0:
        peaks = [round(peak / max_peak, 4) for peak in peaks]

    while len(peaks) < bars:
        peaks.append(0.0)

    return peaks[:bars], duration


def load_cached_peaks(path: Path) -> tuple[list[float], float] | None:
    cache_path = peaks_cache_path(path)
    if not cache_path.exists() or not path.exists():
        return None

    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if cache.get("source_mtime") != path.stat().st_mtime:
            return None
        if cache.get("source_size") != path.stat().st_size:
            return None
        peaks = cache.get("peaks", [])
        duration = float(cache.get("duration", 3600))
        if peaks:
            return peaks, duration
    except (OSError, ValueError, TypeError):
        return None
    return None


def save_cached_peaks(path: Path, peaks: list[float], duration: float) -> None:
    cache_path = peaks_cache_path(path)
    try:
        cache_path.write_text(
            json.dumps(
                {
                    "duration": duration,
                    "peaks": peaks,
                    "source_mtime": path.stat().st_mtime,
                    "source_size": path.stat().st_size,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Could not write peaks cache %s: %s", cache_path, exc)


def get_peaks_for_file(path: Path, bars: int = DEFAULT_BARS) -> tuple[list[float], float]:
    if not path.exists() or path.stat().st_size == 0:
        return [], 0.0

    cached = load_cached_peaks(path)
    if cached:
        return cached

    peaks, duration = _decode_peaks(path, bars)
    if peaks:
        save_cached_peaks(path, peaks, duration)

    return peaks, duration


def ensure_peaks(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    if load_cached_peaks(path):
        return
    peaks, duration = _decode_peaks(path)
    if peaks:
        save_cached_peaks(path, peaks, duration)


def ensure_peaks_async(path: Path) -> None:
    threading.Thread(target=ensure_peaks, args=(path,), daemon=True).start()


def warm_missing_peaks(recordings_dir: Path) -> None:
    for audio_path in sorted(recordings_dir.glob("*.mp3")):
        if not peaks_cache_path(audio_path).exists():
            try:
                ensure_peaks(audio_path)
            except Exception:
                logger.exception("Failed to warm peaks for %s", audio_path.name)
