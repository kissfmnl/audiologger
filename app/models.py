from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


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
