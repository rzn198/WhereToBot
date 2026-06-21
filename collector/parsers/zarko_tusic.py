import re
from datetime import date, timezone
from loguru import logger
from telethon.tl.types import Message

from collector.parsers.base import BaseParser, ParsedEvent
from collector.entities.utf16 import utf16_len
from collector.entities.recalculator import recalc_entities
from collector.entities.truncator import truncate_if_needed, EventText
from collector.date_parser.parser import parse_date, MONTH_MAP


_DAY_HEADER = re.compile(
    r'^(Понедельник|Вторник|Среда|Четверг|Пятница|Суббота|Субота|Воскресенье)'
    r',\s+(\d{1,2})\s+(\w+)',
    re.IGNORECASE | re.MULTILINE
)

_EVENT_LINE = re.compile(r'^[•\-]\s+(.+)$', re.MULTILINE)


def _parse_day_date(day_str: int, month_str: str, published_at: date) -> date | None:
    month_lower = month_str.lower()
    month_num = MONTH_MAP.get(month_lower)
    if not month_num:
        return None

    year = published_at.year
    try:
        return date(year, month_num, day_str)
    except ValueError:
        return None


def _get_line_start_offset(text: str, line_start_char: int) -> int:
    return utf16_len(text[:line_start_char])


class ZarkoTusicParser(BaseParser):
    def parse(self, message: Message) -> list[ParsedEvent]:
        if not message.message:
            return []

        text = message.message
        entities = self._entities_to_dicts(message)
        published_at = message.date.astimezone(timezone.utc).date()
        if not self._is_digest(text):
            logger.debug(
                f"ZarkoTusic: not a digest | msg_id={message.id}"
            )
            from collector.parsers.default import DefaultParser
            return DefaultParser().parse(message)

        events = self._parse_digest(
            text=text,
            entities=entities,
            published_at=published_at,
            msg_id=message.id,
        )

        logger.info(
            f"ZarkoTusic: parsed {len(events)} events | msg_id={message.id}"
        )
        return events

    def _is_digest(self, text: str) -> bool:
        return bool(_DAY_HEADER.search(text))

    def _parse_digest(
        self,
        text: str,
        entities: list[dict],
        published_at: date,
        msg_id: int,
    ) -> list[ParsedEvent]:
        results = []
        position = 1

        day_headers = []
        for match in _DAY_HEADER.finditer(text):
            day_num = int(match.group(2))
            month_str = match.group(3)
            event_date = _parse_day_date(day_num, month_str, published_at)
            day_headers.append({
                'start': match.start(),
                'end': match.end(),
                'header_text': match.group(0),
                'date': event_date,
            })

        if not day_headers:
            return []

        for i, day in enumerate(day_headers):
            block_end = day_headers[i + 1]['start'] if i + 1 < len(day_headers) else len(text)
            block_text = text[day['start']:block_end]

            for line_match in _EVENT_LINE.finditer(block_text):
                line_text = line_match.group(0)  # строка с "•"
                line_content = line_match.group(1)  # содержимое без "•"

                event_text = f"{day['header_text']}\n{line_text}"
                orig_line_start = day['start'] + line_match.start()
                orig_line_utf16_start = utf16_len(text[:orig_line_start])
                new_header_utf16_len = utf16_len(day['header_text'] + '\n')

                event_entities = recalc_entities(
                    entities=entities,
                    original_text=text,
                    new_text=event_text,
                    offset_shift=orig_line_utf16_start - new_header_utf16_len,
                )

                event = truncate_if_needed(
                    EventText(text=event_text, entities=event_entities)
                )

                results.append(ParsedEvent(
                    text=event.text,
                    entities=event.entities,
                    date=day['date'],
                    position=position,
                ))

                position += 1

                logger.debug(
                    f"ZarkoTusic: event pos={position - 1} | "
                    f"date={day['date']} | "
                    f"text={line_content[:50]!r}"
                )

        return results