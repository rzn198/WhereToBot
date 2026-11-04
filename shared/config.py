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
        'api_key': os.getenv('OPENCODE_API_KEY', ''),
        'base_url': yaml_oc.get('base_url', 'https://opencode.ai/zen/go/v1'),
        'models': yaml_oc.get('models', []),
        'max_chars': int(yaml_oc.get('max_chars', 600)),
    }

    cfg['parsing'] = _parsing_section(cfg.get('parsing') or {}, cfg['opencode'])
    cfg['deduplication'] = _dedup_section(cfg.get('deduplication') or {}, cfg['opencode'])

    return cfg


def _parsing_section(y: dict, oc: dict) -> dict:
    """AI-парсинг дат и категорий messages. Отдельный ключ: OPENCODE_PARSING_API_KEY."""
    return {
        'date_method': (os.getenv('PARSING_DATE_METHOD') or y.get('date_method') or 'regex').strip().lower(),
        'category_method': (os.getenv('PARSING_CATEGORY_METHOD') or y.get('category_method') or 'dict').strip().lower(),
        'ai_null_date_fallback': bool(y.get('ai_null_date_fallback', True)),
        'ai_empty_categories_fallback': bool(y.get('ai_empty_categories_fallback', True)),
        'ai_trust_non_event': bool(y.get('ai_trust_non_event', False)),
        'max_chars': int(y.get('max_chars', 2000)),
        'max_calls_per_run': int(y.get('max_calls_per_run', 1000)),
        'timeout': float(y.get('timeout', 60)),
        'temperature': (float(y['temperature']) if y.get('temperature') is not None else None),
        'base_url': y.get('base_url') or oc['base_url'],
        'models': list(y.get('models') or []),
        'api_key': os.getenv('OPENCODE_PARSING_API_KEY', ''),
    }


def _dedup_section(y: dict, oc: dict) -> dict:
    """Прежние ключи секции (flag_threshold) сохраняются; AI-дедуп — в подсекции `ai`.
    Отдельный ключ: OPENCODE_DEDUP_API_KEY."""
    ai = y.get('ai') or {}
    prio = ai.get('priority') or {}
    out = dict(y)
    out['ai'] = {
        'base_url': ai.get('base_url') or oc['base_url'],
        'models': list(ai.get('models') or []),
        'api_key': os.getenv('OPENCODE_DEDUP_API_KEY', ''),
        'timeout': float(ai.get('timeout', 120)),
        'priority': {
            'messages': list(prio.get('messages') or []),
            'events': list(prio.get('events') or []),
        },
    }
    return out


config = load_config()