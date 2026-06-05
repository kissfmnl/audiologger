import logging
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from sqlmodel import Session, select
from sqlalchemy import text

from app.database import BASE_DIR, LOGOS_DIR, engine
from app.models import Station

logger = logging.getLogger(__name__)

STATIONS_CONFIG = BASE_DIR / "config" / "stations.yaml"
STATION_ID_PATTERN = re.compile(r"^[a-z0-9_]+$")

COUNTRIES = [
    {"code": "NL", "name": "Nederland", "flag": "🇳🇱", "timezone": "Europe/Amsterdam", "city": "Amsterdam"},
    {"code": "BE", "name": "België", "flag": "🇧🇪", "timezone": "Europe/Brussels", "city": "Brussel"},
    {"code": "DE", "name": "Duitsland", "flag": "🇩🇪", "timezone": "Europe/Berlin", "city": "Berlin"},
    {"code": "FR", "name": "Frankrijk", "flag": "🇫🇷", "timezone": "Europe/Paris", "city": "Parijs"},
    {"code": "UK", "name": "Verenigd Koninkrijk", "flag": "🇬🇧", "timezone": "Europe/London", "city": "Londen"},
    {"code": "US", "name": "Verenigde Staten (ET)", "flag": "🇺🇸", "timezone": "America/New_York", "city": "New York"},
]

COUNTRY_MAP = {c["code"]: c for c in COUNTRIES}


def resolve_logo_file(logo_path: str | None) -> Path | None:
    if not logo_path:
        return None

    path = Path(logo_path)
    candidates = [
        path,
        LOGOS_DIR / path.name,
        LOGOS_DIR / logo_path,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def logo_filename(station_id: str) -> str:
    return f"{station_id}.jpg"


def get_country_info(country_code: str) -> dict:
    code = country_code.upper()
    return COUNTRY_MAP.get(code, COUNTRY_MAP["NL"])


def flag_for_country(country_code: str) -> str:
    return get_country_info(country_code)["flag"]


def timezone_for_country(country_code: str) -> str:
    return get_country_info(country_code)["timezone"]


def hours_to_cron(hours: str = "*") -> str:
    return "0 * * * *"


def format_schedule_label(station: dict) -> str:
    if station.get("is_event"):
        start = station.get("event_start_date") or "—"
        end = station.get("event_end_date") or "—"
        return f"Evenement · {start} t/m {end}"
    tz = station.get("timezone", "Europe/Amsterdam")
    return f"Hele dag · elk uur ({tz})"


def station_to_dict(station: Station) -> dict:
    logo_url = None
    logo_file = resolve_logo_file(station.logo_path)
    if logo_file:
        logo_url = f"/logos/{logo_file.name}"

    country = station.country.upper()
    info = get_country_info(country)

    data = {
        "id": station.id,
        "name": station.name,
        "country": country,
        "flag": info["flag"],
        "timezone": station.timezone or info["timezone"],
        "country_name": info["name"],
        "country_city": info["city"],
        "url": station.url,
        "schedule_hours": "*",
        "schedule_cron": hours_to_cron(),
        "is_event": station.is_event,
        "event_start_date": station.event_start_date or "",
        "event_end_date": station.event_end_date or "",
        "active": station.active,
        "logo_path": station.logo_path,
        "logo_url": logo_url,
    }
    data["schedule_label"] = format_schedule_label(data)
    return data


def migrate_station_schema() -> None:
    columns = {
        "timezone": "TEXT DEFAULT 'Europe/Amsterdam'",
        "is_event": "INTEGER DEFAULT 0",
        "event_start_date": "TEXT",
        "event_end_date": "TEXT",
    }

    with engine.connect() as conn:
        existing = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(station)")).fetchall()
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE station ADD COLUMN {name} {definition}"))
        conn.commit()

    with Session(engine) as session:
        stations = session.exec(select(Station)).all()
        changed = False
        for station in stations:
            info = get_country_info(station.country)
            if station.timezone != info["timezone"]:
                station.timezone = info["timezone"]
                changed = True
            if station.flag != info["flag"]:
                station.flag = info["flag"]
                changed = True
            if station.schedule_hours != "*":
                station.schedule_hours = "*"
                changed = True
        if changed:
            session.commit()


def seed_stations_from_yaml() -> None:
    """Alleen handmatig aanroepen (dev). Wordt niet meer automatisch bij opstart gebruikt."""
    if not STATIONS_CONFIG.exists():
        return

    with open(STATIONS_CONFIG, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    with Session(engine) as session:
        if session.exec(select(Station)).first():
            return

        for item in data.get("stations", []):
            country = item.get("country", "NL").upper()
            info = get_country_info(country)
            station = Station(
                id=item["id"],
                name=item["name"],
                country=country,
                flag=info["flag"],
                timezone=info["timezone"],
                url=item["url"],
                schedule_hours="*",
                active=item.get("active", True),
            )
            session.add(station)

        session.commit()
        logger.info("Seeded stations from stations.yaml (manual/dev only)")


def reconcile_logos() -> None:
    """Koppel logo-bestanden op schijf aan zenders in de database."""
    if not LOGOS_DIR.exists():
        return

    with Session(engine) as session:
        changed = False
        for station in session.exec(select(Station)).all():
            expected = logo_filename(station.id)
            logo_file = LOGOS_DIR / expected
            if logo_file.exists() and station.logo_path != expected:
                station.logo_path = expected
                changed = True
            elif station.logo_path:
                resolved = resolve_logo_file(station.logo_path)
                if resolved and station.logo_path != expected:
                    station.logo_path = expected
                    changed = True
        if changed:
            session.commit()
            logger.info("Reconciled station logos from disk")


def load_stations(active_only: bool = False) -> list[dict]:
    with Session(engine) as session:
        statement = select(Station).order_by(Station.name)
        if active_only:
            statement = statement.where(Station.active == True)  # noqa: E712
        stations = session.exec(statement).all()
        return [station_to_dict(s) for s in stations]


def get_station_by_id(station_id: str) -> dict | None:
    with Session(engine) as session:
        station = session.get(Station, station_id)
        return station_to_dict(station) if station else None


def get_station_model(session: Session, station_id: str) -> Station | None:
    return session.get(Station, station_id)


def validate_station_id(station_id: str) -> None:
    if not station_id or not STATION_ID_PATTERN.match(station_id):
        raise ValueError("ID mag alleen kleine letters, cijfers en underscores bevatten")


def validate_url(url: str) -> None:
    if not url.startswith(("http://", "https://")):
        raise ValueError("Stream-URL moet beginnen met http:// of https://")


def validate_country(country: str) -> str:
    code = country.upper()
    if code not in COUNTRY_MAP:
        raise ValueError("Kies een geldig land uit de lijst")
    return code


def parse_event_dates(
    is_event: bool,
    event_start: str | None,
    event_end: str | None,
) -> tuple[bool, str | None, str | None]:
    if not is_event:
        return False, None, None

    if not event_start or not event_end:
        raise ValueError("Evenementzenders vereisen een start- en einddatum")

    try:
        start = date.fromisoformat(event_start)
        end = date.fromisoformat(event_end)
    except ValueError as exc:
        raise ValueError("Ongeldige datum (gebruik JJJJ-MM-DD)") from exc

    if end < start:
        raise ValueError("Einddatum moet na startdatum liggen")

    return True, start.isoformat(), end.isoformat()


def should_record_station(station: dict, moment: datetime | None = None) -> bool:
    if not station.get("active", True):
        return False

    tz = ZoneInfo(station.get("timezone", "Europe/Amsterdam"))
    now = moment.astimezone(tz) if moment and moment.tzinfo else datetime.now(tz)

    if station.get("is_event"):
        start_raw = station.get("event_start_date")
        end_raw = station.get("event_end_date")
        if not start_raw or not end_raw:
            return False
        today = now.date()
        return date.fromisoformat(start_raw) <= today <= date.fromisoformat(end_raw)

    return True


def recording_start_time(station: dict, moment: datetime | None = None) -> datetime:
    tz = ZoneInfo(station.get("timezone", "Europe/Amsterdam"))
    now = moment.astimezone(tz) if moment and moment.tzinfo else datetime.now(tz)
    local = now.replace(minute=0, second=0, microsecond=0)
    return local.replace(tzinfo=None)


def create_station(
    session: Session,
    station_id: str,
    name: str,
    country: str,
    url: str,
    is_event: bool = False,
    event_start_date: str | None = None,
    event_end_date: str | None = None,
    active: bool = True,
) -> Station:
    validate_station_id(station_id)
    validate_url(url)
    country = validate_country(country)
    is_event, event_start_date, event_end_date = parse_event_dates(
        is_event, event_start_date, event_end_date
    )

    if session.get(Station, station_id):
        raise ValueError(f"Zender met ID '{station_id}' bestaat al")

    info = get_country_info(country)
    station = Station(
        id=station_id,
        name=name.strip(),
        country=country,
        flag=info["flag"],
        timezone=info["timezone"],
        url=url.strip(),
        schedule_hours="*",
        is_event=is_event,
        event_start_date=event_start_date,
        event_end_date=event_end_date,
        active=active,
    )
    session.add(station)
    session.commit()
    session.refresh(station)
    return station


def update_station(
    session: Session,
    station_id: str,
    name: str,
    country: str,
    url: str,
    is_event: bool,
    event_start_date: str | None,
    event_end_date: str | None,
    active: bool,
) -> Station:
    station = session.get(Station, station_id)
    if not station:
        raise ValueError("Zender niet gevonden")

    validate_url(url)
    country = validate_country(country)
    is_event, event_start_date, event_end_date = parse_event_dates(
        is_event, event_start_date, event_end_date
    )
    info = get_country_info(country)

    station.name = name.strip()
    station.country = country
    station.flag = info["flag"]
    station.timezone = info["timezone"]
    station.url = url.strip()
    station.schedule_hours = "*"
    station.is_event = is_event
    station.event_start_date = event_start_date
    station.event_end_date = event_end_date
    station.active = active

    session.add(station)
    session.commit()
    session.refresh(station)
    return station


def delete_station(session: Session, station_id: str) -> None:
    station = session.get(Station, station_id)
    if not station:
        raise ValueError("Zender niet gevonden")

    if station.logo_path:
        logo = resolve_logo_file(station.logo_path)
        if logo and logo.exists():
            logo.unlink(missing_ok=True)

    session.delete(station)
    session.commit()


def save_station_logo(station_id: str, image_bytes: bytes) -> str:
    if len(image_bytes) > 5 * 1024 * 1024:
        raise ValueError("Logo is te groot (max 5 MB)")

    LOGOS_DIR.mkdir(parents=True, exist_ok=True)
    filename = logo_filename(station_id)
    logo_path = LOGOS_DIR / filename
    logo_path.write_bytes(image_bytes)

    with Session(engine) as session:
        station = session.get(Station, station_id)
        if not station:
            raise ValueError("Zender niet gevonden")

        station.logo_path = filename
        session.add(station)
        session.commit()

    return filename
