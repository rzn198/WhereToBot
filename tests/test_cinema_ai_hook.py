"""Конфиг, хук и CSV-выгрузка AI киноафиши."""
import asyncio
import csv
import importlib.util
import os

import pytest

from cinema_ai import hook
from shared import config as cfgmod


def load_script():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts', 'cinema_ai_shadow.py')
    spec = importlib.util.spec_from_file_location('cinema_ai_shadow', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


OC = {'base_url': 'https://x/v1'}
TR = {'models': ['a', 'b']}


def test_config_section_defaults_to_translation_models_and_translate_key(monkeypatch):
    monkeypatch.setenv('OPENCODE_TRANSLATE_API_KEY', 'k-tr')
    monkeypatch.delenv('CINEMA_AI_METHOD', raising=False)
    s = cfgmod._cinema_ai_section({}, OC, TR)
    assert (s['method'], s['models'], s['api_key'], s['base_url'], s['temperature']) == ('off', ['a', 'b'], 'k-tr', 'https://x/v1', 0)


def test_config_section_own_models_and_method_validation(monkeypatch):
    monkeypatch.delenv('CINEMA_AI_METHOD', raising=False)
    s = cfgmod._cinema_ai_section({'method': 'on', 'models': ['z'], 'max_films_per_run': 7}, OC, TR)
    assert s['method'] == 'on' and s['models'] == ['z'] and s['max_films_per_run'] == 7
    assert cfgmod._cinema_ai_section({'method': 'nonsense'}, OC, TR)['method'] == 'off'


def test_env_overrides_method(monkeypatch):
    monkeypatch.setenv('CINEMA_AI_METHOD', 'shadow')
    assert cfgmod._cinema_ai_section({'method': 'on'}, OC, TR)['method'] == 'shadow'


async def test_hook_off_does_nothing_even_without_db():
    await hook.run_cinema_ai(None, {'method': 'off'})          # pool=None: до БД дело не доходит


async def test_hook_swallows_errors(monkeypatch):
    class Boom:
        async def fetch(self, *a, **k):
            raise RuntimeError('db down')
    await hook.run_cinema_ai(Boom(), {'method': 'on', 'api_key': 'k', 'models': ['m']})   # не должно бросать
    assert hook._running is False


def test_trigger_does_nothing_when_off(monkeypatch):
    monkeypatch.setitem(hook.config, 'cinema_ai', {'method': 'off'})
    hook.trigger(None)                                          # без running loop: если бы создал задачу — упало бы
    assert not hook._tasks


def test_write_csvs_splits_by_kind_and_keeps_cyrillic(tmp_path):
    mod = load_script()
    rows = [{'kind': 'title', 'id': 1, 'sessions': 3, 'variants': 'A | B', 'en': 'A', 'ru': 'Фильм', 'same_as': '', 'merge_into': '', 'veto': ''},
            {'kind': 'genre', 'term': 'Akcija', 'genre_ru': 'боевик'}]
    out = mod.write_csvs(rows, str(tmp_path))
    assert len(out) == 2
    with open(tmp_path / 'cinema_ai_titles.csv', encoding='utf-8-sig') as f:
        assert list(csv.DictReader(f))[0]['ru'] == 'Фильм'
    assert not (tmp_path / 'cinema_ai_descriptions.csv').exists()


def test_script_args_reject_apply_with_dry_run():
    with pytest.raises(SystemExit):
        load_script().parse_args(['--apply', '--dry-run'])
