import pytest

from shared.config import _push_section


@pytest.mark.parametrize('raw,expected', [
    (True, 'on'),          # YAML: mode: on
    (False, 'off'),        # YAML: mode: off (раньше превращалось в 'on')
    ('shadow', 'shadow'), (' Shadow ', 'shadow'), (None, 'on'), ('', 'on'), ('мусор', 'on'),
])
def test_push_mode_handles_yaml_booleans(raw, expected, monkeypatch):
    monkeypatch.delenv('PUSH_MODE', raising=False)
    assert _push_section({'mode': raw}, {})['mode'] == expected


def test_env_overrides_yaml(monkeypatch):
    monkeypatch.setenv('PUSH_MODE', 'shadow')
    assert _push_section({'mode': False}, {})['mode'] == 'shadow'


from shared.config import _translation_section, _event_categories_section


@pytest.mark.parametrize('raw,expected', [(True, 'on'), (False, 'off'), ('shadow', 'shadow'), (' ON ', 'on'), (None, 'off')])
def test_translation_method_handles_yaml_booleans(raw, expected, monkeypatch):
    monkeypatch.delenv('TRANSLATION_METHOD', raising=False)
    assert _translation_section({'method': raw}, {'base_url': 'u'})['method'] == expected


def test_translation_method_true_does_not_crash_other_sections(monkeypatch):
    for k in ('EVENT_CATEGORY_METHOD', 'EVENT_CATEGORY_SCOPE'):
        monkeypatch.delenv(k, raising=False)
    sec = _event_categories_section({'method': True, 'scope': False}, {'base_url': 'u'})
    assert sec['method'] == 'source' and sec['scope'] == 'unreliable'      # on/off для них не режимы → значения по умолчанию
