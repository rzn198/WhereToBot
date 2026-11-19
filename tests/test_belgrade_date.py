"""«Сегодня» в пользовательской части бота считается по Белграду, а не по UTC (без БД)."""
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.handlers import categories as cats
from shared import tz

ROOT = Path(__file__).parent.parent
USER_FACING = ['bot/handlers/calendar.py', 'bot/handlers/categories.py', 'bot/handlers/cinema.py', 'bot/handlers/events.py',
               'bot/handlers/admin_panel.py', 'bot/utils/categories.py', 'bot/utils/moderation.py', 'bot/formatters/event_card.py']


def freeze(monkeypatch, utc: datetime):
    monkeypatch.setattr(tz, 'belgrade_now', lambda: utc.astimezone(tz.BELGRADE_TZ))


def test_late_evening_utc_is_next_day_in_belgrade(monkeypatch):
    freeze(monkeypatch, datetime(2026, 11, 17, 23, 30, tzinfo=timezone.utc))     # 00:30 18 ноября по Белграду
    assert tz.belgrade_today() == date(2026, 11, 18)


def test_midday_same_date(monkeypatch):
    freeze(monkeypatch, datetime(2026, 11, 17, 11, 0, tzinfo=timezone.utc))
    assert tz.belgrade_today() == date(2026, 11, 17)


def test_summer_time_offset_two_hours(monkeypatch):
    freeze(monkeypatch, datetime(2026, 7, 10, 22, 30, tzinfo=timezone.utc))      # CEST: 00:30 11 июля
    assert tz.belgrade_today() == date(2026, 7, 11)


async def test_today_button_after_midnight_belgrade_uses_belgrade_day(monkeypatch):
    freeze(monkeypatch, datetime(2026, 11, 17, 23, 30, tzinfo=timezone.utc))
    q = MagicMock(); q.data = 'catd:concerts:today'; q.answer = AsyncMock()
    upd = MagicMock(callback_query=q); upd.effective_user.id = 1; upd.effective_chat.id = 1
    ctx = MagicMock(); ctx.user_data = {}; ctx.bot_data = {'pool': MagicMock()}
    seen = {}

    async def fake_load(pool, user_id, name):
        seen['filter'] = name
        return ['e1']

    with patch('bot.handlers.events.load_event_ids', fake_load), patch.object(cats, 'show_current_event', AsyncMock()):
        await cats.handle_category_day(upd, ctx)
    assert seen['filter'] == 'cat:concerts:date:2026-11-18'


def test_get_date_range_follows_belgrade(monkeypatch):
    from bot.handlers import events
    freeze(monkeypatch, datetime(2026, 11, 17, 23, 30, tzinfo=timezone.utc))
    assert events.get_date_range('today') == (date(2026, 11, 18), date(2026, 11, 18))
    assert events.get_date_range('tomorrow') == (date(2026, 11, 19), date(2026, 11, 19))


def test_event_card_date_year_uses_belgrade(monkeypatch):
    from bot.formatters.event_card import format_date_ru
    freeze(monkeypatch, datetime(2026, 12, 31, 23, 30, tzinfo=timezone.utc))     # уже 1 января 2027 в Белграде
    assert format_date_ru(date(2027, 1, 5)) == 'Вт, 5 января'                    # без года: год текущий (2027)


@pytest.mark.parametrize('path', USER_FACING)
def test_no_utc_today_left_in_user_facing_code(path):
    src = (ROOT / path).read_text(encoding='utf-8')
    assert 'CURRENT_DATE' not in src, f'{path}: CURRENT_DATE (UTC) вместо даты по Белграду'
    assert not re.search(r'\bdate\.today\(\)', src), f'{path}: date.today() (UTC) вместо belgrade_today()'


def test_sql_helper_expression_matches_what_code_uses():
    from bot.utils.categories import MESSAGES_FUTURE, EVENTS_FUTURE
    assert tz.BELGRADE_TODAY_SQL in MESSAGES_FUTURE and tz.BELGRADE_TODAY_SQL in EVENTS_FUTURE
