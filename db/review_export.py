"""Выгрузка таблицы event_reviews (пересмотры и решения очередей «Неразобранные») в CSV."""
from __future__ import annotations

import csv
import io

from db.tables import Tables

COLUMNS = ['id', 'created_at', 'flow', 'status', 'kind', 'reason_code', 'reason_text', 'correct_category', 'correct_date',
           'title', 'starts_at', 'venue', 'source', 'source_url', 'event_types', 'description',
           'event_id', 'message_id', 'resolved_at', 'resolution_note']

# код выбора → (условие WHERE, часть имени файла, подпись)
SCOPES = {
    'open': ("flow = 'review' AND status = 'open'", 'open', 'Открытые пересмотры'),
    'all': ('TRUE', 'all', 'Все пересмотры и решения очередей'),
    'nd': ("flow = 'no_date'", 'no_date', 'Очередь «Без даты»'),
    'nc': ("flow = 'no_category'", 'no_category', 'Очередь «Без категории»'),
}


def _cell(v):
    if isinstance(v, list):
        return '|'.join(v)
    return '' if v is None else str(v)


async def fetch_rows(pool, scope: str) -> list:
    where = SCOPES[scope][0]
    return await pool.fetch(f"SELECT {', '.join(COLUMNS)} FROM {Tables.EVENT_REVIEWS} WHERE {where} ORDER BY id")


def to_csv_bytes(rows) -> bytes:
    buf = io.StringIO(newline='')
    w = csv.writer(buf)
    w.writerow(COLUMNS)
    for r in rows:
        w.writerow([_cell(r[c]) for c in COLUMNS])
    return buf.getvalue().encode('utf-8-sig')
