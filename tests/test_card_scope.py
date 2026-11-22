"""Пробный запуск: card_lines.horizon_days и card_lines.only_profiles."""
from datetime import date, timedelta

from card_lines.select import select_candidates
from card_lines.settings import card_line_settings

TODAY = date(2026, 11, 23)


class FakePool:
    def __init__(self, rows):
        self.rows, self.args = rows, None

    async def fetch(self, sql, *args):
        self.args = args
        return self.rows


def row(i, types):
    return {'kind': 'e', 'id': i, 'title': f'Событие {i}', 'body': 'Описание ' * 10, 'venue': '', 'event_types': types,
            'd': TODAY, 'src_hash': None, 'hook_status': None, 'cl_updated': None}


def test_settings_parse():
    d = card_line_settings({})
    assert d.horizon_days is None and d.only_profiles == ()
    s = card_line_settings({'horizon_days': 1, 'only_profiles': ['Концерты']})
    assert s.horizon_days == 1 and s.only_profiles == ('Концерты',)
    assert card_line_settings({'horizon_days': 0}).horizon_days == 0
    assert card_line_settings({'horizon_days': 'x'}).horizon_days is None
    assert card_line_settings({'only_profiles': 'Концерты'}).only_profiles == ('Концерты',)


async def test_only_profiles_filters_candidates():
    pool = FakePool([row(1, ['Концерты']), row(2, ['Кино/Театр']), row(3, ['Музеи'])])
    st = card_line_settings({'method': 'on', 'only_profiles': ['Концерты']})
    sel = await select_candidates(pool, st, today=TODAY, horizon_days=1, city='Belgrade')
    assert [c.id for c in sel.todo] == [1]
    st_all = card_line_settings({'method': 'on'})
    sel = await select_candidates(pool, st_all, today=TODAY, horizon_days=1, city='Belgrade')
    assert sorted(c.id for c in sel.todo) == [1, 2]            # Музеи без ИИ
    assert pool.args[1:3] == (TODAY, TODAY + timedelta(days=1))


async def test_run_pass_uses_config_horizon(monkeypatch):
    import card_lines.service as svc
    seen = {}

    async def fake_select(pool, st, *, today, horizon_days, city, include_done=False):
        seen['h'] = horizon_days
        from card_lines.select import Selection
        return Selection()

    class FakeClient:
        def begin_run(self): pass
        async def aclose(self): pass
    monkeypatch.setattr(svc, 'select_candidates', fake_select)
    await svc.run_pass(None, FakeClient(), card_line_settings({'method': 'on', 'horizon_days': 1}), city='Belgrade')
    assert seen['h'] == 1
    monkeypatch.setattr(svc, 'config_horizon_days', lambda: 365)
    await svc.run_pass(None, FakeClient(), card_line_settings({'method': 'on'}), city='Belgrade')
    assert seen['h'] == 365


# ------------------------------------------------------------------ scripts/card_lines_run.py и card_lines.auto

def _script():
    import importlib.util, os, sys
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts', 'card_lines_run.py')
    spec = importlib.util.spec_from_file_location('card_lines_run', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_run_script_builds_settings_from_args():
    mod = _script()
    args = mod.parse_args(['--profiles', 'Концерты, Кино/Театр', '--days', '2', '--hooks', 'on', '--enrich', 'on', '--max-calls', '50'])
    st, over = mod.build_settings(args, {'method': 'off', 'models': ['m1'], 'hooks': {'method': 'off', 'hook_max': 70}})
    assert st.method == 'on' and st.horizon_days == 1 and st.only_profiles == ('Концерты', 'Кино/Театр')
    assert st.hooks.enabled and st.hooks.enrich.enabled and st.hooks.hook_max == 70
    assert st.max_calls_per_run == 50 and not st.after_dedup and over['models'] == ['m1']
    args = mod.parse_args(['--models', 'a, b', '--concurrency', '1'])
    st, over = mod.build_settings(args, {'hooks': {'method': 'on'}})
    assert over['models'] == ['a', 'b'] and st.concurrency == 1 and st.only_profiles == () and st.horizon_days == 1
    assert st.hooks.enabled


def test_run_script_rejects_bad_args():
    import pytest
    mod = _script()
    for bad in (['--days', '0'], ['--start', '23.11.2026'], ['--hooks', 'maybe']):
        with pytest.raises(SystemExit):
            mod.parse_args(bad)


def test_auto_false_stops_service_trigger_but_not_run_pass():
    from card_lines.service import CardLinesHook
    hook = CardLinesHook(None, None, card_line_settings({'method': 'on', 'auto': False}))
    assert hook.trigger('start') is False and not hook.running
    assert card_line_settings({'method': 'on'}).auto is True
