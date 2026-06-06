import json
import logging
import math
import shutil
import struct
import subprocess
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_BARS = 1000
BYTES_PER_SECOND_128K = 16000


def peaks_cache_path(audio_path: Path) -> Path:
    return audio_path.with_name(f"{audio_path.name}.peaks.json")


def placeholder_peaks(bars: int = DEFAULT_BARS) -> list[float]:
    return [round(0.07 + 0.05 * math.sin(index * 0.06), 4) for index in range(bars)]


def estimate_duration(path: Path) -> float:
    cached = load_cached_peaks(path)
    if cached:
        return cached[1]
    if not path.exists():
        return 3600.0
    size = path.stat().st_size
    if size <= 0:
        return 3600.0
    return max(1.0, size / BYTES_PER_SECOND_128K)


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
            timeout=5,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
        logger.warning("ffprobe failed for %s: %s", path, exc)
    return estimate_duration(path)


def _parse_audiowaveform_json(payload: dict) -> list[float]:
    raw = payload.get("data", [])
    channels = int(payload.get("channels", 1) or 1)
    bits = int(payload.get("bits", 8) or 8)
    max_val = 128 if bits == 8 else 32768
    stride = 2 * channels
    peaks: list[float] = []

    for index in range(0, len(raw) - 1, stride):
        minimum = abs(int(raw[index]))
        maximum = abs(int(raw[index + 1])) if index + 1 < len(raw) else 0
        peaks.append(max(minimum, maximum) / max_val)

    max_peak = max(peaks) if peaks else 0.0
    if max_peak > 0:
        peaks = [round(peak / max_peak, 4) for peak in peaks]
    return peaks


def _decode_peaks_audiowaveform(path: Path, bars: int) -> tuple[list[float], float]:
    if not shutil.which("audiowaveform"):
        return [], 0.0

    duration = max(get_audio_duration(path), 1.0)
    pixels_per_second = max(0.15, bars / duration)

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        output_path = Path(handle.name)

    try:
        result = subprocess.run(
            [
                "audiowaveform",
                "-i",
                str(path),
                "-o",
                str(output_path),
                "--output-format",
                "json",
                "-b",
                "8",
                "--pixels-per-second",
                f"{pixels_per_second:.4f}",
            ],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "audiowaveform failed for %s: %s",
                path.name,
                (result.stderr or result.stdout)[-300:],
            )
            return [], duration

        payload = json.loads(output_path.read_text(encoding="utf-8"))
        peaks = _parse_audiowaveform_json(payload)
        if not peaks:
            return [], duration
        return peaks[:bars], duration
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        logger.warning("audiowaveform failed for %s: %s", path.name, exc)
        return [], duration
    finally:
        output_path.unlink(missing_ok=True)


def _decode_peaks_ffmpeg(path: Path, bars: int) -> tuple[list[float], float]:
    duration = max(get_audio_duration(path), 1.0)
    sample_rate = 80
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
        chunk = process.stdout.read(262144)
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
        process.wait(timeout=2)
    except (subprocess.TimeoutExpired, OSError):
        process.kill()

    max_peak = max(peaks) if peaks else 0.0
    if max_peak > 0:
        peaks = [round(peak / max_peak, 4) for peak in peaks]

    while len(peaks) < bars:
        peaks.append(0.0)

    return peaks[:bars], duration


def _decode_peaks(path: Path, bars: int) -> tuple[list[float], float]:
    peaks, duration = _decode_peaks_audiowaveform(path, bars)
    if peaks:
        return peaks, duration
    return _decode_peaks_ffmpeg(path, bars)


def load_cached_peaks(path: Path) -> tuple[list[float], float] | None:
    cache_path = peaks_cache_path(path)
    if not cache_path.exists() or not path.exists():
        return None

    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if int(cache.get("source_mtime", -1)) != int(path.stat().st_mtime):
            return None
        if int(cache.get("source_size", -1)) != int(path.stat().st_size):
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
        stat = path.stat()
        cache_path.write_text(
            json.dumps(
                {
                    "duration": duration,
                    "peaks": peaks,
                    "source_mtime": int(stat.st_mtime),
                    "source_size": int(stat.st_size),
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Could not write peaks cache %s: %s", cache_path, exc)


def read_peaks_fast(path: Path) -> dict:
    """Return peaks immediately; never block the request on ffmpeg."""
    duration = estimate_duration(path)
    if not path.exists() or path.stat().st_size == 0:
        return {
            "peaks": placeholder_peaks(),
            "duration": duration,
            "ready": True,
            "precise": False,
        }

    cached = load_cached_peaks(path)
    if cached:
        peaks, cached_duration = cached
        return {
            "peaks": peaks,
            "duration": cached_duration,
            "ready": True,
            "precise": True,
        }

    ensure_peaks_async(path)
    return {
        "peaks": placeholder_peaks(),
        "duration": duration,
        "ready": True,
        "precise": False,
    }


def ensure_peaks(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    if load_cached_peaks(path):
        return
    peaks, duration = _decode_peaks(path, DEFAULT_BARS)
    if peaks:
        save_cached_peaks(path, peaks, duration)


def ensure_peaks_async(path: Path) -> None:
    threading.Thread(target=ensure_peaks, args=(path,), daemon=True).start()


def warm_missing_peaks(recordings_dir: Path) -> None:
    for audio_path in sorted(recordings_dir.glob("*.mp3")):
        if load_cached_peaks(audio_path):
            continue
        try:
            ensure_peaks(audio_path)
        except Exception:
            logger.exception("Failed to warm peaks for %s", audio_path.name)
