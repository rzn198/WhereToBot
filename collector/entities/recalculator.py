from loguru import logger
from collector.entities.utf16 import utf16_len


def recalc_entities(
    entities: list[dict],
    original_text: str,
    new_text: str,
    offset_shift: int,
) -> list[dict]:

    new_text_len = utf16_len(new_text)
    result = []

    for entity in entities:
        original_offset = entity.get('offset', 0)
        length = entity.get('length', 0)

        new_offset = original_offset - offset_shift

        if new_offset < 0:
            logger.debug(
                f"Entity skipped: starts before event | "
                f"type={entity.get('type')} original_offset={original_offset} "
                f"new_offset={new_offset}"
            )
            continue

        if new_offset + length > new_text_len:
            logger.debug(
                f"Entity skipped: exceeds event boundary | "
                f"type={entity.get('type')} new_offset={new_offset} "
                f"length={length} new_text_len={new_text_len}"
            )
            continue

        result.append({
            **entity,
            'offset': new_offset,
            'length': length,
        })

    return result


def filter_entities_for_text(
    entities: list[dict],
    text: str,
) -> list[dict]:
    text_len = utf16_len(text)
    result = []

    for entity in entities:
        offset = entity.get('offset', 0)
        length = entity.get('length', 0)

        if offset < 0 or offset + length > text_len:
            logger.debug(
                f"Entity filtered: out of bounds | "
                f"type={entity.get('type')} offset={offset} "
                f"length={length} text_len={text_len}"
            )
            continue

        result.append(entity)

    return result