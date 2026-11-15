"""Ежедневный пуш «сегодня из вашего избранного».

* День и время считаются по Белграду (config push.time / push.catchup_until, режим push.mode: off | shadow | on).
* Пост в избранном → строка с началом текста и ссылкой «Открыть пост»; событие сайта → название-ссылка на source_url.
* Длинный пуш делится на несколько сообщений (лимит Telegram 4096), кнопки — под последним.
* Журнал push_log: повторно за день не шлём, видим доставку, заблокировавших пропускаем неделю.
* После рассылки админу уходит сводка. Догоняющий запуск при старте бота досылает тем, кому не ушло."""
import asyncio
import re
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from loguru import logger
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, MessageEntity
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from bot.formatters.event_card import format_date_ru
from bot.utils.links import bot_card_url, post_url
from collector.entities.utf16 import utf16_len
from db import push_log
from db.tables import Tables
from shared.config import config

SEND_DELAY = config['alerts']['send_delay_seconds']      # ~0.04 сек = 25 сообщ/сек
RETRY_PAUSE = 30                                          # пауза перед повтором для временных сбоев, сек
TG_LIMIT = 4096
VENUE_LIMIT = 80
BELGRADE = ZoneInfo('Europe/Belgrade')
OPEN_FAVORITES = 'alert:fav'
CLOSE = 'alert:dismiss'

_lock = asyncio.Lock()
_WS = re.compile(r'\s+')


@dataclass
class Item:
    kind: str            # 'm' пост | 'e' событие
    text: str            # пост: текст; событие: название
    venue: str = ''
    url: str = ''        # событие: билеты (source_url); пост: оригинал в канале ('' — ссылки нет)
    ref: int | None = None   # пост: surrogate_id — по нему ссылка ведёт на карточку в боте


def belgrade_today() -> date:
    return datetime.now(BELGRADE).date()


def shorten(text: str, limit: int) -> str:
    """Одна строка без переносов; если длиннее limit — по границе слова и «…»."""
    text = _WS.sub(' ', text or '').strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(' ')
    if space >= limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(' ,.;:!?-–—(') + '…'


def _block(i: int, item: Item, max_chars: int, bot_username: str | None = None) -> tuple[str, list[tuple[int, int, str]]]:
    """Текст одного пункта и ссылки в нём: (смещение, длина, url) в UTF-16."""
    prefix = f'{i}. '
    links: list[tuple[int, int, str]] = []
    if item.kind == 'e':
        title = shorten(item.text, max_chars) or '(без названия)'
        text = prefix + title
        if item.url:
            links.append((utf16_len(prefix), utf16_len(title), item.url))
        venue = shorten(item.venue, VENUE_LIMIT)
        if venue:
            text += f' — {venue}'
        return text, links
    body = shorten(item.text, max_chars) or '(без текста)'
    text = prefix + body
    # пост: ссылка на карточку в избранном (бот); нет имени бота — оригинал в канале
    if bot_username and item.ref:
        url, label = bot_card_url(bot_username, item.ref), 'Открыть в боте'
    else:
        url, label = item.url, 'Открыть пост'
    if url:
        text += ' · '
        links.append((utf16_len(text), utf16_len(label), url))
        text += label
    return text, links


def build_messages(items: list[Item], day: date, today: date | None = None, max_chars: int | None = None,
                   header: str | None = None, bot_username: str | None = None) -> list[tuple[str, list[MessageEntity]]]:
    """Пуш → список сообщений (текст, ссылки). Заголовок в первом; нумерация сквозная."""
    cfg = config['push']
    max_chars = max_chars or cfg['max_chars']
    head = (header or cfg['header']).format(date=format_date_ru(day, today or day))
    blocks = [_block(i, it, max_chars, bot_username) for i, it in enumerate(items, 1)]

    parts: list[tuple[str, list[MessageEntity]]] = []
    text, ents = head, []

    def flush():
        nonlocal text, ents
        parts.append((text, ents))
        text, ents = '', []

    for btext, links in blocks:
        sep = '\n\n'
        if text and utf16_len(text) + utf16_len(sep) + utf16_len(btext) > TG_LIMIT:
            flush()
        if text:
            text += sep
        base = utf16_len(text)
        text += btext
        ents.extend(MessageEntity(type=MessageEntity.TEXT_LINK, offset=base + off, length=ln, url=url)
                    for off, ln, url in links)
    if text:
        flush()
    return parts


async def bot_username(bot: Bot) -> str | None:
    """Имя бота для ссылок на карточки. Нет имени — пуш не ломается, ссылки постов ведут на оригинал в канале."""
    try:
        name = bot.username
    except Exception:
        name = None
    if not (isinstance(name, str) and name):
        try:
            name = (await bot.get_me()).username
        except Exception as e:
            logger.warning(f"PUSH_BOT_USERNAME_FAILED | {type(e).__name__}: {e}")
            return None
    return name if isinstance(name, str) and name else None


def keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('⭐ В избранное', callback_data=OPEN_FAVORITES)],
        [InlineKeyboardButton('Закрыть', callback_data=CLOSE)],
    ])


_SELECT_FAV = f"""
SELECT f.user_id, 0 AS ord, m.created_at AS c, COALESCE(m.text, '') AS title, ''::text AS venue,
       m.surrogate_id AS sid, m.id AS tg_id, m.channel_id, ch.username, ''::text AS url
FROM {Tables.FAVORITES} f
JOIN {Tables.MESSAGES} m ON m.surrogate_id = f.msg_id
LEFT JOIN {Tables.CHANNELS} ch ON ch.channel_id = m.channel_id
WHERE m.date = $1
  AND m.hidden_by_admin = FALSE
  AND ($2::bigint IS NULL OR f.user_id = $2)
  AND m.surrogate_id NOT IN (SELECT msg_id FROM {Tables.HIDDEN_EVENTS} WHERE user_id = f.user_id)
UNION ALL
SELECT ef.user_id, 1, e.created_at, e.title, COALESCE(e.venue, ''), NULL::bigint, NULL::bigint, NULL::bigint, NULL::text,
       COALESCE(e.source_url, '')
FROM {Tables.EVENT_FAVORITES} ef
JOIN {Tables.EVENTS} e ON e.id = ef.event_id
WHERE e.starts_at = $1
  AND e.hidden_by_admin = FALSE
  AND ($2::bigint IS NULL OR ef.user_id = $2)
  AND e.id NOT IN (SELECT event_id FROM {Tables.EVENT_HIDDEN} WHERE user_id = ef.user_id)
ORDER BY user_id, ord, c
"""


def _item(r) -> Item:
    if r['ord'] == 0:
        return Item('m', r['title'], '', post_url(r['username'], r['channel_id'], r['tg_id']), ref=r['sid'])
    return Item('e', r['title'], r['venue'], r['url'])


async def fetch_items(pool, day: date, user_id: int | None = None) -> dict[int, list[Item]]:
    """Избранное пользователей на дату day: посты раньше событий; скрытое админом или самим пользователем — нет."""
    out: dict[int, list[Item]] = {}
    for r in await pool.fetch(_SELECT_FAV, day, user_id):
        out.setdefault(r['user_id'], []).append(_item(r))
    return out


def _seconds(value) -> float:
    return value.total_seconds() if hasattr(value, 'total_seconds') else float(value)


async def _send_part(bot: Bot, user_id: int, text: str, entities, markup) -> None:
    await bot.send_message(
        chat_id=user_id, text=text, entities=entities or None, reply_markup=markup,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def deliver(bot: Bot, user_id: int, parts) -> tuple[str, int, str | None]:
    """(status, отправлено сообщений, ошибка). status: sent | blocked | failed."""
    sent = 0
    for n, (text, ents) in enumerate(parts):
        markup = keyboard() if n == len(parts) - 1 else None
        try:
            try:
                await _send_part(bot, user_id, text, ents, markup)
            except RetryAfter as e:
                logger.warning(f"PUSH_RETRY | user_id={user_id} | wait={e.retry_after}s")
                await asyncio.sleep(_seconds(e.retry_after) + 1)
                await _send_part(bot, user_id, text, ents, markup)
            sent += 1
        except Forbidden as e:
            return 'blocked', sent, f'{type(e).__name__}: {e}'
        except BadRequest as e:
            gone = 'chat not found' in str(e).lower() or 'deactivated' in str(e).lower()
            return ('blocked' if gone else 'failed'), sent, f'{type(e).__name__}: {e}'
        except TelegramError as e:
            return 'failed', sent, f'{type(e).__name__}: {e}'
        if n < len(parts) - 1:
            await asyncio.sleep(SEND_DELAY)
    return 'sent', sent, None


async def _notify_admin(bot: Bot, text: str) -> None:
    try:
        await bot.send_message(chat_id=config['telegram']['admin_user_id'], text=text)
    except Exception as e:
        logger.warning(f"PUSH_SUMMARY_FAILED | {type(e).__name__}: {e}")


def _summary(day: date, st: dict, shadow: bool = False) -> str:
    head = f"📬 Пуши за {format_date_ru(day, day)}" + (" (shadow, никому не отправлено)" if shadow else "")
    if shadow:
        return f"{head}: получателей {st['users']}, пунктов {st['items']}"
    lines = [f"{head}: отправлено {st['sent']}"]
    if st['blocked']:
        lines.append(f"заблокировали бота: {st['blocked']}")
    if st['failed']:
        lines.append(f"ошибок: {st['failed']}")
    if st['skipped_blocked']:
        lines.append(f"пропущено (блок за неделю): {st['skipped_blocked']}")
    text = lines[0] + ('; ' + ', '.join(lines[1:]) if len(lines) > 1 else '')
    if st['errors']:
        text += '\n' + '\n'.join(f"• {e}" for e in sorted(st['errors'])[:3])
    return text


async def send_push_notifications(bot: Bot, pool, today: date | None = None) -> dict:
    """Рассылка пушей за сегодняшний день по Белграду. Безопасна к повторному запуску: отправленным не шлёт."""
    cfg = config['push']
    if cfg['mode'] == 'off':
        logger.info("PUSH_OFF")
        return {}
    async with _lock:
        today = today or belgrade_today()
        shadow = cfg['mode'] == 'shadow'
        logger.info(f"PUSH_START | date={today} | mode={cfg['mode']}")
        by_user = await fetch_items(pool, today)
        st = {'users': len(by_user), 'items': sum(len(v) for v in by_user.values()),
              'sent': 0, 'blocked': 0, 'failed': 0, 'skipped_blocked': 0, 'errors': set()}
        if not by_user:
            logger.info("PUSH_SKIP | no favorites for today")
            return st
        if shadow:
            logger.info(f"PUSH_SHADOW | users={st['users']} items={st['items']}")
            await _notify_admin(bot, _summary(today, st, shadow=True))
            return st

        done = await push_log.sent_users(pool, today)
        blocked = await push_log.recently_blocked(pool, today)
        todo = [u for u in by_user if u not in done]
        logger.info(f"PUSH_USERS | total={len(by_user)} already_sent={len(done & set(by_user))} todo={len(todo)}")

        username = await bot_username(bot)

        async def attempt(user_id: int) -> tuple[str, int, int, str | None]:
            parts = build_messages(by_user[user_id], today, today, bot_username=username)
            status, n, err = await deliver(bot, user_id, parts)
            if status == 'failed' and n:
                # часть сообщений уже ушла: повтор прислал бы дубль, считаем доставленным
                status, err = 'sent', f'partial {n}/{len(parts)}: {err}'
            return status, n, len(parts), err

        async def finish(user_id: int, status: str, n: int, err: str | None) -> None:
            st[status] += 1
            if status == 'failed':
                st['errors'].add((err or '')[:120])
            try:
                await push_log.record(pool, today, user_id, status, len(by_user[user_id]), max(n, 1), err)
            except Exception as e:
                logger.error(f"PUSH_LOG_FAILED | user_id={user_id} | {type(e).__name__}: {e}")
            logger.info(f"PUSH_{status.upper()} | user_id={user_id} | items={len(by_user[user_id])}"
                        + (f" | {err}" if err else ""))

        retry: list[int] = []
        for user_id in todo:
            if user_id in blocked:
                st['skipped_blocked'] += 1
                continue
            status, n, _, err = await attempt(user_id)
            if status == 'failed':
                retry.append(user_id)
            else:
                await finish(user_id, status, n, err)
            await asyncio.sleep(SEND_DELAY)

        if retry:
            logger.warning(f"PUSH_RETRY_PASS | users={len(retry)} | in {RETRY_PAUSE}s")
            await asyncio.sleep(RETRY_PAUSE)
            for user_id in retry:
                status, n, _, err = await attempt(user_id)
                await finish(user_id, status, n, err)
                await asyncio.sleep(SEND_DELAY)

        logger.info(f"PUSH_DONE | sent={st['sent']} blocked={st['blocked']} failed={st['failed']} "
                    f"skipped_blocked={st['skipped_blocked']} total={st['users']}")
        if st['sent'] or st['blocked'] or st['failed']:
            await _notify_admin(bot, _summary(today, st))
        return st


async def catch_up_push(bot: Bot, pool) -> None:
    """При старте бота: если время рассылки сегодня уже прошло, а окно догоняющей отправки не закончилось —
    досылаем тем, кому пуш не ушёл (отправленным не шлём)."""
    cfg = config['push']
    if cfg['mode'] != 'on':
        return
    now = datetime.now(BELGRADE).strftime('%H:%M')
    if not (cfg['time'] <= now < cfg['catchup_until']):
        logger.info(f"PUSH_CATCHUP_SKIP | now={now} window={cfg['time']}-{cfg['catchup_until']}")
        return
    logger.info(f"PUSH_CATCHUP | now={now}")
    await send_push_notifications(bot, pool)


async def send_preview(bot: Bot, pool, admin_id: int) -> str:
    """Пуш админу из его избранного: на сегодня, иначе на ближайший день; если избранное пусто — образец из событий дня.
    Возвращает строку-итог для ответа админу. В push_log ничего не пишется."""
    today = belgrade_today()
    items = (await fetch_items(pool, today, admin_id)).get(admin_id)
    day, note = today, 'так увидят пользователи, у которых избранное на сегодня'
    if not items:
        nearest = await pool.fetchval(f"""
            SELECT min(d) FROM (
                SELECT m.date AS d FROM {Tables.FAVORITES} f JOIN {Tables.MESSAGES} m ON m.surrogate_id = f.msg_id
                WHERE f.user_id = $1 AND m.date > $2 AND m.hidden_by_admin = FALSE
                UNION ALL
                SELECT e.starts_at FROM {Tables.EVENT_FAVORITES} ef JOIN {Tables.EVENTS} e ON e.id = ef.event_id
                WHERE ef.user_id = $1 AND e.starts_at > $2 AND e.hidden_by_admin = FALSE) x""", admin_id, today)
        if nearest:
            day = nearest
            items = (await fetch_items(pool, day, admin_id)).get(admin_id)
            note = f'в вашем избранном на сегодня пусто, показан ближайший день с ⭐'
    if not items:
        day = today
        demo = await _demo_items(pool, today)
        items, note = demo, 'в вашем избранном пусто, показан образец из событий сегодняшнего дня'
    if not items:
        return 'Показать нечего: в избранном пусто и сегодня нет событий.'
    parts = build_messages(items, day, today, bot_username=await bot_username(bot))
    await bot.send_message(chat_id=admin_id, text=f'🧪 Предпросмотр пуша: {note}. Сообщений: {len(parts)}.')
    status, n, err = await deliver(bot, admin_id, parts)
    return 'Отправлено ✅' if status == 'sent' else f'Не удалось отправить: {err}'


async def _demo_items(pool, day: date) -> list[Item]:
    rows = await pool.fetch(f"""
        (SELECT 0 AS ord, COALESCE(m.text, '') AS title, ''::text AS venue, m.surrogate_id AS sid, m.id AS tg_id, m.channel_id, ch.username,
                ''::text AS url
         FROM {Tables.MESSAGES} m LEFT JOIN {Tables.CHANNELS} ch ON ch.channel_id = m.channel_id
         WHERE m.date = $1 AND m.hidden_by_admin = FALSE AND m.position = 0 ORDER BY m.created_at LIMIT 2)
        UNION ALL
        (SELECT 1, e.title, COALESCE(e.venue, ''), NULL::bigint, NULL::bigint, NULL::bigint, NULL::text, COALESCE(e.source_url, '')
         FROM {Tables.EVENTS} e WHERE e.starts_at = $1 AND e.hidden_by_admin = FALSE ORDER BY e.created_at LIMIT 3)
    """, day)
    return [_item(r) for r in rows]
