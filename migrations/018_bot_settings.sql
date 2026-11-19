-- 018: настройки бота, меняемые из админки без деплоя (ключ → значение).
-- Первая настройка: ui.category_mode (cards | window_admin | window). Значение из БД главнее config.yaml.
-- Безопасно выполнять повторно.

CREATE TABLE IF NOT EXISTS bot_settings (
    key         TEXT         PRIMARY KEY,
    value       TEXT         NOT NULL,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_by  BIGINT
);
