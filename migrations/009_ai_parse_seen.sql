-- 009: «пост уже оценён AI-парсингом».
-- Просроченные посты не сохраняются в messages, поэтому проверка «уже сохранён» их не видит
-- и каждый прогон коллектора отправлял их в AI заново. Здесь запоминаем оценённые посты.
-- Безопасно выполнять повторно (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS ai_parse_seen (
    channel_id  BIGINT       NOT NULL,
    message_id  BIGINT       NOT NULL,
    position    INT          NOT NULL DEFAULT 0,
    text_hash   TEXT         NOT NULL,       -- md5 текста: пост отредактировали → оцениваем заново
    seen_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (channel_id, message_id, position)
);

CREATE INDEX IF NOT EXISTS idx_ai_parse_seen_seen_at ON ai_parse_seen (seen_at);
