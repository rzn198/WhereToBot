-- 012: пересмотры событий и постов, отмеченные админом в боте (кнопка «Пересмотр» на карточке).
-- Запись хранит снимок события на момент пересмотра (snapshot + основные поля), поэтому остаётся читаемой,
-- даже если событие позже изменится или будет удалено чисткой. Исправленные категория/дата живут ТОЛЬКО здесь,
-- в events/messages ничего не меняется. Закрывается вручную в БД (status = 'resolved').
-- Безопасно выполнять повторно (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS event_reviews (
    id                BIGSERIAL    PRIMARY KEY,
    kind              TEXT         NOT NULL CHECK (kind IN ('event', 'post')),
    event_id          BIGINT,                      -- events.id (kind = 'event'), без FK: запись переживает чистку
    message_id        BIGINT,                      -- messages.surrogate_id (kind = 'post')
    reason_code       TEXT         NOT NULL CHECK (reason_code IN ('wrong_date', 'wrong_category', 'manual')),
    reason_text       TEXT,                        -- ручной ввод админа
    correct_category  TEXT,                        -- выбранная админом категория ('Другое' — только здесь)
    correct_date      DATE,                        -- введённая админом дата
    title             TEXT,
    description       TEXT,                        -- полный текст события / поста
    starts_at         DATE,
    venue             TEXT,
    source            TEXT,                        -- событие: events.source; пост: канал
    source_url        TEXT,
    event_types       TEXT[],                      -- категории в БД на момент пересмотра
    snapshot          JSONB        NOT NULL,       -- все поля записи как были
    status            TEXT         NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    resolved_at       TIMESTAMPTZ,
    resolution_note   TEXT
);

-- Один открытый пересмотр на событие / пост: повторное нажатие ничего не дублирует.
CREATE UNIQUE INDEX IF NOT EXISTS uq_event_reviews_open_event
    ON event_reviews (event_id) WHERE status = 'open' AND event_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_event_reviews_open_message
    ON event_reviews (message_id) WHERE status = 'open' AND message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_event_reviews_status_created
    ON event_reviews (status, created_at);
