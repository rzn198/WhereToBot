import pytest
from collector.entities.truncator import truncate_if_needed, EventText
from collector.entities.utf16 import utf16_len


class TestTruncateIfNeeded:

    def test_no_truncation_needed(self):
        event = EventText(text='Короткий текст', entities=[])
        result = truncate_if_needed(event)
        assert result.text == 'Короткий текст'
        assert result.entities == []

    def test_exact_limit_no_truncation(self):
        text = 'а' * 4096
        event = EventText(text=text, entities=[])
        result = truncate_if_needed(event)
        assert result.text == text
        assert utf16_len(result.text) == 4096

    def test_truncation_adds_ellipsis(self):
        text = 'а' * 4097
        event = EventText(text=text, entities=[])
        result = truncate_if_needed(event)
        assert result.text.endswith('...')
        assert utf16_len(result.text) == 4096

    def test_entity_before_truncation_kept(self):
        text = 'а' * 4097
        entity = {'type': 'bold', 'offset': 0, 'length': 10}
        event = EventText(text=text, entities=[entity])
        result = truncate_if_needed(event)
        assert len(result.entities) == 1
        assert result.entities[0]['offset'] == 0
        assert result.entities[0]['length'] == 10

    def test_entity_after_truncation_dropped(self):
        text = 'а' * 4097
        entity = {'type': 'bold', 'offset': 4094, 'length': 10}
        event = EventText(text=text, entities=[entity])
        result = truncate_if_needed(event)
        assert len(result.entities) == 0

    def test_multiple_entities_partial_keep(self):
        text = 'а' * 4097
        entities = [
            {'type': 'bold',   'offset': 0,    'length': 10},   # сохраняется
            {'type': 'italic', 'offset': 100,  'length': 20},   # сохраняется
            {'type': 'bold',   'offset': 4090, 'length': 10},   # отбрасывается
        ]
        event = EventText(text=text, entities=entities)
        result = truncate_if_needed(event)
        assert len(result.entities) == 2

    def test_truncation_with_emoji(self):
        text = '🎸' * 2000  # 2000 эмодзи = 4000 UTF-16 units
        text += 'а' * 100   # + 100 кириллических = 4100 UTF-16 units
        event = EventText(text=text, entities=[])
        result = truncate_if_needed(event)
        assert result.text.endswith('...')
        assert utf16_len(result.text) <= 4096

    def test_custom_limit(self):
        text = 'а' * 1025
        event = EventText(text=text, entities=[])
        result = truncate_if_needed(event, limit=1024)
        assert result.text.endswith('...')
        assert utf16_len(result.text) == 1024

    def test_empty_text(self):
        event = EventText(text='', entities=[])
        result = truncate_if_needed(event)
        assert result.text == ''
        assert result.entities == []