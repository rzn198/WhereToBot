-- 015: журнал ежедневных пушей «сегодня из вашего избранного».
-- Одна строка = один пользователь за один день. По ней: не отправляем дважды (перезапуск бота, догоняющий запуск),
-- видим доставку (sent / blocked / failed), раз в неделю пробуем заблокировавших снова. Текст пуша не хранится.
-- Записи старше 30 дней удаляет ежедневная очистка. Безопасно выполнять повторно.

CREATE TABLE IF NOT EXISTS push_log (
    push_date   DATE         NOT NULL,
    user_id     BIGINT       NOT NULL,
    status      TEXT         NOT NULL CHECK (status IN ('sent', 'blocked', 'failed')),
    items       INT          NOT NULL DEFAULT 0,
    parts       INT          NOT NULL DEFAULT 1,
    error       TEXT,
    sent_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (push_date, user_id)
);
CREATE INDEX IF NOT EXISTS idx_push_log_user_status ON push_log (user_id, status, push_date);
