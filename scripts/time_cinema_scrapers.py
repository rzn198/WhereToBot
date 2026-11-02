"""Замер скраперов кинотеатров: время, число HTTP-запросов, паузы на retry.

Без БД и без Telegram — только сеть. Запуск из корня проекта:

    python scripts/time_cinema_scrapers.py            # все источники по очереди
    python scripts/time_cinema_scrapers.py --only cineplexx roda
    python scripts/time_cinema_scrapers.py --json out.json

В конце — таблица и оценка, сколько вышло бы при параллельной работе.
"""
import argparse
import json
import statistics
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import os
os.environ.setdefault('APP_ENV', 'local')

import requests  # noqa: E402

_lock = threading.Lock()
_cur = {'source': None}
CALLS: dict[str, list] = {}      # source -> [(seconds, url, ok)]
SLEEPS: dict[str, float] = {}    # source -> seconds


def _rec(url, sec, ok):
    with _lock:
        CALLS.setdefault(_cur['source'], []).append((sec, str(url), ok))


_orig_get = requests.get
_orig_urlopen = urllib.request.urlopen
_orig_sleep = time.sleep


def _get(url, *a, **k):
    t = time.perf_counter()
    try:
        r = _orig_get(url, *a, **k)
        _rec(url, time.perf_counter() - t, r.status_code < 400)
        return r
    except Exception:
        _rec(url, time.perf_counter() - t, False)
        raise


def _urlopen(req, *a, **k):
    url = getattr(req, 'full_url', req)
    t = time.perf_counter()
    try:
        r = _orig_urlopen(req, *a, **k)
        _rec(url, time.perf_counter() - t, True)
        return r
    except Exception:
        _rec(url, time.perf_counter() - t, False)
        raise


def _sleep(s):
    with _lock:
        SLEEPS[_cur['source']] = SLEEPS.get(_cur['source'], 0.0) + s
    _orig_sleep(s)


requests.get, urllib.request.urlopen, time.sleep = _get, _urlopen, _sleep


def build_jobs(cinemas, only):
    from cinema_collector.scrapers.cineplexx import CineplexxScraper
    from cinema_collector.scrapers.tuckwood import TuckwoodScraper
    from cinema_collector.scrapers.cinegrand import CineGrandScraper
    from cinema_collector.scrapers.cinestar import CineStarScraper
    from cinema_collector.scrapers.roda import RodaScraper
    from cinema_collector.scrapers.mts_dvorana import MtsDvoranaScraper
    jobs = [('cineplexx', lambda: [CineplexxScraper(cinemas.get('cineplexx', []))])]
    for key, cls in (('tuckwood', TuckwoodScraper), ('cinegrand', CineGrandScraper),
                     ('cinestar', CineStarScraper), ('roda', RodaScraper),
                     ('mts_dvorana', MtsDvoranaScraper)):
        jobs.append((key, (lambda k=key, c=cls: [c(x) for x in cinemas.get(k, [])])))
    return [j for j in jobs if not only or j[0] in only]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', nargs='*')
    ap.add_argument('--json')
    args = ap.parse_args()

    from cinema_collector.runner import load_cinemas
    from loguru import logger
    logger.remove()
    logger.add(sys.stderr, level='WARNING')
    cinemas = load_cinemas()

    rows = []
    total0 = time.perf_counter()
    for source, make in build_jobs(cinemas, args.only):
        _cur['source'] = source
        t0 = time.perf_counter()
        events, error = [], None
        try:
            for sc in make():
                events += sc.fetch()
        except Exception as e:                       # скрапер упал целиком
            error = f'{type(e).__name__}: {e}'
        wall = time.perf_counter() - t0
        calls = CALLS.get(source, [])
        http = sum(c[0] for c in calls)
        slept = SLEEPS.get(source, 0.0)
        durs = sorted((c[0] for c in calls), reverse=True)
        rows.append(dict(
            source=source, wall=wall, calls=len(calls), failed=sum(1 for c in calls if not c[2]),
            http=http, slept=slept, other=max(wall - http - slept, 0.0),
            avg=(http / len(calls)) if calls else 0.0, p_max=durs[0] if durs else 0.0,
            events=len(events), movies=len({e.movie_id or e.movie_title for e in events}),
            first=min((e.showtime for e in events), default=None),
            last=max((e.showtime for e in events), default=None),
            # потолок ускорения запросов при N воркерах: не быстрее самого долгого запроса
            est5=(wall - http) + max(http / 5, durs[0] if durs else 0.0),
            error=error,
        ))
    total = time.perf_counter() - total0

    print()
    hdr = f"{'источник':<12}{'всего,с':>8}{'запросов':>9}{'ошиб.':>6}{'в HTTP,с':>9}{'пауз,с':>7}{'ср.зап.,с':>10}{'макс,с':>7}{'сеансов':>8}{'фильмов':>8}{'~5 потоков,с':>13}"
    print(hdr)
    print('-' * len(hdr))
    for r in rows:
        print(f"{r['source']:<12}{r['wall']:>8.1f}{r['calls']:>9}{r['failed']:>6}{r['http']:>9.1f}{r['slept']:>7.1f}"
              f"{r['avg']:>10.2f}{r['p_max']:>7.2f}{r['events']:>8}{r['movies']:>8}{r['est5']:>13.1f}"
              + (f"   ОШИБКА: {r['error']}" if r['error'] else ''))
    seq = sum(r['wall'] for r in rows)
    par_sources = max((r['wall'] for r in rows), default=0)
    par_both = max((r['est5'] for r in rows), default=0)
    print('-' * len(hdr))
    print(f"Сейчас (источники по очереди):                    {seq:6.1f} c   (замер всего: {total:.1f} c)")
    print(f"Источники параллельно, внутри как есть:           {par_sources:6.1f} c")
    print(f"Источники параллельно + 5 потоков внутри (оценка): {par_both:6.1f} c")
    for r in rows:
        if r['events'] == 0 or r['error']:
            print(f"!! {r['source']}: сеансов нет{' — ' + r['error'] if r['error'] else ''}")
    slow = sorted(((sec, ok, url, src) for src, cs in CALLS.items() for sec, url, ok in cs), reverse=True)[:5]
    print('\nСамые долгие запросы:')
    for sec, ok, url, src in slow:
        print(f"  {sec:5.2f} c  {'ok ' if ok else 'ERR'} [{src}] {url[:110]}")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, default=str, ensure_ascii=False, indent=1), encoding='utf-8')


if __name__ == '__main__':
    main()
