"""Datas no fuso da aplicação (horário de Brasília) e instantes em UTC."""

from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone


def app_tz() -> ZoneInfo:
    return ZoneInfo(settings.TIME_ZONE)


def now() -> datetime:
    """Instante atual em UTC (ponto único para facilitar testes)."""
    return timezone.now()


def local_today(at: datetime | None = None) -> date:
    return (at or now()).astimezone(app_tz()).date()


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """Início e fim (exclusivo) do dia no fuso da aplicação, em UTC."""
    tz = app_tz()
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(dt_timezone.utc), end.astimezone(dt_timezone.utc)


def iso_utc(value: datetime | None) -> str | None:
    """ISO 8601 em UTC com sufixo Z, ou None."""
    if value is None:
        return None
    return value.astimezone(dt_timezone.utc).isoformat().replace("+00:00", "Z")


def parse_day(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)
