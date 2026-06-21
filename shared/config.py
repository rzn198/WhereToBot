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

    return cfg

config = load_config()