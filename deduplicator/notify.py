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


def _snippet(text: str, n: int = 60) -> str:
    s = ' '.join((text or '').split())
    return s if len(s) <= n else s[:n] + '…'


def run_report(res, method: str, resolver: PriorityResolver | None = None) -> str | None:
    """Итог успешного прогона; None — если сообщать нечего (ничего не изменилось)."""
    st = res.stats
    plans = res.plans
    resolver = resolver or res.resolver or PriorityResolver()
    changes = st.get('hidden', 0) + st.get('unhidden', 0)
    proposed = sum(len(p.wanted) for p in plans)
    tail = []
    if st.get('bad_days'):
        tail.append(f"⚠️ плохих ответов ИИ: {st['bad_days']} (эти даты повторятся)")
    if st.get('skipped_days'):
        tail.append(f"⚠️ лимит запросов: пропущено дат {st['skipped_days']} (догоним в следующий раз)")
    if method == 'ai':
        if not changes and not tail:
            return None
        head = f"🧩 Дедупликатор: скрыто {st.get('hidden', 0)}, возвращено {st.get('unhidden', 0)}"
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
