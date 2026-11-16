-- 016: запросы на ручной запуск дедупликатора из админки бота.
-- Бот и deduplicator — разные контейнеры: бот кладёт запрос сюда, сервис deduplicator забирает его на ближайшем такте
-- (раз в service.poll_seconds), когда не работают коллекторы, и запускает прогон. Итог админу приходит сообщением.
-- Одновременно может быть только один открытый запрос (pending или running). Безопасно выполнять повторно.

CREATE TABLE IF NOT EXISTS dedup_requests (
    id            BIGSERIAL    PRIMARY KEY,
    requested_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    requested_by  BIGINT,
    force         BOOLEAN      NOT NULL DEFAULT false,   -- true = пересчитать все даты, игнорируя отпечатки
    status        TEXT         NOT NULL DEFAULT 'pending'
                               CHECK (status IN ('pending', 'running', 'done', 'failed', 'cancelled')),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    run_id        BIGINT       REFERENCES dedup_runs (id) ON DELETE SET NULL,
    error         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_dedup_requests_open ON dedup_requests ((true)) WHERE status IN ('pending', 'running');
