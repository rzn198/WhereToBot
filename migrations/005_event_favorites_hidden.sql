-- Избранное и скрытие для событий из таблицы events (источники-сайты).
-- Отдельные таблицы: favorites/hidden_events привязаны внешним ключом к messages.
-- ON DELETE CASCADE: когда очистка удаляет прошедшее событие, его избранное/скрытие уходит вместе с ним.
-- ВАЖНО: применить ДО выкладки кода бота. Все сервисы при старте проверяют, что таблицы из
-- db/tables.py существуют, иначе не запустятся.
-- Безопасно запускать повторно (IF NOT EXISTS).
BEGIN;

CREATE TABLE IF NOT EXISTS event_favorites (
    user_id   BIGINT       NOT NULL,
    event_id  BIGINT       NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    added_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT pk_event_favorites PRIMARY KEY (user_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_event_favorites_event ON event_favorites (event_id);

CREATE TABLE IF NOT EXISTS event_hidden (
    user_id   BIGINT  NOT NULL,
    event_id  BIGINT  NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    CONSTRAINT pk_event_hidden PRIMARY KEY (user_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_event_hidden_event ON event_hidden (event_id);

COMMIT;
