from datetime import timezone
from loguru import logger
from telethon.tl.types import Message

from collector.parsers.base import BaseParser, ParsedEvent
from collector.date_parser.parser import parse_date
from collector.entities.truncator import truncate_if_needed, EventText


class DefaultParser(BaseParser):

    def parse(self, message: Message) -> list[ParsedEvent]:
        if not message.message:
            logger.debug(f"Skipping empty message | id={message.id}")
            return []

        text = message.message
        entities = self._entities_to_dicts(message)
        published_at = message.date.astimezone(timezone.utc).date()

        event_date = parse_date(text, published_at)
        event = truncate_if_needed(EventText(text=text, entities=entities))

        return [ParsedEvent(
            text=event.text,
            entities=event.entities,
            date=event_date,
            position=0,
            has_photo=message.photo is not None,  # v1.3: флаг наличия фото
        )]