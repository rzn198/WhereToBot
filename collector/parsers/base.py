from dataclasses import dataclass
from datetime import date
from abc import ABC, abstractmethod
from telethon.tl.types import Message


@dataclass
class ParsedEvent:
    text:               str
    entities:           list[dict]
    date:               date | None
    position:           int = 0
    has_photo:          bool = False        # v1.3: есть ли фото в сообщении
    photo_file_id:      str | None = None
    storage_message_id: int | None = None


class BaseParser(ABC):

    @abstractmethod
    def parse(self, message: Message) -> list[ParsedEvent]:
        ...

    def _entities_to_dicts(self, message: Message) -> list[dict]:
        if not message.entities:
            return []
        return [e.to_dict() for e in message.entities]

