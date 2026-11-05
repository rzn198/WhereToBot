-- 008: AI-дедупликация между messages и events.
-- Безопасно выполнять повторно (IF NOT EXISTS).

-- Скрытие событий сайтов (у messages колонка hidden_by_admin уже есть).
ALTER TABLE events ADD COLUMN IF NOT EXISTS hidden_by_admin BOOLEAN NOT NULL DEFAULT FALSE;

-- Каналы: коллектор записывает сюда числовой id и @username, чтобы иерархию источников
-- в конфиге можно было задавать по имени канала.
CREATE TABLE IF NOT EXISTS channels (
    channel_id  BIGINT       PRIMARY KEY,
    username    TEXT,
    title       TEXT,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Прогоны дедупликатора (статистика и признак «первый прогон в режиме ai»).
CREATE TABLE IF NOT EXISTS dedup_runs (
    id               BIGSERIAL    PRIMARY KEY,
    started_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ,
    method           TEXT         NOT NULL,
    status           TEXT         NOT NULL DEFAULT 'running'
                                  CHECK (status IN ('running', 'ok', 'skipped', 'error')),
    dates_total      INT          NOT NULL DEFAULT 0,
    dates_processed  INT          NOT NULL DEFAULT 0,
    requests         INT          NOT NULL DEFAULT 0,
    tokens_in        BIGINT       NOT NULL DEFAULT 0,
    tokens_out       BIGINT       NOT NULL DEFAULT 0,
    groups_found     INT          NOT NULL DEFAULT 0,
    hidden           INT          NOT NULL DEFAULT 0,
    unhidden         INT          NOT NULL DEFAULT 0,
    error            TEXT
);

-- Решения: кто кого скрыл. kind: 'm' = messages.surrogate_id, 'e' = events.id.
--   proposed — предложение (режим shadow), ничего не скрыто
--   active   — проигравший скрыт этим решением
--   reverted — админ вернул проигравшего: AI больше его не скрывает
--   stale    — устарело (история)
CREATE TABLE IF NOT EXISTS dedup_decisions (
    id           BIGSERIAL    PRIMARY KEY,
    day          DATE         NOT NULL,
    winner_kind  CHAR(1)      NOT NULL CHECK (winner_kind IN ('m', 'e')),
    winner_id    BIGINT       NOT NULL,
    loser_kind   CHAR(1)      NOT NULL CHECK (loser_kind IN ('m', 'e')),
    loser_id     BIGINT       NOT NULL,
    status       TEXT         NOT NULL CHECK (status IN ('proposed', 'active', 'reverted', 'stale')),
    decided_by   TEXT         NOT NULL DEFAULT 'ai' CHECK (decided_by IN ('ai', 'admin')),
    model        TEXT,
    reason       TEXT,
    run_id       BIGINT       REFERENCES dedup_runs (id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);
-- У одного проигравшего не больше одного действующего решения.
CREATE UNIQUE INDEX IF NOT EXISTS uq_dedup_live_loser
    ON dedup_decisions (loser_kind, loser_id) WHERE status IN ('proposed', 'active', 'reverted');
CREATE INDEX IF NOT EXISTS idx_dedup_decisions_day ON dedup_decisions (day);
CREATE INDEX IF NOT EXISTS idx_dedup_decisions_winner ON dedup_decisions (winner_kind, winner_id);

-- Отпечаток набора записей по дате: пересчитываем только изменившиеся даты.
CREATE TABLE IF NOT EXISTS dedup_dates (
    day           DATE         PRIMARY KEY,
    fingerprint   TEXT         NOT NULL,
    processed_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    run_id        BIGINT       REFERENCES dedup_runs (id) ON DELETE SET NULL
);

-- Прогоны коллекторов: по ним дедупликатор понимает, что коллекторы закончили работу.
CREATE TABLE IF NOT EXISTS collector_runs (
    id            BIGSERIAL    PRIMARY KEY,
    service       TEXT         NOT NULL,
    started_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    heartbeat_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    status        TEXT         NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'error'))
);
CREATE INDEX IF NOT EXISTS idx_collector_runs_service_started ON collector_runs (service, started_at DESC);
