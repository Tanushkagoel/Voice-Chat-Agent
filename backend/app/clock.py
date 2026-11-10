from datetime import date

from app.config import get_settings


def today() -> date:
    settings = get_settings()
    if settings.alder_today:
        return date.fromisoformat(settings.alder_today)
    return date.today()
