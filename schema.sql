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

    CONSTRAINT uq_messages_id_position UNIQUE (id, position)
);

CREATE INDEX idx_messages_date_created    ON messages (date, created_at);
CREATE INDEX idx_messages_channel         ON messages (channel_id);
CREATE INDEX idx_messages_published_at    ON messages (published_at);

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