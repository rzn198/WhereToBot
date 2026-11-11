-- 011: кэш AI-категорий событий сайтов.
-- Один раз спрашиваем AI про событие и запоминаем ответ: пока название, описание, площадка и подсказка
-- категории не изменились (text_hash и версия промпта те же), повторных запросов нет.
-- Безопасно выполнять повторно (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS event_ai_categories (
    source       TEXT         NOT NULL,
    source_url   TEXT         NOT NULL,
    text_hash    TEXT         NOT NULL,       -- md5(версия промпта + входные поля)
    categories   TEXT[]       NOT NULL DEFAULT '{}',
    model        TEXT,
    decided_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (source, source_url)
);
