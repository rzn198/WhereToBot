-- 021: разовое исправление категорий 21 события (ноябрь 2026), найденных AI-сверкой категорий.
-- Источник: ticketsrs (20 записей) и polako (1). category_manual = TRUE — скрейпер и AI их больше не перезапишут.
-- Строка применяется только если совпали и id, и источник, и название: на другой базе (локальной) ничего не заденет.
-- Скрытые админом записи пропускаются. Безопасно выполнять повторно.

UPDATE events e
SET event_types = v.cats, category_manual = TRUE
FROM (VALUES
    (535,    'ticketsrs', 'Notre-Dame de Paris',                          ARRAY['Кино/Театр']),
    (536,    'ticketsrs', 'Notre-Dame de Paris',                          ARRAY['Кино/Театр']),
    (537,    'ticketsrs', 'Notre-Dame de Paris',                          ARRAY['Кино/Театр']),
    (538,    'ticketsrs', 'Notre-Dame de Paris',                          ARRAY['Кино/Театр']),
    (93561,  'ticketsrs', 'KABARE',                                       ARRAY['Кино/Театр']),
    (37990,  'ticketsrs', 'KABARE KAPOR',                                 ARRAY['Кино/Театр']),
    (1080,   'ticketsrs', 'Na kafi kod kneginje Ljubice',                 ARRAY['Кино/Театр']),
    (106137, 'ticketsrs', 'Na kafi kod kneginje Ljubice',                 ARRAY['Кино/Театр']),
    (106138, 'ticketsrs', 'Na kafi kod kneginje Ljubice',                 ARRAY['Кино/Театр']),
    (106139, 'ticketsrs', 'Na kafi kod kneginje Ljubice',                 ARRAY['Кино/Театр']),
    (38126,  'ticketsrs', 'MOJ OTAC VUK KARADŽIĆ',                        ARRAY['Кино/Театр']),
    (38127,  'ticketsrs', 'DOSIJE DOSITEJ',                               ARRAY['Кино/Театр']),
    (533,    'ticketsrs', 'Super ENDURO 2027 - TRENING I KVALIFIKACIJE',  ARRAY['Спорт/активный отдых']),
    (534,    'ticketsrs', 'Super ENDURO 2027',                            ARRAY['Спорт/активный отдых']),
    (93606,  'ticketsrs', 'Iluzije u Roditeljstvu - Dr Roberto Grujičić', ARRAY['Мастер-классы/Нетворкинг']),
    (480,    'ticketsrs', 'DNEVNIK ANE FRANK',                            ARRAY['Кино/Театр', 'Семейные/детские']),
    (483,    'ticketsrs', 'ASKA I VUK',                                   ARRAY['Кино/Театр', 'Семейные/детские']),
    (486,    'ticketsrs', 'MAČKA U ČIZMAMA',                              ARRAY['Кино/Театр', 'Семейные/детские']),
    (491,    'ticketsrs', 'MAČKA U ČIZMAMA',                              ARRAY['Кино/Театр', 'Семейные/детские']),
    (102601, 'ticketsrs', 'LJUBAV KAO MAGIJA',                            ARRAY['Кино/Театр', 'Семейные/детские']),
    (100913, 'polako',    'Nothing Sacred, Kontrapunkt',                  ARRAY['Концерты'])
) AS v(id, source, title, cats)
WHERE e.id = v.id
  AND lower(e.source) = v.source
  AND e.title = v.title
  AND NOT e.hidden_by_admin;
