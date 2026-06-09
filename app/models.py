from datetime import date, datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class Station(SQLModel, table=True):
    id: str = Field(primary_key=True)
    name: str
    country: str = Field(default="NL")
    flag: str = Field(default="🇳🇱")
    timezone: str = Field(default="Europe/Amsterdam")
    url: str
    schedule_hours: str = Field(default="*")
    is_event: bool = Field(default=False)
    event_start_date: Optional[str] = Field(default=None)
    event_end_date: Optional[str] = Field(default=None)
    retention_days: Optional[int] = Field(default=None)
    active: bool = Field(default=True)
    logo_path: Optional[str] = Field(default=None)
    dropbox_archive: bool = Field(default=False)


class Recording(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    station_id: str = Field(index=True)
    station_name: str
    country: str
    start_time: datetime = Field(index=True)
    end_time: datetime
    duration_seconds: int
    file_path: str
    file_size_mb: float
    status: str = Field(default="completed", index=True)
    peaks_file: Optional[str] = Field(default=None)
    dropbox_path: Optional[str] = Field(default=None)
