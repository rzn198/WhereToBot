-- 013: очереди «Неразобранные» (без даты / без категории), ручная категория события, статус «пересмотрено».
-- * events.category_manual — категорию выбрал админ кнопкой; скрейперы, AI и скрипты её не перезаписывают.
-- * event_reviews.flow — откуда запись: 'review' (кнопка «Пересмотр»), 'no_date' / 'no_category' (очереди «Неразобранные»).
--   Запись очереди = принятое решение по элементу: пока она есть, элемент в этой очереди больше не показывается.
-- * event_reviews.status — добавлен 'applied' («пересмотрено»: категория записана в events/messages), кнопка остаётся неактивной.
-- * reason_code — добавлены set_date, rejected, set_category, skipped, hidden.
-- Безопасно выполнять повторно.

ALTER TABLE events ADD COLUMN IF NOT EXISTS category_manual BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE event_reviews ADD COLUMN IF NOT EXISTS flow TEXT NOT NULL DEFAULT 'review';

ALTER TABLE event_reviews DROP CONSTRAINT IF EXISTS event_reviews_flow_check;
ALTER TABLE event_reviews ADD CONSTRAINT event_reviews_flow_check
    CHECK (flow IN ('review', 'no_date', 'no_category'));

ALTER TABLE event_reviews DROP CONSTRAINT IF EXISTS event_reviews_reason_code_check;
ALTER TABLE event_reviews ADD CONSTRAINT event_reviews_reason_code_check
    CHECK (reason_code IN ('wrong_date', 'wrong_category', 'manual',
                           'set_date', 'rejected', 'set_category', 'skipped', 'hidden'));

ALTER TABLE event_reviews DROP CONSTRAINT IF EXISTS event_reviews_status_check;
ALTER TABLE event_reviews ADD CONSTRAINT event_reviews_status_check
    CHECK (status IN ('open', 'resolved', 'applied'));

-- Одно решение очереди на элемент: повторное нажатие / гонка ничего не дублирует.
CREATE UNIQUE INDEX IF NOT EXISTS uq_event_reviews_decision_event
    ON event_reviews (flow, event_id) WHERE flow <> 'review' AND event_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_event_reviews_decision_message
    ON event_reviews (flow, message_id) WHERE flow <> 'review' AND message_id IS NOT NULL;
