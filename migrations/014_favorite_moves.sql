-- 014: перенос избранного и скрытий «🙈» с проигравшего дубля на победителя (дедупликатор, method: ai).
-- Одна строка = один пользователь по одному решению dedup_decisions. По ней перенос можно откатить
-- (проигравший воскрес) и выгрузить отчёт. Хранится до даты события + 1 месяц (чистит дедупликатор).
-- Безопасно выполнять повторно.

CREATE TABLE IF NOT EXISTS favorite_moves (
    id                   BIGSERIAL    PRIMARY KEY,
    decision_id          BIGINT       REFERENCES dedup_decisions (id) ON DELETE SET NULL,
    user_id              BIGINT       NOT NULL,
    event_date           DATE         NOT NULL,
    loser_kind           CHAR(1)      NOT NULL CHECK (loser_kind IN ('m', 'e')),
    loser_id             BIGINT       NOT NULL,
    winner_kind          CHAR(1)      NOT NULL CHECK (winner_kind IN ('m', 'e')),
    winner_id            BIGINT       NOT NULL,
    result               TEXT         NOT NULL CHECK (result IN ('fav', 'hidden')),   -- что осталось на победителе: ⭐ или 🙈
    had_fav_loser        BOOLEAN      NOT NULL DEFAULT FALSE,
    loser_fav_added_at   TIMESTAMPTZ,
    had_fav_winner       BOOLEAN      NOT NULL DEFAULT FALSE,
    winner_fav_added_at  TIMESTAMPTZ,
    had_hid_loser        BOOLEAN      NOT NULL DEFAULT FALSE,
    had_hid_winner       BOOLEAN      NOT NULL DEFAULT FALSE,
    moved_at             TIMESTAMPTZ  NOT NULL DEFAULT now(),
    reverted             BOOLEAN      NOT NULL DEFAULT FALSE,
    reverted_at          TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_favorite_moves_decision_user ON favorite_moves (decision_id, user_id);
CREATE INDEX IF NOT EXISTS idx_favorite_moves_date ON favorite_moves (event_date);
CREATE INDEX IF NOT EXISTS idx_favorite_moves_winner ON favorite_moves (winner_kind, winner_id);
