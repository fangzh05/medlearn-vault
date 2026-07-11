from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, PlainSerializer


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EventModel(DomainModel):
    """Append-only observation contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a timezone offset")
    return value


AwareDatetime = Annotated[
    datetime,
    AfterValidator(_aware),
    PlainSerializer(lambda value: value.isoformat(), return_type=str),
]
