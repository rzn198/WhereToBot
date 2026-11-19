"""Профили категорий для ИИ-строк списка: какой атрибут извлекаем, допустимые значения и эмодзи.

Профиль выбирается по ОСНОВНОЙ (первой) категории записи. Профиль задаёт:
  attr_name — как называется атрибут для модели («жанр», «формат»); пусто — атрибута нет, ИИ только сокращает название;
  values    — закрытый список {значение: эмодзи}; модель обязана выбрать из него или вернуть null;
  pattern   — вместо списка: регулярное выражение значения (возраст «6+», «3–7»); эмодзи тогда одно — emoji;
  rules     — правила для модели именно для этой категории (добавляются в промпт);
  version   — версия правил профиля; её смена пересчитывает строки только этой категории;
  ai        — false: профиль без ИИ (строка из данных БД, например музеи).
Встроенные профили можно менять и дополнять в config.yaml → card_lines.profiles.<категория>; задавать нужно только то,
что меняется (значения values заменяются целиком).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from shared.categories import normalize_category

MODES = ('off', 'shadow', 'on')


@dataclass(frozen=True)
class Profile:
    name: str                                   # название категории (ключ профиля)
    ai: bool = True
    attr_name: str = ''
    values: dict[str, str] = field(default_factory=dict)       # значение -> эмодзи
    pattern: str = ''
    emoji: str = ''                             # эмодзи для значений по pattern
    rules: str = ''
    version: str = 'v1'

    @property
    def has_attr(self) -> bool:
        return bool(self.attr_name and (self.values or self.pattern))

    def canonical_value(self, raw) -> str | None:
        """Значение атрибута из ответа модели; None, если не из списка / не по шаблону / пусто."""
        if not self.has_attr or raw is None or isinstance(raw, (dict, list, bool)):
            return None
        s = re.sub(r'\s+', ' ', str(raw)).strip().strip('.«»"\'')
        if not s or s.lower() in ('null', 'none', 'нет', '-', '—', 'неизвестно', 'не указано'):
            return None
        if self.values:
            for v in self.values:
                if v.casefold() == s.casefold():
                    return v
            return None
        s = re.sub(r'\s*[-—–]\s*', '–', s)
        return s if re.fullmatch(self.pattern, s) else None

    def emoji_for(self, value: str | None) -> str:
        if not value:
            return ''
        return self.values.get(value, '') if self.values else self.emoji


DEFAULT_PROFILES: dict[str, dict] = {
    'Концерты': {
        'attr_name': 'жанр',
        'values': {'рок': '🎸', 'метал': '🤘', 'поп': '🎤', 'хип-хоп': '🎙', 'электроника': '🎧', 'джаз/блюз': '🎷',
                   'классика': '🎻', 'фолк': '🪕', 'народная': '🪗'},
        'rules': ('Название — исполнитель или группа на первом месте (как в источнике); если выступают несколько, '
                  'первые двое-трое через запятую. Названия туров, «концерт», «live», «Beograd» не нужны. '
                  'Жанр определяй по описанию и по тому, что знаешь об исполнителе; «народная» — сербская и балканская '
                  'эстрада и фолк-поп (севдалинка, турбо-фолк); если жанр неясен или это вечеринка/диджей-сет без '
                  'явного жанра — null.'),
    },
    'Кино/Театр': {
        'attr_name': 'формат',
        'values': {'фильм': '🎬', 'документальный фильм': '📽', 'спектакль': '🎭', 'мюзикл': '🎶', 'опера': '🎼',
                   'балет': '🩰'},
        'rules': ('Название — название спектакля или фильма (как в источнике), без названия театра, без слов «спектакль», '
                  '«представление», «Pozorišna predstava». Для поста со списком показов возьми главный или первый. '
                  'Формат: «спектакль» для драмы, комедии, моноспектакля, детского спектакля; если формат неясен — null.'),
    },
    'Спорт/активный отдых': {
        'attr_name': 'вид спорта',
        'values': {'бег': '🏃', 'футбол': '⚽', 'баскетбол': '🏀', 'волейбол': '🏐', 'теннис': '🎾', 'плавание': '🏊',
                   'поход': '🥾', 'велоспорт': '🚴', 'единоборства': '🥋', 'йога': '🧘', 'настолки': '🎲'},
        'rules': ('Название — суть события: матч «Сербия — Италия», забег, поход (например «Три вершины Девицы»), турнир. '
                  'Без организатора и подробностей маршрута. «Поход» — пеший поход и псейкинг в горы. '
                  'Если вид спорта неясен — null.'),
    },
    'Игры и развлечения': {
        'attr_name': 'тип',
        'values': {'стендап': '🎤', 'квиз': '🧠', 'настолки': '🎲', 'караоке': '🎤', 'вечеринка': '🪩',
                   'импровизация': '🎭', 'лекция': '🎓'},
        'rules': ('Название — имя комика, название шоу или квиза (как в источнике), без слов «вечер», «шоу в Белграде». '
                  'Тип: «стендап» — выступления комиков и открытый микрофон; «импровизация» — импров-шоу; '
                  'если тип неясен — null.'),
    },
    'Мастер-классы/Нетворкинг': {
        'attr_name': 'формат',
        'values': {'лекция': '🎓', 'мастер-класс': '🛠', 'нетворкинг': '🤝', 'разговорный клуб': '🗣', 'курс': '📚',
                   'квиз': '🧠'},
        'rules': ('Название — тема или название встречи (как в источнике), без имени организатора и слов «приглашаем», '
                  '«открытая лекция». «Курс» — несколько занятий подряд, «мастер-класс» и воркшоп — одно практическое занятие. '
                  'Если формат неясен — null.'),
    },
    'Выставки/Ярмарки': {
        'attr_name': 'тип',
        'values': {'выставка': '🖼', 'фотовыставка': '📷', 'ярмарка': '🛍'},
        'rules': ('Название — название выставки или ярмарки (как в источнике), без слов «выставка» и «открытие», '
                  'без названия галереи. «Ярмарка» — ярмарки, маркеты, базары. Если тип неясен — null.'),
    },
    'Фестивали': {
        'attr_name': 'вид',
        'values': {'музыка': '🎶', 'кино': '🎬', 'еда': '🍽', 'искусство': '🎨', 'литература': '📚'},
        'rules': ('Название — название фестиваля (как в источнике) или, для отдельного выступления внутри фестиваля, '
                  'имя исполнителя. Слово «фестиваль» в названии оставляй, если оно часть имени. '
                  'Вид — по тематике фестиваля; если неясен или смешанный — null.'),
    },
    'Семейные/детские': {
        'attr_name': 'возраст',
        'pattern': r'\d{1,2}\+|\d{1,2}–\d{1,2}',
        'emoji': '🧸',
        'rules': ('Название — суть события для детей (спектакль, лагерь, мастерская), без слов «для детей» в начале. '
                  'Возраст — только если указан явно в данных: в виде «6+» или «3–7»; иначе null.'),
    },
    'Поездки и экскурсии': {
        'attr_name': 'тип',
        'values': {'экскурсия': '🚶', 'поездка': '🚌', 'поход': '🥾'},
        'rules': ('Название — маршрут или тема («Скрытый Белград», «Ниш и термальные бани»), без слов «экскурсия», '
                  '«приглашаем». «Экскурсия» — по городу или объекту, «поездка» — выезд в другой город, '
                  '«поход» — пеший. Если тип неясен — null.'),
    },
    'Музеи': {'ai': False},
}


def _version(value, default: str) -> str:
    s = str(value).strip() if value not in (None, '') else ''
    return s or default


def _profile(name: str, base: dict, over: dict | None) -> Profile:
    d = dict(base)
    for k, v in (over or {}).items():
        d[k] = v
    values = d.get('values') or {}
    values = {str(k).strip(): str(v or '').strip() for k, v in values.items() if str(k).strip()} if isinstance(values, dict) else {}
    return Profile(
        name=name,
        ai=bool(d.get('ai', True)),
        attr_name=str(d.get('attr_name') or '').strip(),
        values=values,
        pattern=str(d.get('pattern') or ''),
        emoji=str(d.get('emoji') or '').strip(),
        rules=str(d.get('rules') or '').strip(),
        version=_version(d.get('version'), 'v1'),
    )


def build_profiles(cfg_profiles: dict | None) -> dict[str, Profile]:
    """Встроенные профили + правки из config.yaml (card_lines.profiles). Неизвестные категории из конфига тоже принимаются."""
    cfg_profiles = cfg_profiles if isinstance(cfg_profiles, dict) else {}
    out: dict[str, Profile] = {}
    for name, base in DEFAULT_PROFILES.items():
        out[name] = _profile(name, base, cfg_profiles.get(name) if isinstance(cfg_profiles.get(name), dict) else None)
    for name, over in cfg_profiles.items():
        name = normalize_category(str(name))
        if name not in out and isinstance(over, dict):
            out[name] = _profile(name, {}, over)
    return out


def profile_for(event_types, profiles: dict[str, Profile]) -> Profile | None:
    """Профиль основной (первой) категории записи; None — у категории нет профиля или он без ИИ."""
    for cat in event_types or []:
        p = profiles.get(normalize_category(str(cat)))
        return p if p is not None and p.ai else None
    return None
