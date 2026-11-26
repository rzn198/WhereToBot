-- 024: избранное киноафиши — конкретный сеанс (фильм + кинотеатр + время).
-- Привязка к cinema_movies, а не к cinema_events: сеансы пересобираются каждые 8 часов, а фильм остаётся.
-- Сеанс, которого больше нет в афише, остаётся в таблице и показывается как «снят» (пуш о нём не шлём).
-- Безопасно выполнять повторно.

CREATE TABLE IF NOT EXISTS cinema_favorites (
    id          BIGSERIAL    PRIMARY KEY,                       -- короткий идентификатор для callback_data
    user_id     BIGINT       NOT NULL,
    movie_id    BIGINT       NOT NULL REFERENCES cinema_movies (id) ON DELETE CASCADE,
    cinema_name TEXT         NOT NULL,
    showtime    TIMESTAMPTZ  NOT NULL,
    added_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_cinema_favorites UNIQUE (user_id, movie_id, cinema_name, showtime)
);

CREATE INDEX IF NOT EXISTS idx_cinema_favorites_user_showtime ON cinema_favorites (user_id, showtime);
CREATE INDEX IF NOT EXISTS idx_cinema_favorites_showtime ON cinema_favorites (showtime);
