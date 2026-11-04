-- Единое название категории: «Семейные/детские мероприятия» -> «Семейные/детские».
-- Порядок категорий сохраняется (первая — главная), повторы схлопываются.
-- Безопасно выполнять повторно: после первого раза строк с прежним названием нет.

UPDATE messages m
SET event_types = (
    SELECT COALESCE(array_agg(x ORDER BY first_pos), '{}')
    FROM (
        SELECT x, MIN(pos) AS first_pos
        FROM (
            SELECT CASE WHEN t = 'Семейные/детские мероприятия' THEN 'Семейные/детские' ELSE t END AS x, pos
            FROM unnest(m.event_types) WITH ORDINALITY AS u(t, pos)
        ) s
        GROUP BY x
    ) d
)
WHERE 'Семейные/детские мероприятия' = ANY (m.event_types);

UPDATE events e
SET event_types = (
    SELECT COALESCE(array_agg(x ORDER BY first_pos), '{}')
    FROM (
        SELECT x, MIN(pos) AS first_pos
        FROM (
            SELECT CASE WHEN t = 'Семейные/детские мероприятия' THEN 'Семейные/детские' ELSE t END AS x, pos
            FROM unnest(e.event_types) WITH ORDINALITY AS u(t, pos)
        ) s
        GROUP BY x
    ) d
)
WHERE 'Семейные/детские мероприятия' = ANY (e.event_types);
