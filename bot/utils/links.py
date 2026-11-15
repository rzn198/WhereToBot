"""Ссылки на записи: пост в канале и ссылка в бота на карточку."""
import re

FAV_START_RE = re.compile(r'^fav_(\d{1,18})$')


def post_url(username: str | None, channel_id: int | None, tg_id: int | None) -> str:
    """Оригинал поста в канале ('' — собрать ссылку не из чего)."""
    if not tg_id:
        return ''
    if username:
        return f"https://t.me/{username.lstrip('@')}/{tg_id}"
    if channel_id:
        return f"https://t.me/c/{channel_id}/{tg_id}"
    return ''


def bot_card_url(bot_username: str, surrogate_id: int) -> str:
    """Ссылка в бота: по нажатию Telegram присылает боту /start fav_<id>, бот открывает карточку в избранном."""
    return f"https://t.me/{bot_username.lstrip('@')}?start=fav_{int(surrogate_id)}"
