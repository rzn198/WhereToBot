from dataclasses import dataclass
from loguru import logger
from collector.entities.utf16 import utf16_len, truncate_to_utf16


TELEGRAM_MAX_LEN = 4096


@dataclass
class EventText:
    text: str
    entities: list[dict]


def truncate_if_needed(event: EventText, limit: int = TELEGRAM_MAX_LEN) -> EventText:

    text_len = utf16_len(event.text)

    if text_len <= limit:
        return event

    truncated_text = truncate_to_utf16(event.text, limit - 3) + '...'
    truncated_len = utf16_len(truncated_text)

    truncated_entities = []
    skipped = 0

    for entity in event.entities:
        offset = entity.get('offset', 0)
        length = entity.get('length', 0)

        if offset + length <= truncated_len - 3:  # до '...'
            truncated_entities.append(entity)
        else:
            skipped += 1

    if skipped:
        logger.debug(
            f"Truncated {skipped} entities | "
            f"original_len={text_len} truncated_len={truncated_len}"
        )

    logger.info(
        f"Text truncated | "
        f"original={text_len} → truncated={truncated_len} | "
        f"entities_kept={len(truncated_entities)} skipped={skipped}"
    )

    return EventText(text=truncated_text, entities=truncated_entities)