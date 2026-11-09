CREATE TABLE messages (
    surrogate_id        BIGSERIAL PRIMARY KEY,
    id                  BIGINT        NOT NULL,
    position            INT           NOT NULL DEFAULT 0,
    channel_id          BIGINT        NOT NULL,
    text                TEXT          NOT NULL,
    entities            JSONB,
    date                DATE,
    published_at        TIMESTAMPTZ   NOT NULL,
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    photo_file_id       VARCHAR,
    storage_message_id  BIGINT,
    hidden_by_admin     BOOLEAN       NOT NULL DEFAULT FALSE,
    duplicate_of        BIGINT        REFERENCES messages(surrogate_id),
    event_types         TEXT[]        NOT NULL DEFAULT '{}',

    CONSTRAINT uq_messages_channel_id_position UNIQUE (channel_id, id, position)
);

CREATE INDEX idx_messages_date_created    ON messages (date, created_at);
CREATE INDEX idx_messages_channel         ON messages (channel_id);
CREATE INDEX idx_messages_published_at    ON messages (published_at);
CREATE INDEX idx_messages_event_types     ON messages USING gin (event_types);

CREATE TABLE favorites (
    user_id     BIGINT        NOT NULL,
    msg_id      BIGINT        NOT NULL REFERENCES messages (surrogate_id) ON DELETE CASCADE,
    added_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT pk_favorites PRIMARY KEY (user_id, msg_id)
);

CREATE INDEX idx_favorites_msg_id ON favorites (msg_id);

CREATE TABLE hidden_events (
    user_id     BIGINT  NOT NULL,
    msg_id      BIGINT  NOT NULL REFERENCES messages (surrogate_id) ON DELETE CASCADE,

    CONSTRAINT pk_hidden_events PRIMARY KEY (user_id, msg_id)
);

CREATE INDEX idx_hidden_events_user ON hidden_events (user_id);

CREATE TABLE user_stats (
    user_id             BIGINT        PRIMARY KEY,
    first_seen          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    last_seen           TIMESTAMPTZ   NOT NULL DEFAULT now(),
    actions_count       INT           NOT NULL DEFAULT 0,
    menu_message_id     BIGINT        DEFAULT NULL,
    menu_message_type   VARCHAR(10)   DEFAULT 'text'
);

CREATE TABLE conversations (
    user_id             BIGINT        PRIMARY KEY,
    last_msg_id         BIGINT,
    unanswered_count    INT           NOT NULL DEFAULT 0,
    is_banned           BOOLEAN       NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE TABLE conversation_messages (
    id          BIGSERIAL     PRIMARY KEY,
    user_id     BIGINT        NOT NULL REFERENCES conversations (user_id) ON DELETE CASCADE,
    direction   VARCHAR(10)   NOT NULL CHECK (direction IN ('incoming', 'outgoing')),
    text        TEXT          NOT NULL,
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX idx_conv_messages_user ON conversation_messages (user_id);

CREATE INDEX IF NOT EXISTS idx_messages_date
    ON messages(date)
    WHERE hidden_by_admin = FALSE;

CREATE INDEX IF NOT EXISTS idx_messages_channel_date
    ON messages(channel_id, date)
    WHERE hidden_by_admin = FALSE;

CREATE INDEX IF NOT EXISTS idx_favorites_user
    ON favorites(user_id);

CREATE INDEX IF NOT EXISTS idx_hidden_user
    ON hidden_events(user_id);

CREATE TABLE scheduled_alerts (
    id               BIGSERIAL PRIMARY KEY,
    text             TEXT NOT NULL,
    scheduled_at     TIMESTAMPTZ NOT NULL,
    sent_at          TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    source           VARCHAR(10) NOT NULL,
    msg_surrogate_id BIGINT REFERENCES messages(surrogate_id)
);

CREATE TABLE IF NOT EXISTS cinema_events (
    id                        BIGSERIAL PRIMARY KEY,
    cinema_name               TEXT NOT NULL,
    cinema_maps_url           TEXT,
    cinema_url                TEXT,
    movie_id                  TEXT,
    movie_title               TEXT NOT NULL,
    movie_title_serbian       TEXT,
    movie_genre               TEXT,
    movie_description_short   TEXT,
    movie_description_full    TEXT,
    movie_title_normalized    TEXT,
    movie_poster_url          TEXT,
    movie_poster_file_id      TEXT,
    movie_poster_msg_id       BIGINT,
    showtime                  TIMESTAMPTZ NOT NULL,
    ticket_url                TEXT,
    source                    VARCHAR(20) NOT NULL,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cinema_events_showtime
    ON cinema_events(showtime);

CREATE INDEX IF NOT EXISTS idx_cinema_events_title_normalized_showtime
    ON cinema_events(movie_title_normalized, showtime);

CREATE INDEX IF NOT EXISTS idx_cinema_events_showtime_belgrade_date
    ON cinema_events (DATE(showtime AT TIME ZONE 'Europe/Belgrade'));

CREATE TABLE IF NOT EXISTS cinema_descriptions (
    cinema_name TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS public.events (
    id             bigserial     NOT NULL,
    "source"       varchar(30)   NOT NULL,
    source_url     text          NOT NULL,
    title          text          NOT NULL,
    description    text          NULL,
    starts_at      date          NOT NULL,
    venue          text          NULL,
    city           text          NULL,
    country        bpchar(2)     DEFAULT 'RS'::bpchar NULL,
    images         text          NULL,
    dedup_key      text          NULL,
    created_at     timestamptz   DEFAULT now() NOT NULL,
    updated_at     timestamptz   DEFAULT now() NOT NULL,
    event_types    text[]        NULL,
    image_file_id  text          NULL,
    description_ru text          NULL,
    hidden_by_admin boolean      NOT NULL DEFAULT FALSE,
    CONSTRAINT events_pkey PRIMARY KEY (id),
    CONSTRAINT uq_events_source_url UNIQUE (source_url)
);

CREATE INDEX IF NOT EXISTS idx_events_city_starts
    ON public.events USING btree (city, starts_at);
CREATE INDEX IF NOT EXISTS idx_events_starts_date
    ON public.events USING btree (starts_at);
CREATE INDEX IF NOT EXISTS idx_events_source
    ON public.events USING btree (source);
CREATE INDEX IF NOT EXISTS idx_events_dedup_key
    ON public.events USING btree (dedup_key);
CREATE INDEX IF NOT EXISTS idx_events_event_types
    ON public.events USING gin (event_types);
CREATE INDEX IF NOT EXISTS idx_events_image_file_id_null
    ON public.events USING btree (id) WHERE image_file_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_events_description_ru_null
    ON public.events USING btree (id) WHERE description_ru IS NULL;

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

-- Покрывающие индексы для единой ленты (см. migrations/006_feed_covering_indexes.sql)
CREATE INDEX IF NOT EXISTS idx_events_city_starts_cov
    ON events (city, starts_at) INCLUDE (id, created_at);

CREATE INDEX IF NOT EXISTS idx_messages_date_cov
    ON messages (date) INCLUDE (surrogate_id, created_at)
    WHERE hidden_by_admin = FALSE;

-- 008: AI-дедупликация

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

-- 009: «пост уже оценён AI-парсингом» (просроченные посты не попадают в messages)
CREATE TABLE IF NOT EXISTS ai_parse_seen (
    channel_id  BIGINT       NOT NULL,
    message_id  BIGINT       NOT NULL,
    position    INT          NOT NULL DEFAULT 0,
    text_hash   TEXT         NOT NULL,
    seen_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (channel_id, message_id, position)
);

CREATE INDEX IF NOT EXISTS idx_ai_parse_seen_seen_at ON ai_parse_seen (seen_at);

-- 010: видимые события для ленты и календаря
CREATE INDEX IF NOT EXISTS idx_events_city_starts_visible
    ON events (city, starts_at) INCLUDE (id, created_at)
    WHERE hidden_by_admin = FALSE;
