from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class Station(SQLModel, table=True):
    id: str = Field(primary_key=True)
    name: str
    country: str
    flag: str = Field(default="📻")
    url: str
    schedule_hours: str = Field(default="*")
    active: bool = Field(default=True)
    logo_path: Optional[str] = Field(default=None)


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
