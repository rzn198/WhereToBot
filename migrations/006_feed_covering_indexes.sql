-- Покрывающие индексы для единой ленты (посты + события).
-- Запрос ленты читает только дату, id и created_at. Если эти колонки лежат прямо в индексе,
-- Postgres не ходит в таблицу за каждой строкой (Index Only Scan, Heap Fetches: 0).
-- Замер до/после на одной базе, 2 прогона. На данных как на проде время построения ленты
-- не меняется (в пределах шума, ~1,3 мс), пропускная способность при 100 одновременных
-- пользователях +17%. На данных в 10 раз больше: лента «Неделя» -33% (9,3-11 -> 6,3-7,2 мс),
-- пропускная способность +4...+17%. Миграция нужна как запас на рост, сегодня выигрыш небольшой.
--
-- Безопасно: только добавляет индексы, данные и код не меняются; можно запускать повторно.
-- Существующие индексы (idx_events_city_starts, idx_messages_date) не удаляются.
-- Запускать БЕЗ обёртки BEGIN/COMMIT: VACUUM не работает внутри транзакции.

CREATE INDEX IF NOT EXISTS idx_events_city_starts_cov
    ON events (city, starts_at) INCLUDE (id, created_at);

CREATE INDEX IF NOT EXISTS idx_messages_date_cov
    ON messages (date) INCLUDE (surrogate_id, created_at)
    WHERE hidden_by_admin = FALSE;

-- карта видимости и статистика, чтобы планировщик сразу выбирал Index Only Scan
VACUUM (ANALYZE) events;
VACUUM (ANALYZE) messages;
