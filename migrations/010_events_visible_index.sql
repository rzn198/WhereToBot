-- 010: видимые события для ленты и календаря.
-- Лента, счётчики категорий и календарь фильтруют events.hidden_by_admin = FALSE (дубли, скрытые AI);
-- частичный покрывающий индекс, как у messages (006). Безопасно выполнять повторно.
CREATE INDEX IF NOT EXISTS idx_events_city_starts_visible
    ON events (city, starts_at) INCLUDE (id, created_at)
    WHERE hidden_by_admin = FALSE;
