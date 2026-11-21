-- 020: ИИ-хуки строк списка и кэш обогащения (Wikipedia / MusicBrainz).
-- hook_status: ok (хук есть) | none (данных для хука нет) | invalid (ответ не прошёл проверку) |
--              not_found (обогащение ничего не нашло: повтор через ttl_missing_days) | retry (сбой сети: повтор через несколько часов).
-- hook_source: text (основание в описании записи) | web (основание в найденных фактах); hook_evidence — цитата-основание.
-- text_time — время старта, явно указанное в тексте записи (запасное, если в БД starts_time пусто).
-- card_enrichment — кэш найденных фактов по ключу «источник:имя»; status found | not_found | ambiguous.
-- Безопасно выполнять повторно.

ALTER TABLE card_lines
    ADD COLUMN IF NOT EXISTS hook          TEXT,
    ADD COLUMN IF NOT EXISTS hook_formula  SMALLINT,
    ADD COLUMN IF NOT EXISTS hook_source   TEXT,
    ADD COLUMN IF NOT EXISTS hook_evidence TEXT,
    ADD COLUMN IF NOT EXISTS hook_url      TEXT,
    ADD COLUMN IF NOT EXISTS hook_status   TEXT,
    ADD COLUMN IF NOT EXISTS text_time     TIME;

CREATE INDEX IF NOT EXISTS card_lines_src_hash_idx ON card_lines (src_hash);

CREATE TABLE IF NOT EXISTS card_enrichment (
    key         TEXT         PRIMARY KEY,
    source      TEXT         NOT NULL,
    query       TEXT         NOT NULL,
    status      TEXT         NOT NULL CHECK (status IN ('found', 'not_found', 'ambiguous')),
    facts       TEXT,
    url         TEXT,
    fetched_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ  NOT NULL
);
