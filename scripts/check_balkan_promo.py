"""Полная проверка balkan-promo.com и работы наших AI на его событиях. В БД ничего не пишет.

    python scripts/check_balkan_promo.py                    # сайт + скрейпер + AI (категории, перевод)
    python scripts/check_balkan_promo.py --skip-ai          # без обращений к AI (ключи не нужны)
    python scripts/check_balkan_promo.py --db               # + AI-дедупликация против записей из БД (сухой прогон)
    python scripts/check_balkan_promo.py --no-pages --no-tickets --no-images   # только главная и скрейпер

Что проверяется (результат: консоль + папка scripts/bp_check/<дата-время>/, её целиком можно прислать):
  1. САЙТ. Главная: код ответа, редиректы, блок <script id="gigs"> (ключи, типы, новые/пропавшие поля), каждый концерт:
     дата ↔ день недели ↔ «15 ноября», статус/отмена, дубли, клубы и адреса, билеты (EUR/RUB/RSD/крипто), картинки
     (код, тип, размер), страница концерта (код, редирект, дата/название на странице, описание), sitemap.xml,
     версии /sr/ и /en/ (те же концерты и даты?). Билетные ссылки: доступность (по умолчанию проверяются).
  2. СКРЕЙПЕР. Настоящий BalkanPromoScraper.fetch(): сколько отдал сайт и сколько получилось событий, почему
     остальные пропущены, поля каждого события, подозрительное (пустое/одинаковое описание, город не Белград, webp, дубли).
  3. AI.
     а) категории (event_collector.ai_categories): входят ли события balkan-promo в охват при текущем scope
        (у них фиксированная категория «Концерты» — при scope: unreliable AI их не трогает), и что ответил бы AI
        настоящим промптом и моделями из конфига (категории сайта AI только дополняет);
     б) перевод описаний (translator): какие описания уже русские (идут без модели), остальные — через модель;
     в) дедупликация (--db): сухой прогон настоящего движка: события сайта против постов каналов и событий других
        источников из БД на те же даты; видны и старые записи balkan-promo с прежними URL. Нужна БД с записями
        (прод по SSH-туннелю или запуск на сервере: docker compose exec event_collector python scripts/check_balkan_promo.py --db).

Ключи (как в остальных скриптах, .env.local при APP_ENV=local или переменные окружения):
  категории — OPENCODE_EVENTS_API_KEY или OPENCODE_PARSING_API_KEY; перевод — OPENCODE_TRANSLATE_API_KEY;
  дедуп — OPENCODE_DEDUP_API_KEY. --use-parsing-key подставляет ключ парсинга туда, где своего нет.
Билетные ссылки содержат токены виджетов: в консоли и CSV токены скрыты; home.html хранится как отдал сайт.

Код возврата: 0 — проблем нет, 1 — есть ПРОБЛЕМЫ (ВНИМАНИЕ не считается), 2 — не удалось начать (нет сайта/конфига)."""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
import time
import traceback
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8')
    except Exception:
        pass
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault('APP_ENV', 'local')

import httpx  # noqa: E402
from selectolax.parser import HTMLParser  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept-Language": "ru,en;q=0.8"}
KNOWN_KEYS = {'id', 'n', 'w', 'wd', 'club', 'cu', 'where', 'img', 'fp', 'logo', 'u', 'eur', 'rub', 'rsd', 'cr', 'd', 's', 'sl'}
REQUIRED_KEYS = ('id', 'n', 'u', 'd', 'club', 'cu', 'img')
KNOWN_STATUSES = {'', 'cancelled'}
WEEKDAYS = ('пон', 'вто', 'сре', 'чет', 'пят', 'суб', 'вос')
TICKET_KEYS = ('eur', 'rub', 'rsd')
MAX_PHOTO_BYTES = 10 * 1024 * 1024          # photo.max_size_bytes в config.yaml
PLACEHOLDER = '—'
MONTHS_RU = {'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4, 'мая': 5, 'июня': 6, 'июля': 7, 'августа': 8,
             'сентября': 9, 'октября': 10, 'ноября': 11, 'декабря': 12}


def mask(url: str | None) -> str:
    """Ссылка без query (там токены виджетов билетов)."""
    if not url:
        return ''
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}{p.path}" + ('?…' if p.query else '')


class Report:
    def __init__(self, out_dir: Path):
        self.out = out_dir
        self.lines: list[str] = []
        self.counts = Counter()
        self.problems: list[str] = []
        self.warnings: list[str] = []

    def say(self, text: str = '') -> None:
        print(text, flush=True)
        self.lines.append(text)

    def head(self, text: str) -> None:
        self.say(f"\n{'=' * 8} {text} {'=' * 8}")

    def ok(self, text: str) -> None:
        self.counts['ok'] += 1
        self.say(f"  [OK] {text}")

    def warn(self, text: str) -> None:
        self.counts['warn'] += 1
        self.warnings.append(text)
        self.say(f"  [ВНИМАНИЕ] {text}")

    def bad(self, text: str) -> None:
        self.counts['bad'] += 1
        self.problems.append(text)
        self.say(f"  [ПРОБЛЕМА] {text}")

    def info(self, text: str) -> None:
        self.say(f"  {text}")

    def save(self, name: str, text: str) -> None:
        (self.out / name).write_text(text, encoding='utf-8')

    def csv(self, name: str, fields: list[str], rows: list[dict]) -> None:
        with open(self.out / name, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
            w.writeheader()
            w.writerows(rows)


async def safe(rep: Report, title: str, coro) -> object:
    """Сбой одного раздела не отменяет остальные."""
    try:
        return await coro
    except Exception as e:
        rep.bad(f"{title}: раздел оборвался — {type(e).__name__}: {e}")
        rep.save(f"error_{re.sub(r'[^a-z0-9]+', '_', title.lower())}.txt", traceback.format_exc())
        return None


# ----------------------------------------------------------------------------------------------- 1. сайт
async def get(client: httpx.AsyncClient, url: str, **kw) -> tuple[httpx.Response | None, float, str]:
    t = time.monotonic()
    try:
        r = await client.get(url, **kw)
        return r, time.monotonic() - t, ''
    except Exception as e:
        return None, time.monotonic() - t, f"{type(e).__name__}: {e}"


def weekday_ok(d: date, wd: str | None) -> bool | None:
    if not wd:
        return None
    return wd.strip().lower()[:3] == WEEKDAYS[d.weekday()]


def audit_gigs(rep: Report, gigs: list[dict], addresses: dict, bp, today: date) -> list[dict]:
    """Проверка JSON-блока без сети. Возвращает строки для gigs.csv."""
    rep.head(f"1.2 КОНЦЕРТЫ В БЛОКЕ #gigs ({len(gigs)})")
    keys = Counter(k for g in gigs for k in g)
    new = sorted(set(keys) - KNOWN_KEYS)
    gone = sorted(k for k in KNOWN_KEYS if k not in keys and k not in ('logo', 'sl', 's'))
    rep.info("ключи: " + ", ".join(f"{k}×{n}" for k, n in sorted(keys.items())))
    (rep.warn if new else rep.ok)(f"новые поля, которых скрейпер не знает: {new}" if new else "новых полей нет")
    (rep.bad if gone else rep.ok)(f"пропали поля: {gone} — скрейпер может перестать их получать" if gone else "все известные поля на месте")

    statuses = Counter((g.get('s') or '') for g in gigs)
    rep.info(f"статусы: {dict(statuses)}")
    unknown_st = [s for s in statuses if s not in KNOWN_STATUSES]
    if unknown_st:
        rep.warn(f"неизвестные статусы {unknown_st}: скрейпер пропускает только 'cancelled', остальные выйдут как обычные "
                 f"(примеры: {[(g.get('n'), g.get('s'), g.get('sl')) for g in gigs if g.get('s') in unknown_st][:3]})")

    rows = []
    seen_u, seen_id, seen_nd = Counter(), Counter(), Counter()
    for g in gigs:
        seen_u[g.get('u')] += 1
        seen_id[g.get('id')] += 1
        seen_nd[((g.get('n') or '').strip().lower(), g.get('d'), g.get('cu'))] += 1
    for g in gigs:
        title, name = (g.get('n') or '').strip(), (g.get('n') or g.get('id') or '?')
        problems: list[str] = []
        for k in REQUIRED_KEYS:
            if not g.get(k):
                problems.append(f"нет {k}")
        d = None
        try:
            d = date.fromisoformat(g.get('d') or '')
        except ValueError:
            problems.append(f"дата не ISO: {g.get('d')!r}")
        if d:
            if weekday_ok(d, g.get('wd')) is False:
                problems.append(f"день недели не сходится: d={d} ({WEEKDAYS[d.weekday()]}), wd={g.get('wd')!r}")
            m = re.search(r"(\d{1,2})\s+(\S+)", g.get('w') or '')
            if m:
                month = MONTHS_RU.get(m.group(2).lower().strip('.,'))
                if month is None:
                    problems.append(f"месяц в w не распознан: {g.get('w')!r}")
                elif (int(m.group(1)), month) != (d.day, d.month):
                    problems.append(f"w={g.get('w')!r} не совпадает с d={d}")
            elif g.get('w'):
                problems.append(f"w не разобралось: {g.get('w')!r}")
            if (d - today).days > 400:
                problems.append(f"дата слишком далеко: {d}")
        if seen_u[g.get('u')] > 1:
            problems.append("повтор URL концерта")
        if seen_id[g.get('id')] > 1:
            problems.append("повтор id")
        if seen_nd[((g.get('n') or '').strip().lower(), g.get('d'), g.get('cu'))] > 1:
            problems.append("тот же концерт (название+дата+клуб) указан дважды")
        if title != (g.get('n') or ''):
            problems.append("в названии лишние пробелы по краям")
        if re.search(r"<[^>]+>|&\w+;", title):
            problems.append(f"в названии остатки разметки: {title!r}")
        if g.get('cu') and g.get('cu') not in addresses:
            problems.append(f"адрес клуба {g.get('cu')} не найден в блоке клубов")
        has_ticket = any(g.get(k) for k in TICKET_KEYS) or g.get('cr')
        cancelled = g.get('s') == 'cancelled'
        if not has_ticket and not cancelled:
            problems.append("нет ни одной ссылки на билеты")
        if g.get('img') and not str(g['img']).lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
            problems.append(f"необычное расширение картинки: {g['img']}")
        state = 'отменён' if cancelled else ('прошёл' if d and d < today else 'ждём')
        flag = "!" if problems else " "
        rep.info(f"{flag} {d or g.get('d')} {state:7} {name[:40]:40} | {g.get('club') or PLACEHOLDER} | "
                 f"билеты: {','.join(k for k in TICKET_KEYS if g.get(k)) or PLACEHOLDER}{'+крипто' if g.get('cr') else ''}")
        for p in problems:
            rep.warn(f"{name}: {p}") if not p.startswith(('нет id', 'нет u', 'нет d', 'повтор', 'дата не ISO')) else rep.bad(f"{name}: {p}")
        rows.append(dict(id=g.get('id'), title=title, date=g.get('d'), state=state, club=g.get('club'), club_url=g.get('cu'),
                         where=g.get('where'), url=g.get('u'), status=g.get('s') or '', status_text=g.get('sl') or '',
                         img=g.get('img'), eur=mask(g.get('eur')), rub=mask(g.get('rub')), rsd=mask(g.get('rsd')),
                         crypto=g.get('cr') or '', problems='; '.join(problems)))
    if not any(r['state'] == 'ждём' for r in rows):
        rep.warn("нет ни одного будущего неотменённого концерта: после фильтров скрейпер вернёт 0 событий")
    return rows


async def audit_site(rep: Report, client: httpx.AsyncClient, bp, args) -> tuple[str | None, list[dict] | None, dict]:
    rep.head("1.1 ГЛАВНАЯ СТРАНИЦА")
    r, dt, err = await get(client, bp.HOME)
    if r is None:
        rep.bad(f"главная недоступна: {err}")
        return None, None, {}
    rep.save('home.html', r.text)
    rep.info(f"GET {bp.HOME} -> {r.status_code} за {dt:.1f}с, итоговый URL {r.url}, {len(r.text)} симв., "
             f"content-type={r.headers.get('content-type')}, server={r.headers.get('server')}")
    if r.history:
        rep.info("редиректы: " + " -> ".join(f"{h.status_code} {h.url}" for h in r.history))
    (rep.ok if r.status_code == 200 else rep.bad)(f"код ответа {r.status_code}")
    if urlsplit(str(r.url)).netloc != urlsplit(bp.HOME).netloc:
        rep.bad(f"сайт перенаправляет на другой домен: {r.url}")
    tree = HTMLParser(r.text)
    title = tree.css_first('title')
    rep.info(f"title: {title.text(strip=True) if title else None}")

    gigs = bp.parse_gigs(r.text)
    if gigs is None:
        rep.bad("блок <script id=\"gigs\"> не найден или не разобрался JSON — это и есть изменение вёрстки; скрейпер вернёт 0 событий")
        scripts = [(sc.attributes.get('id'), sc.attributes.get('type'), len(sc.text() or '')) for sc in tree.css('script')]
        concert_links = len(re.findall(r"/koncerty/[\w-]+/?", r.text))
        ld_json = len(tree.css('script[type="application/ld+json"]'))
        rep.info(f"скрипты на странице (id, type, длина): {scripts[:25]}")
        rep.info(f"ссылок /koncerty/: {concert_links}, __NEXT_DATA__={bool(tree.css_first('#__NEXT_DATA__'))}, "
                 f"ld+json={ld_json}, .flexbe-card={len(tree.css('.flexbe-card'))}")
        rep.info("Пришлите home.html из папки отчёта — по нему подгоню разбор.")
        return r.text, None, {}
    rep.ok(f"блок #gigs разобран: {len(gigs)} концертов")
    addresses = bp.club_addresses(r.text)
    rep.info(f"клубов с адресом: {len(addresses)} {dict(list(addresses.items())[:6])}")
    if not addresses:
        rep.warn("адреса клубов не найдены (блок #kluby): город будет определяться только по полю where")
    return r.text, gigs, addresses


async def audit_pages(rep: Report, client: httpx.AsyncClient, bp, gigs: list[dict], rows: list[dict], args, today: date) -> dict:
    """Страницы концертов, картинки, билеты. Возвращает {id: описание со страницы}."""
    by_id = {r['id']: r for r in rows}
    live = [g for g in gigs if g.get('s') != 'cancelled' and g.get('u')]
    if args.max_pages:
        live = live[:args.max_pages]
    descs: dict[str, str | None] = {}
    if not args.no_pages:
        rep.head(f"1.3 СТРАНИЦЫ КОНЦЕРТОВ ({len(live)})")
        (rep.out / 'pages').mkdir(exist_ok=True)
        for g in live:
            url = bp._abs(g['u'])
            r, dt, err = await get(client, url)
            name = g.get('n') or g.get('id')
            if r is None:
                rep.bad(f"{name}: страница недоступна ({err})")
                continue
            (rep.out / 'pages' / f"{re.sub(r'[^A-Za-z0-9_-]', '_', str(g.get('id')))}.html").write_text(r.text, encoding='utf-8')
            problems = []
            if r.status_code != 200:
                problems.append(f"код {r.status_code}")
            if r.history:
                problems.append(f"редирект на {r.url}")
            t = HTMLParser(r.text)
            page_text = t.body.text(separator=' ') if t.body else r.text
            if g.get('w') and g['w'].lower() not in page_text.lower():
                problems.append(f"на странице нет даты {g['w']!r}")
            if (g.get('n') or '').lower() not in page_text.lower() and (g.get('n') or '').lower() not in (t.css_first('title').text().lower() if t.css_first('title') else ''):
                problems.append("название не найдено на странице")
            hero = t.css_first('.c-ph img')
            if hero and g.get('img') and Path(urlsplit(hero.attributes.get('src') or '').path).name != Path(urlsplit(g['img']).path).name:
                problems.append(f"картинка на странице другая: {Path(urlsplit(hero.attributes.get('src') or '').path).name} vs {Path(urlsplit(g['img']).path).name}")
            desc = bp.extract_description(r.text)
            descs[g.get('id')] = desc
            if not desc:
                problems.append("нет блока «О концерте» (section.c-about): у события не будет описания")
            elif len(desc) < 60:
                problems.append(f"описание очень короткое ({len(desc)} симв.)")
            rep.info(f"{'!' if problems else ' '} {name[:38]:38} {r.status_code} {dt:.1f}с {len(r.text)//1024}KB описание={len(desc) if desc else 0} симв.")
            for p in problems:
                rep.warn(f"{name}: {p}")
            by_id.get(g.get('id'), {})['page_problems'] = '; '.join(problems)
            await asyncio.sleep(bp.EVENT_DELAY)

    if not args.no_images:
        rep.head("1.4 КАРТИНКИ")
        seen = {}
        for g in live:
            img = bp._abs(g.get('img'))
            if not img or img in seen:
                continue
            r, dt, err = await get(client, img)
            name = g.get('n') or g.get('id')
            if r is None:
                seen[img] = None
                rep.bad(f"{name}: картинка недоступна ({err})")
                continue
            ctype, size = r.headers.get('content-type', ''), len(r.content)
            seen[img] = (r.status_code, ctype, size)
            problems = []
            if r.status_code != 200:
                problems.append(f"код {r.status_code}")
            if not ctype.startswith('image/'):
                problems.append(f"content-type {ctype!r}")
            if size < 5 * 1024:
                problems.append(f"подозрительно мала ({size} байт)")
            if size > MAX_PHOTO_BYTES:
                problems.append(f"больше лимита фото {MAX_PHOTO_BYTES // 1024 // 1024} МБ ({size // 1024} КБ)")
            if ctype.endswith('webp') or img.lower().endswith('.webp'):
                rep.info(f"  (webp) {name}: Telegram может не принять webp как фото — проверьте, как бот отправляет эту картинку")
            for p in problems:
                rep.warn(f"{name}: картинка {mask(img)}: {p}")
            if not problems:
                rep.info(f"  {name[:38]:38} {r.status_code} {ctype} {size // 1024} КБ")
        broken = [u for u, v in seen.items() if v is None or v[0] != 200]
        (rep.bad if broken else rep.ok)(f"картинок проверено {len(seen)}, проблемных {len(broken)}")

    if not args.no_tickets:
        rep.head("1.5 БИЛЕТНЫЕ ССЫЛКИ")
        checked = Counter()
        for g in live:
            for k in TICKET_KEYS:
                url = g.get(k)
                if not url:
                    continue
                r, dt, err = await get(client, url)
                label = f"{g.get('n')} [{k}] {mask(url)}"
                if r is None:
                    rep.warn(f"{label}: недоступна ({err})")
                    checked['fail'] += 1
                elif r.status_code >= 400:
                    rep.warn(f"{label}: код {r.status_code}")
                    checked['fail'] += 1
                else:
                    checked['ok'] += 1
        rep.info(f"билетных ссылок проверено: {dict(checked)} (виджеты билетов часто закрыты для ботов — единичные 403 не критичны)")
        if checked['ok'] == 0 and checked['fail'] == 0:
            rep.warn("у будущих концертов нет билетных ссылок")
    return descs


async def audit_extras(rep: Report, client: httpx.AsyncClient, bp, gigs: list[dict]) -> None:
    rep.head("1.6 SITEMAP И ЯЗЫКОВЫЕ ВЕРСИИ")
    ids = {g.get('u'): g for g in gigs}
    r, _, err = await get(client, urljoin(bp.HOME, 'sitemap.xml'))
    if r is None or r.status_code != 200:
        rep.info(f"sitemap.xml: {err or r.status_code} (не критично)")
    else:
        locs = re.findall(r"<loc>([^<]+)</loc>", r.text)
        concerts = [u for u in locs if '/koncerty/' in u]
        in_gigs = {bp._abs(u) for u in ids}
        missing = [u for u in concerts if u not in in_gigs and u.rstrip('/') + '/' not in in_gigs]
        rep.info(f"sitemap: ссылок {len(locs)}, концертов {len(concerts)}")
        (rep.warn if missing else rep.ok)(
            f"в sitemap есть концерты, которых нет в #gigs ({len(missing)}): {missing[:5]}" if missing else "все концерты sitemap есть в #gigs")
    for lang in ('sr', 'en'):
        r, _, err = await get(client, urljoin(bp.HOME, f'{lang}/'))
        if r is None or r.status_code != 200:
            rep.info(f"/{lang}/: {err or r.status_code}")
            continue
        other = bp.parse_gigs(r.text)
        if other is None:
            rep.warn(f"/{lang}/: блок #gigs не найден")
            continue
        mine = {g.get('id'): g.get('d') for g in gigs}
        theirs = {g.get('id'): g.get('d') for g in other}
        diff = {i: (mine.get(i), theirs.get(i)) for i in set(mine) | set(theirs) if mine.get(i) != theirs.get(i)}
        (rep.warn if diff else rep.ok)(f"/{lang}/: концертов {len(other)}; расхождения по id/датам с русской версией: {diff}" if diff
                                       else f"/{lang}/: те же {len(other)} концертов и даты, что в русской версии")


# ----------------------------------------------------------------------------------------------- 2. скрейпер
async def audit_scraper(rep: Report, bp, gigs: list[dict] | None, addresses: dict, today: date) -> list:
    rep.head("2. СКРЕЙПЕР (настоящий BalkanPromoScraper.fetch)")
    scraper = bp.BalkanPromoScraper()
    t = time.monotonic()
    items = await scraper.fetch()
    rep.info(f"fetch за {time.monotonic() - t:.1f}с: сайт отдал {scraper.listed_count}, событий получилось {len(items)}")
    if scraper.listed_count == 0 and not items:
        rep.bad("скрейпер вернул 0 событий и listed_count=0 — здоровье источника в админке будет BROKEN")
    if gigs is not None:
        reasons = Counter()
        for g in gigs:
            if scraper._build_item(g, addresses, today) is None:
                reasons['отменён' if g.get('s') == 'cancelled' else 'прошёл/нет даты/нет названия или URL'] += 1
        rep.info(f"пропущено скрейпером: {dict(reasons) or 'ничего'}")
        built = len(gigs) - sum(reasons.values())
        (rep.ok if built == len(items) else rep.warn)(
            f"по блоку #gigs ожидалось {built} событий, fetch вернул {len(items)}" + ("" if built == len(items) else " — расхождение: ошибки страниц/описаний (смотрите BALKAN_EVENT_ERROR в логе)"))

    descs = Counter(i.description for i in items if i.description)
    keys = Counter(i.dedup_key for i in items)
    urls = Counter(i.source_url for i in items)
    rows = []
    for i in items:
        problems = []
        if not i.description:
            problems.append("нет описания")
        elif len(i.description) < 80:
            problems.append(f"короткое описание ({len(i.description)})")
        if i.description and descs[i.description] > 1:
            problems.append("описание такое же у другого события (общий текст сайта?)")
        if i.description and any(m in i.description.lower() for m in bp._SERVICE_MARKERS):
            problems.append("в описании служебный текст (про билеты/оплату)")
        if not i.venue:
            problems.append("нет площадки")
        if i.city != 'Belgrade':
            problems.append(f"город {i.city!r}: бот показывает только Белград, событие в ленту не попадёт")
        if not (i.images or '').startswith('http'):
            problems.append(f"картинка не абсолютная: {i.images!r}")
        elif i.images.lower().endswith('.webp'):
            problems.append("картинка webp: проверьте, что бот её показывает")
        if not i.dedup_key:
            problems.append("нет dedup_key")
        if keys[i.dedup_key] > 1:
            problems.append("повтор dedup_key")
        if urls[i.source_url] > 1:
            problems.append("повтор source_url")
        if i.starts_at.tzinfo is None:
            problems.append("starts_at без часового пояса")
        if i.event_types != ['Концерты'] or i.category_basis != 'fixed':
            problems.append(f"категории неожиданные: {i.event_types} / {i.category_basis}")
        rep.info(f"{'!' if problems else ' '} {i.starts_at.date()} {i.title[:34]:34} | {i.venue} | {i.city} | описание {len(i.description or '')} симв.")
        for p in problems:
            (rep.warn)(f"{i.title}: {p}")
        rows.append(dict(title=i.title, date=i.starts_at.date(), time=i.starts_at.strftime('%H:%M'), venue=i.venue, city=i.city,
                         url=i.source_url, image=i.images, categories='|'.join(i.event_types), basis=i.category_basis,
                         dedup_key=i.dedup_key, description_len=len(i.description or ''),
                         description=(i.description or '')[:600], problems='; '.join(problems)))
    rep.csv('items.csv', ['title', 'date', 'time', 'venue', 'city', 'url', 'image', 'categories', 'basis', 'dedup_key',
                          'description_len', 'description', 'problems'], rows)
    return items


# ----------------------------------------------------------------------------------------------- 3. AI
def pick_key(own: str | None, args, config) -> str:
    return own or (config['parsing'].get('api_key') if args.use_parsing_key else '') or ''


async def ai_categories(rep: Report, items: list, args) -> None:
    from collector.categorizer_v2 import category_names
    from event_collector import ai_categories as ac
    from shared.ai import AIUnavailable, client_from_config
    from shared.config import config
    rep.head("3а. AI-КАТЕГОРИИ")
    cfg = dict(config.get('event_categories') or {})
    scope, mode = cfg.get('scope', 'unreliable'), cfg.get('method', 'source')
    rep.info(f"конфиг: method={mode}, scope={scope}, models={cfg.get('models')}")
    basis = {i.category_basis for i in items}
    in_scope = scope == 'all' or bool(basis & set(ac.UNRELIABLE_BASES))
    if mode == 'source':
        rep.info("method=source: AI на событиях сайтов не вызывается вообще")
    elif not in_scope:
        rep.ok(f"события balkan-promo (basis={sorted(basis)}) при scope={scope} AI не обрабатывает: категория «Концерты» фиксирована")
    else:
        rep.info(f"при scope={scope} AI обработает все события balkan-promo, но категории сайта только дополнит")
    if not items:
        rep.info("событий нет — проверять нечего")
        return
    models = [m.strip() for m in args.models.split(',')] if args.models else (cfg.get('models') or (config.get('parsing') or {}).get('models') or [])
    cfg['models'] = models
    cfg['api_key'] = pick_key(cfg.get('api_key'), args, config)
    if args.ai_base_url:
        cfg['base_url'] = args.ai_base_url
    if not cfg['api_key'] or not models:
        rep.warn("нет ключа (OPENCODE_EVENTS_API_KEY / OPENCODE_PARSING_API_KEY) или моделей: проверка AI-категорий пропущена")
        return
    client = client_from_config('check-balkan-categories', cfg)
    client.begin_run()
    allowed, max_chars = category_names(), int(cfg.get('max_chars', 1200))
    rows = []
    rep.info(f"гоним настоящий промпт, модели: {', '.join(models)}")
    for i in items:
        try:
            got = ac.parse_response(await client.complete(ac.SYSTEM_PROMPT, ac.build_user_prompt(i, allowed, max_chars)), allowed)
            client.report_ok()
        except AIUnavailable as e:
            rep.bad(f"модель недоступна: {e}")
            break
        except Exception as e:
            client.report_bad_output()
            rep.bad(f"{i.title}: плохой ответ модели ({type(e).__name__}: {e})")
            continue
        merged = ac.merge_categories(i, got)
        extra = [c for c in got if c not in i.event_types]
        note = ''
        if 'Концерты' not in got:
            note = 'AI не считает это концертом'
            rep.warn(f"{i.title}: AI не поставил «Концерты» ({got}) — проверьте, это концерт?")
        elif extra:
            note = f"AI добавил: {extra}"
            rep.info(f"  {i.title}: AI добавил к «Концерты»: {extra}")
        rep.info(f"  {i.title[:40]:40} AI={got} итог при scope=all: {merged}")
        rows.append(dict(title=i.title, site=i.event_types, ai=got, merged=merged, note=note))
    await client.aclose()
    rep.info(client.stats.line())
    rep.csv('ai_categories.csv', ['title', 'site', 'ai', 'merged', 'note'], rows)


async def ai_translation(rep: Report, items: list, args) -> None:
    from shared.ai import AIUnavailable, client_from_config
    from shared.config import config
    from translator.service import translation_settings
    from translator.text import (SYSTEM_PROMPT, BadTranslation, build_description_ru, cut_input, is_russian, validate)
    rep.head("3б. AI-ПЕРЕВОД ОПИСАНИЙ")
    tcfg = dict(config['translation'])
    st = translation_settings(tcfg)
    rep.info(f"конфиг: method={st.method}, sources={list(st.sources)}, models={tcfg.get('models')}, max_chars={st.max_chars}")
    if 'balkan-promo' not in st.sources:
        rep.warn("balkan-promo нет в translation.sources: описания переводиться не будут")
    if st.method == 'off':
        rep.info("translation.method=off: на проде перевод сейчас выключен (проверка ниже показывает, что было бы при включении)")
    todo = [i for i in items if i.description]
    if not todo:
        rep.info("нет описаний для проверки")
        return
    russian = [i for i in todo if is_russian(i.description)]
    rep.info(f"описаний {len(todo)}: уже на русском (не переводятся, description_ru не пишется, бот покажет оригинал) — {len(russian)}, к переводу — {len(todo) - len(russian)}")
    foreign = [i for i in todo if i not in russian]
    if not foreign:
        rep.ok("все описания русские: переводчик для balkan-promo ничего не делает")
        return
    tcfg['api_key'] = pick_key(tcfg.get('api_key'), args, config)
    if args.models:
        tcfg['models'] = [m.strip() for m in args.models.split(',')]
    if args.ai_base_url:
        tcfg['base_url'] = args.ai_base_url
    if not tcfg['api_key'] or not tcfg.get('models'):
        rep.warn("нет OPENCODE_TRANSLATE_API_KEY или translation.models: перевод не проверен")
        return
    client = client_from_config('check-balkan-translate', tcfg)
    client.begin_run()
    rows = []
    for i in foreign:
        sent = cut_input(i.description, st.input_chars)
        try:
            out = await client.complete(SYSTEM_PROMPT, sent)
            validate(out, sent)
            client.report_ok()
        except AIUnavailable as e:
            rep.bad(f"модель недоступна: {e}")
            break
        except BadTranslation as e:
            client.report_bad_output()
            rep.bad(f"{i.title}: ответ не прошёл проверку ({e})")
            continue
        final = build_description_ru(out, i.source_url, st.max_chars)
        ru = is_russian(out)
        (rep.ok if ru else rep.warn)(f"{i.title[:40]}: переведено ({len(out)} симв.), на русском: {ru}")
        rep.info(f"    было: {sent[:120]!r}\n    стало: {out[:160]!r}")
        rows.append(dict(title=i.title, original=sent[:400], translated=out[:600], final=final[:700], russian=ru))
    await client.aclose()
    rep.info(client.stats.line())
    rep.csv('translations.csv', ['title', 'original', 'translated', 'final', 'russian'], rows)


async def ai_dedup(rep: Report, items: list, args) -> None:
    from deduplicator import runner, store
    from deduplicator.records import Record
    from shared.ai import client_from_config
    from shared.config import config
    rep.head("3в. AI-ДЕДУПЛИКАЦИЯ (сухой прогон, БД только читается)")
    cfg = config['deduplication']
    ai = dict(cfg['ai'])
    ai['api_key'] = pick_key(ai.get('api_key'), args, config)
    if args.models:
        ai['models'] = [m.strip() for m in args.models.split(',')]
    if args.ai_base_url:
        ai['base_url'] = args.ai_base_url
    if not ai['api_key'] or not ai.get('models'):
        rep.warn("нет OPENCODE_DEDUP_API_KEY (или --use-parsing-key) / deduplication.ai.models: дедуп не проверен")
        return
    if not items:
        rep.info("событий нет — сравнивать нечего")
        return
    from db.connection import close_db_pool, init_db_pool
    pool = await init_db_pool()
    try:
        days = sorted({i.starts_at.date() for i in items})[:args.dedup_days]
        mine = [i for i in items if i.starts_at.date() in days]
        rep.info(f"даты для сравнения: {', '.join(map(str, days))}; событий balkan-promo на них: {len(mine)}")
        db_records = await store.load_records(pool, days[0], days[-1])
        urls = {i.source_url for i in mine}
        old = [r for r in db_records if r.source == 'event:balkan-promo' and r.url not in urls and r.day in days]
        stored = [r for r in db_records if r.source == 'event:balkan-promo' and r.url in urls]
        if old:
            rep.warn(f"в БД на эти даты есть записи balkan-promo с ДРУГИМИ URL ({len(old)}): вероятные старые дубли (прежняя вёрстка сайта)")
            for r in old[:10]:
                rep.info(f"    id={r.id} {r.day} {r.url} | {r.text[:80]!r}")
        if stored:
            rep.info(f"эти концерты уже есть в БД ({len(stored)}); в сравнение берутся свежие данные сайта")
        base = [r for r in db_records if not (r.source == 'event:balkan-promo' and r.url in urls)]
        synthetic = [Record(kind='e', id=-(n + 1), day=i.starts_at.date(), source='event:balkan-promo',
                            text='. '.join(p.strip() for p in (i.title, i.venue or '', (i.description or '')[:store.EVENT_DESC_CHARS]) if p and p.strip()),
                            city=i.city, url=i.source_url) for n, i in enumerate(mine)]
        for d in days:
            same = [r for r in base if r.day == d]
            rep.info(f"  {d}: в БД других записей на этот день — {len(same)} (посты каналов {sum(r.kind == 'm' for r in same)}, события {sum(r.kind == 'e' for r in same)})")
        if not any(r.day in days for r in base):
            rep.warn("в БД нет других записей на эти даты (пустая локальная база?): дедуп сравнивать не с чем — запустите на сервере или через туннель к проду")

        real_load = store.load_records

        async def patched(p, start, end):
            return [r for r in base + synthetic if start <= r.day <= end]
        store.load_records = patched
        client = client_from_config('check-balkan-dedup', {**ai, 'timeout': args.timeout})
        try:
            run_cfg = {'ai': {**ai, 'concurrency': 2}}
            res = await runner.run_dedup(pool, client, run_cfg, method='shadow', dry_run=True, force=True, only_days=days,
                                         ignore_hidden=True, ignore_decisions=True)
        finally:
            store.load_records = real_load
            await client.aclose()
        if res.status != 'ok':
            rep.bad(f"прогон дедупа: {res.status} — {res.error}")
            return
        st = res.stats
        rep.info(f"запросов {st['requests']}, tokens in/out {st['tokens_in']}/{st['tokens_out']}, групп {st['groups_found']}, плохих ответов {st['bad_days']}")
        rows, hit = [], 0
        for plan in res.plans:
            losers = {nd.loser.key: nd for nd in plan.wanted}
            for gi, g in enumerate(plan.groups, 1):
                members = list(g.members)
                if not any(m.id < 0 and m.source == 'event:balkan-promo' for m in members):
                    continue
                hit += 1
                rep.info(f"  группа {plan.day}#{gi} — {g.reason or 'без причины'}")
                for m in sorted(members, key=res.resolver.rank):
                    role = 'ПРОИГРАЕТ' if m.key in losers else 'остаётся'
                    mine_flag = ' (событие с сайта)' if m.id < 0 else ''
                    rep.info(f"     [{role}] {res.resolver.label(m)} id={m.id}{mine_flag}: {m.text[:110]!r}")
                    rows.append(dict(day=plan.day, group=gi, role=role, source=res.resolver.label(m), id=m.id,
                                     is_site_event=m.id < 0, text=m.text[:300], reason=g.reason))
        (rep.info if hit else rep.ok)(f"групп с событиями balkan-promo: {hit}" if hit else
                                      "AI не нашёл дублей у событий balkan-promo: все считаются уникальными")
        # для ручной сверки: что в БД на те же дни упоминает клуб/название
        rep.info("  Для ручной сверки (записи БД на те же дни с названием или клубом из события):")
        for i in mine:
            words = {w.lower() for w in re.findall(r"[\w'’]{4,}", f"{i.title} {i.venue or ''}")}
            near = [r for r in base if r.day == i.starts_at.date() and any(w in r.text.lower() for w in words)]
            if near:
                rep.info(f"   {i.title}:")
                for r in near[:5]:
                    rep.info(f"      {res.resolver.label(r)} id={r.id}: {r.text[:100]!r}")
        rep.csv('dedup.csv', ['day', 'group', 'role', 'source', 'id', 'is_site_event', 'text', 'reason'], rows)
    finally:
        await close_db_pool(pool)


# ----------------------------------------------------------------------------------------------- main
async def main_async(args) -> int:
    out_dir = Path(args.out_dir) / datetime.now().strftime('%Y%m%d_%H%M%S')
    out_dir.mkdir(parents=True, exist_ok=True)
    rep = Report(out_dir)
    try:
        from loguru import logger
        logger.remove()
        logger.add(sys.stderr, level='WARNING')
        from event_collector.scrapers import balkan_promo as bp
        from shared.config import config  # noqa: F401
    except FileNotFoundError as e:
        sys.exit(f"{e}\nЗадайте APP_ENV=production (и переменные окружения/.env) либо создайте .env.local.")

    if args.base_url:                                   # для тестов на копии сайта
        bp.HOME, bp.BASE = args.base_url.rstrip('/') + '/', args.base_url.rstrip('/')
    today = datetime.now(bp.BELGRADE_TZ).date()
    rep.say(f"Проверка balkan-promo: {bp.HOME} · сегодня по Белграду {today} · отчёт: {out_dir}")

    async with httpx.AsyncClient(timeout=25, headers=UA, follow_redirects=True) as client:
        home_html, gigs, addresses = await safe(rep, 'сайт', audit_site(rep, client, bp, args)) or (None, None, {})
        if gigs is not None:
            try:
                rows = audit_gigs(rep, gigs, addresses, bp, today)
            except Exception as e:
                rep.bad(f"разбор концертов оборвался — {type(e).__name__}: {e}")
                rep.save('error_gigs.txt', traceback.format_exc())
                rows = []
            await safe(rep, 'страницы', audit_pages(rep, client, bp, gigs, rows, args, today))
            await safe(rep, 'sitemap', audit_extras(rep, client, bp, gigs))
            rep.save('gigs.json', json.dumps([{**g, **{k: mask(g.get(k)) for k in TICKET_KEYS if g.get(k)}} for g in gigs],
                                             ensure_ascii=False, indent=1))
            rep.csv('gigs.csv', ['id', 'title', 'date', 'state', 'club', 'club_url', 'where', 'url', 'status', 'status_text', 'img',
                                 'eur', 'rub', 'rsd', 'crypto', 'page_problems', 'problems'], rows)

    items = await safe(rep, 'скрейпер', audit_scraper(rep, bp, gigs, addresses, today)) or []
    if not args.skip_ai:
        await safe(rep, 'AI-категории', ai_categories(rep, items, args))
        await safe(rep, 'AI-перевод', ai_translation(rep, items, args))
        if args.db:
            await safe(rep, 'AI-дедуп', ai_dedup(rep, items, args))
        else:
            rep.head("3в. AI-ДЕДУПЛИКАЦИЯ")
            rep.info("пропущена: нужен --db (БД с записями каналов и других сайтов на те же даты)")

    rep.head("ИТОГ")
    rep.say(f"  проверок OK: {rep.counts['ok']} · ВНИМАНИЕ: {rep.counts['warn']} · ПРОБЛЕМЫ: {rep.counts['bad']}")
    for p in rep.problems:
        rep.say(f"  ПРОБЛЕМА: {p}")
    for w in rep.warnings[:40]:
        rep.say(f"  внимание: {w}")
    if len(rep.warnings) > 40:
        rep.say(f"  … и ещё {len(rep.warnings) - 40} (см. report.txt)")
    rep.save('report.txt', '\n'.join(rep.lines))
    rep.say(f"\nОтчёт сохранён в {out_dir}. Заархивируйте папку и пришлите вместе с выводом консоли.")
    return 1 if rep.counts['bad'] else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--skip-ai', action='store_true', help='не обращаться к AI')
    ap.add_argument('--db', action='store_true', help='проверить дедупликацию против записей из БД (только чтение)')
    ap.add_argument('--no-pages', action='store_true', help='не открывать страницы концертов')
    ap.add_argument('--no-images', action='store_true', help='не проверять картинки')
    ap.add_argument('--no-tickets', action='store_true', help='не проверять билетные ссылки')
    ap.add_argument('--max-pages', type=int, default=0, help='проверять не больше N концертов (0 — все)')
    ap.add_argument('--models', help='модели через запятую вместо конфига (для всех AI-проверок)')
    ap.add_argument('--use-parsing-key', action='store_true', help='подставить ключ парсинга, если своего ключа нет')
    ap.add_argument('--dedup-days', type=int, default=5, help='сколько ближайших дат сравнивать в дедупе (по умолчанию 5)')
    ap.add_argument('--timeout', type=float, default=120, help='таймаут AI-запроса дедупа, сек')
    ap.add_argument('--base-url', help='другой адрес сайта (копия для проверки скрипта)')
    ap.add_argument('--ai-base-url', help='другой адрес AI-шлюза')
    ap.add_argument('--out-dir', default=str(HERE / 'bp_check'))
    sys.exit(asyncio.run(main_async(ap.parse_args())))


if __name__ == '__main__':
    main()
