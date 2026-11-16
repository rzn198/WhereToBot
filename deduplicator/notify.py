"""Сообщения админу о работе дедупликатора: итог прогона и начало/конец сбоя ИИ (один раз на эпизод)."""
from __future__ import annotations

from loguru import logger

from deduplicator.priority import PriorityResolver
from deduplicator.schedule import BAD, ServiceSettings

SAMPLES = 8


async def send_admin(text: str) -> bool:
    """Прямая отправка через бота (как у event_collector.health.notify_admin); сбой отправки только в лог."""
    from shared.config import config
    tg = config.get('telegram') or {}
    token, admin_id = tg.get('bot_token'), tg.get('admin_user_id')
    if not token or not admin_id:
        logger.warning("DEDUP_ALERT_SKIPPED | нет TELEGRAM_BOT_TOKEN/ADMIN_USER_ID")
        return False
    try:
        from telegram import Bot
        async with Bot(token) as bot:
            await bot.send_message(chat_id=admin_id, text=text[:4000])
        return True
    except Exception as e:
        logger.error(f"DEDUP_ALERT_ERROR | {e}")
        return False


async def send_document(data: bytes, filename: str, caption: str = '') -> bool:
    """Отправка файла админу (как send_admin); сбой отправки только в лог."""
    from shared.config import config
    tg = config.get('telegram') or {}
    token, admin_id = tg.get('bot_token'), tg.get('admin_user_id')
    if not token or not admin_id:
        logger.warning("DEDUP_FILE_SKIPPED | нет TELEGRAM_BOT_TOKEN/ADMIN_USER_ID")
        return False
    try:
        import io
        from telegram import Bot
        async with Bot(token) as bot:
            await bot.send_document(chat_id=admin_id, document=io.BytesIO(data), filename=filename, caption=caption[:1000])
        return True
    except Exception as e:
        logger.error(f"DEDUP_FILE_ERROR | {filename} | {e}")
        return False


async def send_run_files(pool, res, method: str, send_file=None) -> int:
    """CSV после прогона: изменения прогона, снимок действующих решений и (если были) переносы избранного. Сбой сборки/отправки не ломает сервис.
    Возвращает число отправленных файлов."""
    from datetime import date
    from db import fav_moves_export
    from deduplicator import report_csv
    send_file = send_file or send_document
    stamp = date.today().strftime('%Y%m%d')
    run = res.run_id or 0
    sent = 0
    for name, builder, caption in (
        (f"dedup_run{run}_changes_{stamp}.csv", lambda: report_csv.build_changes(pool, res, method),
         f"Дедупликатор, прогон {run}: изменения этого прогона"),
        (f"dedup_current_{stamp}.csv", lambda: report_csv.build_current(pool),
         "Дедупликатор: все действующие решения (сегодня и позже)"),
        (f"dedup_run{run}_fav_moves_{stamp}.csv", lambda: fav_moves_export.build(pool, res.run_id),
         f"Дедупликатор, прогон {run}: перенос избранного и скрытий пользователей"),
    ):
        try:
            data = await builder()
            if data and await send_file(data, name, caption):
                sent += 1
        except Exception as e:
            logger.error(f"DEDUP_REPORT_CSV_ERROR | {name} | {type(e).__name__}: {e}")
    return sent


def _snippet(text: str, n: int = 60) -> str:
    s = ' '.join((text or '').split())
    return s if len(s) <= n else s[:n] + '…'


def run_report(res, method: str, resolver: PriorityResolver | None = None) -> str | None:
    """Итог успешного прогона; None — если сообщать нечего (ничего не изменилось)."""
    st = res.stats
    plans = res.plans
    resolver = resolver or res.resolver or PriorityResolver()
    changes = st.get('hidden', 0) + st.get('unhidden', 0)
    if st.get('fav_invariant_fixed'):
        tail_fix = f"⚠️ FAV_INVARIANT: у скрытых дублей оставалось избранное, перенесено на победителей: {st['fav_invariant_fixed']}"
    else:
        tail_fix = None
    proposed = sum(len(p.wanted) for p in plans)
    tail = []
    if st.get('bad_days'):
        tail.append(f"⚠️ плохих ответов ИИ: {st['bad_days']} (эти даты повторятся)")
    if st.get('skipped_days'):
        tail.append(f"⚠️ лимит запросов: пропущено дат {st['skipped_days']} (догоним в следующий раз)")
    if tail_fix:
        tail.append(tail_fix)
    if method == 'ai':
        if not changes and not tail:
            return None
        head = f"🧩 Дедупликатор: скрыто {st.get('hidden', 0)}, возвращено {st.get('unhidden', 0)}"
        if st.get('fav_moved'):
            head += f", перенесено избранного/скрытий {st['fav_moved']}"
        pairs = [nd for p in plans for nd in p.insert]
    else:
        if not proposed and not tail:
            return None
        head = f"👀 Дедупликатор (shadow): предложено скрыть {proposed}, ничего не скрыто"
        pairs = [nd for p in plans for nd in p.wanted]
    lines = [head + f" · дат {st.get('dates_processed', 0)}/{st.get('dates_total', 0)} · запросов {st.get('requests', 0)} "
             f"· токенов {st.get('tokens_in', 0)}/{st.get('tokens_out', 0)}"]
    if pairs:
        lines.append("")
        for nd in pairs[:SAMPLES]:
            lines.append(f"• {resolver.label(nd.winner)} ← {resolver.label(nd.loser)}: {_snippet(nd.loser.text)}")
        if len(pairs) > SAMPLES:
            lines.append(f"… и ещё {len(pairs) - SAMPLES}")
    lines += tail
    return '\n'.join(lines)


def transition_text(prev_status: str | None, status: str, error: str, st: ServiceSettings) -> str | None:
    """🔴 при первом сбое подряд, 🟢 при восстановлении; повторные сбои эпизода молчат."""
    bad_now, bad_before = status in BAD, prev_status in BAD
    if bad_now and not bad_before:
        return (f"🔴 Дедупликатор: прогон не выполнен ({status}): {error or 'причина не указана'}\n"
                f"Повтор через {st.retry_minutes:g} мин, до {st.max_retries} раз подряд, затем жду новых данных от коллекторов.")
    if not bad_now and bad_before:
        return "🟢 Дедупликатор снова работает"
    return None


def manual_summary(res, method: str, force: bool) -> str:
    """Итог ручного запуска из админки: сообщаем всегда, даже если ничего не изменилось."""
    kind = 'полный пересчёт' if force else 'изменившиеся даты'
    if res.status != 'ok':
        return f"🔴 Ручной запуск дедупа ({kind}) не выполнен ({res.status}): {res.error or 'причина не указана'}"
    st = res.stats
    return (f"✅ Ручной запуск дедупа ({kind}) завершён · method={method}\n"
            f"Дат {st.get('dates_processed')}/{st.get('dates_total')} · запросов {st.get('requests')} · групп {st.get('groups_found')} · "
            f"скрыто {st.get('hidden')} · возвращено {st.get('unhidden')} · сохранено прежних пар {st.get('sticky', 0)}"
            + (f" · плохих дат {st.get('bad_days')}" if st.get('bad_days') else '')
            + (f" · пропущено по лимиту {st.get('skipped_days')}" if st.get('skipped_days') else ''))
