"""scripts/category_shadow_check.py: аргументы, группировка одинаковых текстов, сравнение, CSV и сводка (AI подменён)."""
import asyncio
import csv
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import category_shadow_check as cs  # noqa: E402


def _row(i, title, cats, desc='Описание', day=date(2026, 12, 1), venue='V'):
    return {'id': i, 'source': 'polako', 'source_url': f'u{i}', 'title': title, 'venue': venue,
            'description': desc, 'event_types': cats, 'starts_at': day}


def test_args_validation():
    a = cs.parse_args(['--sources', 'polako', '--ids', '1,2', '--dry-run'])
    assert a.sources == 'polako' and a.dry_run and a.max_calls == cs.DEFAULT_MAX_CALLS
    with pytest.raises(SystemExit):
        cs.parse_args(['--ids', 'x'])
    with pytest.raises(SystemExit):
        cs.parse_args(['--from-date', '25.11.2026'])


def test_same_text_on_other_dates_is_asked_once():
    rows = [_row(1, 'A', ['Концерты'], day=date(2026, 12, 1)), _row(2, 'A', ['Концерты'], day=date(2026, 12, 2)),
            _row(3, 'B', ['Концерты'])]
    g = cs.group_rows(rows, 1200)
    assert len(g) == 2 and sorted(len(v) for v in g.values()) == [1, 2]


def test_compare():
    assert cs.compare(['Игры и развлечения'], ['Выставки/Ярмарки']) == (False, False)
    assert cs.compare(['Концерты', 'Фестивали'], ['Концерты']) == (True, False)
    assert cs.compare(['Концерты', 'Фестивали'], ['Фестивали', 'Концерты']) == (False, True)
    assert cs.compare([], ['Концерты']) == (False, False)


def test_csv_and_summary(tmp_path):
    rows = [_row(1, 'Выставка', ['Игры и развлечения']), _row(2, 'Выставка', ['Игры и развлечения'], day=date(2026, 12, 2)),
            _row(3, 'Рок', ['Концерты']), _row(4, 'Сбой', ['Концерты'], desc='x')]
    res = {cs.text_key(rows[0], 1200): {'ai': ['Выставки/Ярмарки'], 'model': 'm', 'error': ''},
           cs.text_key(rows[2], 1200): {'ai': ['Концерты'], 'model': 'm', 'error': ''},
           cs.text_key(rows[3], 1200): {'ai': [], 'model': 'm', 'error': 'плохой ответ'}}
    p = tmp_path / 'o.csv'
    out = cs.write_csv(str(p), rows, res, 1200)
    got = list(csv.DictReader(open(p, encoding='utf-8-sig')))
    assert [g['first_same'] for g in got] == ['НЕТ', 'НЕТ', 'да', ''] and got[3]['error'] == 'плохой ответ'
    s = cs.summary(out)
    assert 'с ответом AI: 3' in s and 'Игры и развлечения → Выставки/Ярмарки' in s and 'ошибок/без ответа: 1' in s


def test_ask_all_with_fake_client_and_caps():
    class Fake:
        available = True
        current_model = 'fake'
        def begin_run(self): pass
        def report_ok(self): pass
        def report_bad_output(self): pass
        async def complete(self, system, user):
            assert 'Название: ' in user and 'Категория на сайте: —' in user
            return '{"categories": ["Выставки/Ярмарки"]}'
    rows = [_row(1, 'A', ['Игры и развлечения']), _row(2, 'B', ['Игры и развлечения']), _row(3, 'C', ['Игры и развлечения'])]
    g = cs.group_rows(rows, 1200)
    allowed = ['Выставки/Ярмарки', 'Игры и развлечения', 'Концерты']
    r = asyncio.run(cs.ask_all(g, Fake(), allowed, 1200, 2, 1))
    assert sorted(v['ai'] and v['ai'][0] or v['error'] for v in r.values()) == ['Выставки/Ярмарки', 'Выставки/Ярмарки', 'лимит запросов']
