import logging
import re
from pathlib import Path

import yaml
from sqlmodel import Session, select

from app.database import BASE_DIR, LOGOS_DIR, engine
from app.models import Station

logger = logging.getLogger(__name__)

STATIONS_CONFIG = BASE_DIR / "config" / "stations.yaml"
STATION_ID_PATTERN = re.compile(r"^[a-z0-9_]+$")

COUNTRY_FLAGS = {
    "NL": "🇳🇱",
    "BE": "🇧🇪",
    "DE": "🇩🇪",
    "US": "🇺🇸",
    "UK": "🇬🇧",
    "FR": "🇫🇷",
}


def parse_cron_to_hours(cron: str) -> str:
    parts = cron.strip().split()
    if len(parts) != 5:
        return "*"
    hour = parts[1]
    return "*" if hour == "*" else hour


def hours_to_cron(hours: str) -> str:
    cleaned = hours.strip()
    if not cleaned or cleaned == "*":
        return "0 * * * *"
    return f"0 {cleaned} * * *"


def format_schedule_label(hours: str) -> str:
    if hours.strip() == "*":
        return "Elk heel uur"
    hour_list = [h.strip() for h in hours.split(",") if h.strip()]
    return ", ".join(f"{h}:00" for h in sorted(hour_list, key=lambda x: int(x)))


def station_to_dict(station: Station) -> dict:
    logo_url = None
    if station.logo_path:
        logo_path = Path(station.logo_path)
        if logo_path.exists():
            logo_url = f"/logos/{logo_path.name}"

    return {
        "id": station.id,
        "name": station.name,
        "country": station.country.upper(),
        "flag": station.flag or COUNTRY_FLAGS.get(station.country.upper(), "📻"),
        "url": station.url,
        "schedule_hours": station.schedule_hours,
        "schedule_cron": hours_to_cron(station.schedule_hours),
        "schedule_label": format_schedule_label(station.schedule_hours),
        "active": station.active,
        "logo_path": station.logo_path,
        "logo_url": logo_url,
    }


def seed_stations_from_yaml() -> None:
    if not STATIONS_CONFIG.exists():
        return

    with open(STATIONS_CONFIG, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    with Session(engine) as session:
        existing = session.exec(select(Station)).first()
        if existing:
            return

        for item in data.get("stations", []):
            country = item.get("country", "NL").upper()
            station = Station(
                id=item["id"],
                name=item["name"],
                country=country,
                flag=item.get("flag") or COUNTRY_FLAGS.get(country, "📻"),
                url=item["url"],
                schedule_hours=parse_cron_to_hours(item.get("schedule", "0 * * * *")),
                active=item.get("active", True),
            )
            session.add(station)

        session.commit()
        logger.info("Seeded stations from stations.yaml")


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


def normalize_hours(hours: str | list[str] | None) -> str:
    if hours is None or hours == "*" or hours == ["*"]:
        return "*"
    if isinstance(hours, str):
        if hours.strip() == "*":
            return "*"
        parts = [h.strip() for h in hours.split(",") if h.strip()]
    else:
        parts = [str(h).strip() for h in hours if str(h).strip()]

    if not parts:
        return "*"

    normalized: list[str] = []
    for part in parts:
        hour = int(part)
        if hour < 0 or hour > 23:
            raise ValueError("Uren moeten tussen 0 en 23 liggen")
        if str(hour) not in normalized:
            normalized.append(str(hour))

    normalized.sort(key=int)
    return ",".join(normalized)


def create_station(
    session: Session,
    station_id: str,
    name: str,
    country: str,
    url: str,
    schedule_hours: str | list[str] | None = "*",
    flag: str | None = None,
    active: bool = True,
) -> Station:
    validate_station_id(station_id)
    validate_url(url)

    if session.get(Station, station_id):
        raise ValueError(f"Zender met ID '{station_id}' bestaat al")

    country = country.upper()
    station = Station(
        id=station_id,
        name=name.strip(),
        country=country,
        flag=flag or COUNTRY_FLAGS.get(country, "📻"),
        url=url.strip(),
        schedule_hours=normalize_hours(schedule_hours),
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
    schedule_hours: str | list[str] | None,
    flag: str | None,
    active: bool,
) -> Station:
    station = session.get(Station, station_id)
    if not station:
        raise ValueError("Zender niet gevonden")

    validate_url(url)
    country = country.upper()

    station.name = name.strip()
    station.country = country
    station.flag = flag or COUNTRY_FLAGS.get(country, "📻")
    station.url = url.strip()
    station.schedule_hours = normalize_hours(schedule_hours)
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
        logo = Path(station.logo_path)
        if logo.exists():
            logo.unlink(missing_ok=True)

    session.delete(station)
    session.commit()


def save_station_logo(station_id: str, image_bytes: bytes) -> str:
    if len(image_bytes) > 5 * 1024 * 1024:
        raise ValueError("Logo is te groot (max 5 MB)")

    LOGOS_DIR.mkdir(parents=True, exist_ok=True)
    logo_path = LOGOS_DIR / f"{station_id}.jpg"
    logo_path.write_bytes(image_bytes)

    with Session(engine) as session:
        station = session.get(Station, station_id)
        if not station:
            raise ValueError("Zender niet gevonden")

        if station.logo_path:
            old_logo = Path(station.logo_path)
            if old_logo.exists() and old_logo != logo_path:
                old_logo.unlink(missing_ok=True)

        station.logo_path = str(logo_path)
        session.add(station)
        session.commit()

    return str(logo_path)
