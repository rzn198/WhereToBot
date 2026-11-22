"""Профили категорий для ИИ-строк списка: какой атрибут извлекаем, допустимые значения и эмодзи.

Профиль выбирается по ОСНОВНОЙ (первой) категории записи. Профиль задаёт:
  attr_name — как называется атрибут для модели («жанр», «формат»); пусто — атрибута нет, ИИ только сокращает название;
  values    — закрытый список {значение: эмодзи}; модель обязана выбрать из него или вернуть null;
  pattern   — вместо списка: регулярное выражение значения (возраст «6+», «3–7»); эмодзи тогда одно — emoji;
  rules     — правила для модели именно для этой категории (добавляются в промпт);
  version   — версия правил профиля; её смена пересчитывает строки только этой категории;
  max_values — сколько значений атрибута можно дать одной записи (по умолчанию 1; у «Концертов» 2: основной жанр первым);
  formulas  — формулы хука по приоритету (name, need, form, web, web_only); пусто — хука у категории нет; hook_rules — доп. правила хука;
  enrich    — вид обогащения фактами из интернета ('artist'); пусто — без обогащения;
  fallback_emoji — эмодзи записи, когда значение не определено (у «Концертов» 🎵; пусто — без эмодзи);
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
class Formula:
    """Формула хука. Порядок в профиле = строгий приоритет: модель берёт первую, под которую в данных есть факты."""
    name: str                       # «повод + программа»
    need: str                       # какие факты должны быть в данных
    form: str                       # форма фразы
    web: bool = False               # можно ли опираться на факты из интернета (устойчивые: кто это, страна, жанр, годы)
    web_only: bool = False          # формула строится ТОЛЬКО по фактам из интернета (цитата обязана быть в них, не в описании)


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
    max_values: int = 1
    fallback_emoji: str = ''
    formulas: tuple[Formula, ...] = ()          # хук: формулы по приоритету; пусто — хука у категории нет
    hook_rules: str = ''                        # дополнительные правила хука этой категории
    enrich: str = ''                            # вид обогащения: 'artist' (MusicBrainz + Wikipedia) или пусто

    @property
    def has_attr(self) -> bool:
        return bool(self.attr_name and (self.values or self.pattern))

    @property
    def has_hook(self) -> bool:
        return bool(self.formulas)

    @property
    def hook_fingerprint(self) -> str:
        """Меняется при правке формул/правил хука/вида обогащения: записи пересчитываются."""
        import hashlib
        import json
        raw = json.dumps([[f.name, f.need, f.form, f.web, f.web_only] for f in self.formulas] + [self.hook_rules, self.enrich],
                         ensure_ascii=False)
        return hashlib.md5(raw.encode('utf-8')).hexdigest()[:10]

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

    def canonical_values(self, raw) -> list[str]:
        """Значения атрибута из ответа модели: строка («рок, вечеринка»), список или одно значение. Только допустимые,
        без повторов, не больше max_values (основной первым). Пусто — модель не уверена."""
        if raw is None or isinstance(raw, (dict, bool)):
            return []
        items = raw if isinstance(raw, list) else re.split(r'[,;]', str(raw)) if self.max_values > 1 else [raw]
        out: list[str] = []
        for it in items:
            v = self.canonical_value(it)
            if v and v not in out:
                out.append(v)
        return out[:self.max_values]

    def emoji_for(self, value: str | None) -> str:
        """Эмодзи значения; у нескольких значений через запятую (`рок,вечеринка`) — эмодзи подряд.
        Нет значения — fallback_emoji профиля."""
        parts = [v.strip() for v in str(value).split(',') if v.strip()] if value else []
        if not parts:
            return self.fallback_emoji
        got = [self.values.get(v, '') if self.values else self.emoji for v in parts]
        return ''.join(e for e in got if e) or self.fallback_emoji      # значение убрали из списка (до пересчёта): общий эмодзи


DEFAULT_PROFILES: dict[str, dict] = {
    'Концерты': {
        'attr_name': 'жанр',
        'max_values': 2,
        'fallback_emoji': '🎵',
        'values': {'рок': '🎸', 'метал': '🤘', 'панк': '⚡', 'инди': '📼', 'поп': '⭐', 'хип-хоп': '🎤',
                   'электроника': '🎛', 'вечеринка': '🪩', 'джаз': '🎷', 'блюз': '🎺', 'классика': '🎻',
                   'фолк': '🪕', 'народная': '🪗', 'этно': '🌍'},
        'version': 'v2',
        'enrich': 'artist',
        'formulas': [
            {'name': 'повод + программа',
             'need': 'в данных явно указан повод (юбилей, возвращение, презентация альбома, тур, «впервые» и т. п.) И то, '
                     'что будет исполнено или показано (программа, альбом, песни)',
             'form': '[Артист] — [повод]: [программа]'},
            {'name': 'кто это + повод',
             'need': 'в данных есть краткая характеристика исполнителя И явный повод',
             'form': '[Артист] — [кто это], [повод]'},
            {'name': 'формат + состав',
             'need': 'указаны формат выступления (акустика, оркестр, диджей-сет, трибьют, камерный вечер) и состав или участники',
             'form': '[Артист] — [формат]: [состав]'},
            {'name': 'что будет в зале',
             'need': 'описаны атмосфера, программа вечера или то, что ждёт зрителя',
             'form': '[Артист] — [что ждёт на концерте]'},
            {'name': 'кто это',
             'need': 'известны только устойчивые факты об исполнителе (тип, страна или город, жанр, годы), повода и программы нет',
             'form': '[Артист] — [кто это: страна или город, жанр, чем известен]', 'web': True, 'web_only': True},
        ],
        'rules': ('Название — исполнитель или группа на первом месте (как в источнике); если выступают несколько, '
                  'первые двое-трое через запятую. Названия туров, «концерт», «live», «Beograd» не нужны. '
                  'Жанр определяй по описанию и по тому, что знаешь об исполнителе. Что к чему относится: '
                  'рок — рок, хард-рок, готик-кантри; метал — метал, хеви, дэт, трэш; '
                  'панк — панк, хардкор, ска-панк, а также альтернатива и пост-рок; '
                  'инди — инди, синтипоп, дрим-поп, шугейз; поп — поп, поп-рок, небалканская эстрада; '
                  'хип-хоп — рэп и хип-хоп; электроника — техно, хаус, drum\'n\'bass и электронные лайвы; '
                  'вечеринка — диджей-сеты, диско, клубные ночи; джаз — джаз, джаз-фанк, соул; блюз — блюз и блюз-рок; '
                  'классика — классика, фортепиано, камерная музыка; фолк — фолк и кантри; '
                  'этно — world music и реггей. Балканская эстрада, турбофолк и севдалинка — ВСЕГДА «народная», не «поп». '
                  'Диджей-сет и клубная ночь — ВСЕГДА «вечеринка», даже если играют электронику. '
                  'Второй жанр добавляй только если он прямо указан в данных (например, рок-группа на клубной вечеринке: '
                  '["рок", "вечеринка"]); основной жанр первым; если сомневаешься, давай один или пустой список.'),
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


def _int(value, default: int, low: int, high: int) -> int:
    try:
        return min(max(int(value), low), high)
    except (TypeError, ValueError):
        return default


def _version(value, default: str) -> str:
    s = str(value).strip() if value not in (None, '') else ''
    return s or default


def _formulas(raw) -> tuple[Formula, ...]:
    out = []
    for f in raw if isinstance(raw, list) else []:
        if isinstance(f, dict) and str(f.get('name') or '').strip():
            out.append(Formula(name=str(f['name']).strip(), need=str(f.get('need') or '').strip(),
                               form=str(f.get('form') or '').strip(), web=_bool(f.get('web')) or _bool(f.get('web_only')),
                               web_only=_bool(f.get('web_only'))))
    return tuple(out)


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('true', 'yes', 'on', '1') if value not in (None, '') else False


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
        max_values=_int(d.get('max_values'), 1, 1, 3),
        fallback_emoji=str(d.get('fallback_emoji') or '').strip(),
        formulas=_formulas(d.get('formulas')),
        hook_rules=str(d.get('hook_rules') or '').strip(),
        enrich=str(d.get('enrich') or '').strip().lower(),
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
