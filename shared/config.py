import os
import yaml
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent


def load_config() -> dict:
    env = os.getenv('APP_ENV', 'production')

    if env == 'local':
        env_file = ROOT / '.env.local'
        if not env_file.exists():
            raise FileNotFoundError(
                ".env.local не найден. "
                "Скопируй .env.example в .env.local и заполни."
            )
        load_dotenv(env_file)
    else:
        load_dotenv(ROOT / '.env')

    with open(ROOT / 'config.yaml', 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f)

    cfg['env'] = env
    cfg['is_local'] = env == 'local'
    cfg['telegram'] = {
        'api_id':           int(os.getenv('TELEGRAM_API_ID', 0)),
        'api_hash':         os.getenv('TELEGRAM_API_HASH', ''),
        'bot_token':        os.getenv('TELEGRAM_BOT_TOKEN', ''),
        'channels':         os.getenv('TELEGRAM_CHANNELS', '').split(','),
        'admin_user_id':    int(os.getenv('ADMIN_USER_ID', 0)),
        'storage_channel':  int(os.getenv('STORAGE_CHANNEL_ID') or 0),
    }
    cfg['db'] = {
        'host':     os.getenv('DB_HOST', 'localhost'),
        'port':     int(os.getenv('DB_PORT', 5432)),
        'name':     os.getenv('DB_NAME', 'eventbot_local'),
        'user':     os.getenv('DB_USER', 'postgres'),
        'password': os.getenv('DB_PASSWORD', ''),
    }

    yaml_oc = cfg.get('opencode', {}) or {}
    cfg['opencode'] = {
        'base_url': yaml_oc.get('base_url', 'https://opencode.ai/zen/go/v1'),
    }

    cfg['translation'] = _translation_section(cfg.get('translation') or {}, cfg['opencode'])
    cfg['parsing'] = _parsing_section(cfg.get('parsing') or {}, cfg['opencode'])
    cfg['deduplication'] = _dedup_section(cfg.get('deduplication') or {}, cfg['opencode'])
    cfg['push'] = _push_section(cfg.get('push') or {}, cfg.get('scheduler') or {})
    cfg['event_categories'] = _event_categories_section((cfg.get('event_collector') or {}).get('categories') or {}, cfg['opencode'])

    return cfg


PUSH_MODES = ('off', 'shadow', 'on')
PUSH_TZ = 'Europe/Belgrade'
PUSH_HEADER = '📅 Сегодня, {date}. Из вашего избранного:'


def _hhmm(value, default: str) -> str:
    """'9:5' / '09:00' → '09:00'; мусор → default."""
    try:
        h, m = str(value).strip().split(':')
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return f'{h:02d}:{m:02d}'
    except (ValueError, AttributeError):
        pass
    return default


def _word(value, default: str) -> str:
    """Режим из YAML/окружения как строка. YAML 1.1 читает голые on/off/yes/no как True/False: `mode: off` без этой
    функции превращался в 'on' (False or 'on'), а `mode: on` падал на .strip(). True → 'on', False → 'off'."""
    if value is True:
        return 'on'
    if value is False:
        return 'off'
    return (str(value).strip().lower() if value not in (None, '') else default)


def _push_section(y: dict, sched: dict) -> dict:
    """Ежедневный пуш «сегодня из вашего избранного».
    mode: off (не запускается) | shadow (только лог, пользователям ничего не уходит) | on. Переопределяется PUSH_MODE.
    time / catchup_until — по времени Белграда. max_chars — длина текста поста в строке пуша.
    Время по умолчанию берётся из прежнего scheduler.push_alert_time."""
    env = os.getenv('PUSH_MODE')
    mode = _word(env if env else y.get('mode'), 'on')
    return {
        'mode': mode if mode in PUSH_MODES else 'on',
        'time': _hhmm(y.get('time') or sched.get('push_alert_time'), '09:00'),
        'catchup_until': _hhmm(y.get('catchup_until'), '20:00'),
        'max_chars': min(1000, max(50, int(y.get('max_chars', 200)))),
        'header': str(y.get('header') or PUSH_HEADER),
        'timezone': PUSH_TZ,
    }


EVENT_CATEGORY_METHODS = ('source', 'shadow', 'ai')
EVENT_CATEGORY_SCOPES = ('unreliable', 'all')


def _event_categories_section(y: dict, oc: dict) -> dict:
    """Категории событий сайтов (event_collector.categories).
    method: source (как отдаёт скрейпер, AI не вызывается) | shadow (AI считается и сравнивается, в БД прежнее) | ai.
    scope:  unreliable (только события, где скрейпер не получил категорию с сайта: подстановка, словарь, пусто)
            | all (все события).
    Ключ: OPENCODE_EVENTS_API_KEY, если не задан — OPENCODE_PARSING_API_KEY."""
    method = _word(os.getenv('EVENT_CATEGORY_METHOD') or y.get('method'), 'source')
    scope = _word(os.getenv('EVENT_CATEGORY_SCOPE') or y.get('scope'), 'unreliable')
    return {
        'method': method if method in EVENT_CATEGORY_METHODS else 'source',
        'scope': scope if scope in EVENT_CATEGORY_SCOPES else 'unreliable',
        'models': list(y.get('models') or []),
        'base_url': y.get('base_url') or oc['base_url'],
        'api_key': os.getenv('OPENCODE_EVENTS_API_KEY') or os.getenv('OPENCODE_PARSING_API_KEY', ''),
        'timeout': float(y.get('timeout', 60)),
        'temperature': (float(y['temperature']) if y.get('temperature') is not None else None),
        'max_chars': int(y.get('max_chars', 1200)),
        'concurrency': max(1, int(y.get('concurrency', 3))),
        'max_calls_per_run': int(y.get('max_calls_per_run', 600)),
    }


TRANSLATION_METHODS = ('off', 'shadow', 'on')


def _translation_section(y: dict, oc: dict) -> dict:
    """Перевод описаний событий (запускается из сервиса deduplicator после его прогона). Ключ: OPENCODE_TRANSLATE_API_KEY (запасной — прежний OPENCODE_API_KEY).
    method: off | shadow (только лог) | on. sources — значения events.source, которые переводим."""
    method = _word(os.getenv('TRANSLATION_METHOD') or y.get('method'), 'off')
    sources = y.get('sources') or []
    if isinstance(sources, str):
        sources = sources.split(',')
    return {
        'method': method if method in TRANSLATION_METHODS else 'off',
        'sources': [str(s).strip().lower() for s in sources if str(s).strip()],
        'models': list(y.get('models') or []),
        'base_url': y.get('base_url') or oc['base_url'],
        'api_key': os.getenv('OPENCODE_TRANSLATE_API_KEY') or os.getenv('OPENCODE_API_KEY', ''),
        'timeout': float(y.get('timeout', 60)),
        'max_chars': int(y.get('max_chars', 600)),
        'input_chars': int(y.get('input_chars', 1500)),
        'concurrency': int(y.get('concurrency', 3)),
        'only_upcoming': bool(y.get('only_upcoming', True)),
        'retry_minutes': float(y.get('retry_minutes', 30)),
        'max_retries': int(y.get('max_retries', 3)),
        'run_on_start': bool(y.get('run_on_start', True)),
        'alerts': bool(y.get('alerts', True)),          # сообщения админу о сбоях перевода (один раз на эпизод)
    }


def _parsing_section(y: dict, oc: dict) -> dict:
    """AI-парсинг дат и категорий messages. Отдельный ключ: OPENCODE_PARSING_API_KEY."""
    return {
        'date_method': _word(os.getenv('PARSING_DATE_METHOD') or y.get('date_method'), 'regex'),
        'category_method': _word(os.getenv('PARSING_CATEGORY_METHOD') or y.get('category_method'), 'dict'),
        'ai_null_date_fallback': bool(y.get('ai_null_date_fallback', True)),
        'ai_empty_categories_fallback': bool(y.get('ai_empty_categories_fallback', True)),
        'ai_trust_non_event': bool(y.get('ai_trust_non_event', False)),
        'max_chars': int(y.get('max_chars', 2000)),
        'max_calls_per_run': int(y.get('max_calls_per_run', 1000)),
        'concurrency': max(1, int(y.get('concurrency', 4))),
        'seen_retention_days': max(1, int(y.get('seen_retention_days', 60))),
        'timeout': float(y.get('timeout', 60)),
        'temperature': (float(y['temperature']) if y.get('temperature') is not None else None),
        'base_url': y.get('base_url') or oc['base_url'],
        'models': list(y.get('models') or []),
        'api_key': os.getenv('OPENCODE_PARSING_API_KEY', ''),
    }


DEDUP_METHODS = ('legacy', 'shadow', 'ai')


def _dedup_priority(raw) -> list[str]:
    """Единый список источников сверху вниз. Прежняя форма {messages: [...], events: [...]} тоже принимается."""
    if isinstance(raw, dict):
        return [str(x) for x in (raw.get('messages') or [])] + \
               [x if str(x).startswith('event:') else f"event:{x}" for x in (raw.get('events') or [])]
    return [str(x) for x in (raw or [])]


def _flag(env_value, default: bool) -> bool:
    """Булев флаг из переменной окружения (1/0, true/false, on/off); пусто — значение из конфига."""
    if env_value is None or str(env_value).strip() == '':
        return bool(default)
    return str(env_value).strip().lower() in ('1', 'true', 'yes', 'on')


def _dedup_audit(y: dict) -> dict:
    """Журнал решений дедупликатора (logs/dedup_audit.jsonl). Выключатель: audit.enabled или DEDUP_AUDIT=0/1."""
    a = y.get('audit') or {}
    return {
        'enabled': _flag(os.getenv('DEDUP_AUDIT'), a.get('enabled', True)),
        'max_text_chars': int(a.get('max_text_chars', 4000)),
        'rotation_mb': int(a.get('rotation_mb', 50)),
        'retention_days': int(a.get('retention_days', 90)),
        'raw_answers': bool(a.get('raw_answers', True)),
    }


def _dedup_section(y: dict, oc: dict) -> dict:
    """Прежние ключи секции (flag_threshold) сохраняются; AI-дедуп — в подсекции `ai`.
    method: legacy (старый хук) | shadow (AI предлагает, ничего не скрывается) | ai (AI скрывает дубли).
    Отдельный ключ: OPENCODE_DEDUP_API_KEY."""
    ai = y.get('ai') or {}
    out = dict(y)
    method = _word(os.getenv('DEDUP_METHOD') or y.get('method'), 'legacy')
    if method not in DEDUP_METHODS:
        method = 'legacy'
    out['method'] = method
    out['audit'] = _dedup_audit(y)
    out['history_days'] = max(1, int(y.get('history_days', 60)))   # сколько хранить решения, прогоны и отпечатки дат
    out['ai'] = {
        'base_url': ai.get('base_url') or oc['base_url'],
        'models': list(ai.get('models') or []),
        'api_key': os.getenv('OPENCODE_DEDUP_API_KEY', ''),
        'timeout': float(ai.get('timeout', 120)),
        'extra_payload': dict(ai.get('extra_payload') or {}),
        'cooldown_seconds': float(ai.get('cooldown_seconds', 300)),
        'priority': _dedup_priority(ai.get('priority')),
        'max_records_per_request': int(ai.get('max_records_per_request', 80)),
        'max_chars_per_record': int(ai.get('max_chars_per_record', 300)),
        'concurrency': int(ai.get('concurrency', 3)),
        'max_requests_per_run': int(ai.get('max_requests_per_run', 300)),
        'max_requests_per_day': int(ai.get('max_requests_per_day', 21)),
        'horizon_days': int(ai.get('horizon_days', 365)),
        'hide_exact_copies': bool(ai.get('hide_exact_copies', True)),
    }
    return out


config = load_config()