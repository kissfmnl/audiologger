import json
import logging
from pathlib import Path

from app.database import LOGOS_DIR, RECORDINGS_DIR

logger = logging.getLogger(__name__)

SETTINGS_PATH = RECORDINGS_DIR / "site.settings.json"
REQUESTS_PATH = RECORDINGS_DIR / "stream_requests.json"

DEFAULT_SETTINGS = {
    "site_logo_path": None,
    "footer_text": "Professionele radio-opname archieven",
    "footer_link_url": "/contact",
    "footer_link_label": "Stream aanvragen",
}


def load_site_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.exists():
        try:
            stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                settings.update(stored)
        except (OSError, ValueError) as exc:
            logger.warning("Could not read site settings: %s", exc)

    logo_path = settings.get("site_logo_path")
    if logo_path:
        settings["logo_url"] = f"/logos/{Path(logo_path).name}"
    else:
        settings["logo_url"] = None
    return settings


def save_site_settings(data: dict) -> dict:
    current = load_site_settings()
    for key in DEFAULT_SETTINGS:
        if key in data:
            current[key] = data[key]
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return load_site_settings()


def save_site_logo(filename: str, content: bytes) -> str:
    LOGOS_DIR.mkdir(parents=True, exist_ok=True)
    path = LOGOS_DIR / filename
    path.write_bytes(content)
    save_site_settings({"site_logo_path": f"logos/{filename}"})
    return filename


def load_stream_requests() -> list[dict]:
    if not REQUESTS_PATH.exists():
        return []
    try:
        data = json.loads(REQUESTS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def add_stream_request(payload: dict) -> dict:
    requests = load_stream_requests()
    from datetime import datetime

    entry = {
        "id": len(requests) + 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        **payload,
    }
    requests.insert(0, entry)
    REQUESTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    REQUESTS_PATH.write_text(json.dumps(requests, indent=2), encoding="utf-8")
    return entry
