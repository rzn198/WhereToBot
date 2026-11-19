-- 019: ИИ-строки списка категорий (окно): короткое название и атрибут (жанр, тип, формат) записи.
-- kind: 'm' пост (messages.surrogate_id) | 'e' событие сайта (events.id); без FK: сирот удаляет ночная очистка.
-- Хранится только результат ИИ; режим «code» (строка из данных БД) считается на лету и остаётся запасным вариантом.
-- src_hash — хэш входных данных и версий промптов: при его смене строка пересчитывается.
-- Безопасно выполнять повторно.

CREATE TABLE IF NOT EXISTS card_lines (
    kind            CHAR(1)      NOT NULL CHECK (kind IN ('m', 'e')),
    ref_id          BIGINT       NOT NULL,
    short_title     TEXT         NOT NULL,
    attr            TEXT,                         -- значение атрибута профиля (жанр и т.п.); NULL = модель не уверена
    profile         TEXT         NOT NULL,        -- профиль (основная категория записи), по которому строка сгенерирована
    src_hash        TEXT         NOT NULL,
    model           TEXT,
    prompt_version  TEXT         NOT NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, ref_id)
);
