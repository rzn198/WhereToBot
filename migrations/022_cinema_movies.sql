-- 022: фильмы киноафиши — один фильм = одна запись, независимо от того, как его называют кинотеатры.
-- cinema_events по-прежнему плоская таблица сеансов (saver каждый прогон удаляет и вставляет сеансы источника),
-- поэтому всё, что считает AI (названия, перевод описания, жанры), живёт здесь и не пропадает при пересборке сеансов.
-- cinema_movie_aliases: канонический ключ названия -> фильм. Склейка AI = перенос алиаса на другой фильм.
-- cinema_genre_map: сербский термин жанра -> русский жанр; genre_ru IS NULL = «это не жанр» (отбрасываем).
-- Безопасно выполнять повторно.

CREATE TABLE IF NOT EXISTS cinema_movies (
    id                   BIGSERIAL    PRIMARY KEY,
    title_key            TEXT         NOT NULL UNIQUE,   -- канонический ключ первого названия (дальше ищем по алиасам)
    title_serbian        TEXT,
    title_original       TEXT,                           -- как отдал первый источник (бывает мусор: «RBO 2026 2027»)
    title_en             TEXT,                           -- AI: международное английское название (или оригинал латиницей)
    title_ru             TEXT,                           -- AI: русское название, только если устоявшееся
    genres_ru            TEXT[],                         -- AI: жанры по-русски, первый — главный
    description_src      TEXT,                           -- самое длинное описание из всех кинотеатров (только растёт)
    description_src_len  INTEGER      NOT NULL DEFAULT 0,
    description_ru       TEXT,                           -- AI: перевод description_src
    translated_src_len   INTEGER,                        -- длина исходника, который переведён (порог повторного перевода)
    status               TEXT         NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'done', 'failed')),
    ai_version           TEXT,
    ai_model             TEXT,
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_seen_at         TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cinema_movie_aliases (
    alias_key  TEXT    PRIMARY KEY,
    movie_id   BIGINT  NOT NULL REFERENCES cinema_movies(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cinema_movie_aliases_movie
    ON cinema_movie_aliases(movie_id);

CREATE TABLE IF NOT EXISTS cinema_genre_map (
    term        TEXT         PRIMARY KEY,
    genre_ru    TEXT,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

ALTER TABLE cinema_events
    ADD COLUMN IF NOT EXISTS movie_key BIGINT REFERENCES cinema_movies(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_cinema_events_movie_key_showtime
    ON cinema_events(movie_key, showtime);
