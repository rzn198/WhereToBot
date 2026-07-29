import re
from datetime import date, timezone
from loguru import logger
from telethon.tl.types import Message

from collector.parsers.base import BaseParser, ParsedEvent

_ENGLISH_WEEKDAY_MAP = {
    'monday':    0,
    'tuesday':   1,
    'wednesday': 2,
    'thursday':  3,
    'friday':    4,
    'saturday':  5,
    'sunday':    6,
}

_ENGLISH_WEEKDAY_PATTERN = re.compile(
    r'^\W*(MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY)\b',
    re.IGNORECASE | re.MULTILINE,
)


def _parse_weekday_date(text: str, published_at: date) -> date | None:
    match = _ENGLISH_WEEKDAY_PATTERN.search(text)
    if not match:
        return None

    day_name = match.group(1).lower()
    target_weekday = _ENGLISH_WEEKDAY_MAP.get(day_name)
    if target_weekday is None:
        return None

    pub_weekday = published_at.weekday()
    days_ahead = (target_weekday - pub_weekday) % 7

    from datetime import timedelta
    return published_at + timedelta(days=days_ahead)


class RunningBegParser(BaseParser):
    def parse(self, message: Message) -> list[ParsedEvent]:
        if not message.message:
            return []

        text = message.message
        entities = self._entities_to_dicts(message)
        published_at = message.date.astimezone(timezone.utc).date()

        event_date = _parse_weekday_date(text, published_at)

        if event_date is None:
            logger.debug(
                f"RunningBeg: no weekday found, delegating to DefaultParser "
                f"| msg_id={message.id}"
            )
            from collector.parsers.default import DefaultParser
            return DefaultParser().parse(message)

        logger.info(
            f"RunningBeg: parsed | msg_id={message.id} | date={event_date}"
        )

        return [ParsedEvent(
            text=text,
            entities=entities,
            date=event_date,
            position=1,
        )]
