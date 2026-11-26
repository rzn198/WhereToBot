-- 023: счётчики неудач AI по фильму киноафиши: отдельно для названий и для перевода описания.
-- Фильм, у которого этап трижды подряд не прошёл проверку, на этом этапе пропускается (не тратим токены на каждый прогон);
-- сбрасывается скриптом scripts/cinema_ai_shadow.py --reset-failed.
-- Безопасно выполнять повторно.

ALTER TABLE cinema_movies
    ADD COLUMN IF NOT EXISTS ai_titles_fails INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS ai_desc_fails   INTEGER NOT NULL DEFAULT 0;
